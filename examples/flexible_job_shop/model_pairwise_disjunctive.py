"""Reference CP-SAT script: flexible job shop via pairwise reified disjunction.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan, like model.py. The difference is the machine-exclusion encoding: no
interval variables at all. Each alternative gets an assignment literal, a
task's duration becomes the linear expression `sum(d_a * lit_a)`, and machine
exclusion is an explicit boolean "A before B" literal per PAIR of tasks that
could share a machine -- the classical disjunctive-graph encoding.

The FJSP twist over the fixed-machine version in ../job_shop is that the
sequencing is only binding when both tasks actually chose that machine, so
each implication carries THREE enforcement literals rather than one:

    add(start[b] >= end[a]).only_enforce_if([lit_a_m, lit_b_m, before])

This is the direct lift of ../job_shop/model_pairwise_disjunctive.py, which on
the fixed-machine ft10 benchmark reached proven optimality in ~3.8s versus
~12.6s for the interval/no-overlap encoding. Whether that advantage survives
machine flexibility is precisely what this file is here to measure, and there
is reason to doubt it: the pair count grows quadratically in how many
alternatives land on a machine, so flexibility inflates it far faster than
problem size alone does.

Two prunings keep the encoding honest rather than needlessly bloated:
- two tasks of the SAME job never need a `before` literal, because the job
  precedence chain already orders them;
- a task with a single alternative contributes no enforcement literal, because
  its machine is not a decision.

A greedy earliest-completion-time warm start supplies hints for both the
timing and the machine choice.

Measured (single worker, seed 42, 600s cap; raw runs in results/ -- mk01
current, mk15 and behnke predating a later stdout change that added
num_tasks, kept rather than re-solved because no change since touched the
model itself): the ft10 advantage does NOT survive machine flexibility.
- mk01: optimal 40 in 0.1s, tying the other two.
- mk15: best 381 (worst of three) with a bound of just 199 against a known
  optimum of 333 -- a lower-bound collapse, not a search failure. Two causes,
  both absent from the fixed-machine case: there are no interval variables, so
  CP-SAT has no disjunctive propagator and loses edge-finding entirely; and
  the three-literal guard leaves every implication DORMANT until both machine
  choices are fixed, so near the root the encoding propagates almost nothing.
  In ../job_shop those guards do not exist and each implication is live
  immediately.
- behnke lar04_1: 748,539 booleans and 1,442,793 constraints, 4.3s to build,
  and only 10,026 conflicts explored in the whole 600s. Its final 427 EQUALS
  the greedy warm start it was handed -- the search improved on the hint by
  nothing at all. Read 427 as the heuristic's score, not this formulation's.

That last point is why this file bundles two changes and must be reported as
such: the encoding and the warm start cannot be separated from its results
without a fourth variant that hints without the pairwise encoding.

Run from the repository root:
    uv run examples/flexible_job_shop/model_pairwise_disjunctive.py data_mk01.json 10
"""

import collections
import itertools
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict, Field


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class Alternative(FrozenModel):
    machine: int
    duration: int


class TaskSpec(FrozenModel):
    alternatives: list[Alternative]


class ProblemInstance(FrozenModel):
    jobs: list[list[TaskSpec]]
    num_machines: int


class ScheduleEntry(FrozenModel):
    job: int
    task: int
    machine: int
    start: int
    duration: int
    end: int


class Solution(FrozenModel):
    status: str
    schedule: list[ScheduleEntry] | None = None
    objective: int | None = None
    best_objective_bound: float | None = None
    instance_name: str = ""
    stats: dict[str, object] = Field(default_factory=dict)


def _data_path() -> Path:
    return Path(__file__).parent / "data" / (sys.argv[1] if len(sys.argv) > 1 else "data_mk01.json")


def _time_limit_seconds() -> float:
    return float(sys.argv[2]) if len(sys.argv) > 2 else 60.0


def _results_dir() -> Path | None:
    # Saving the result is OPT-IN. These scripts are meant to be run through the MCP
    # file tools against the user's own checkout, and a plain solve must not mutate
    # it -- so nothing is written unless this third argument names a directory (the
    # committed runs used `results`). A relative name resolves next to this script,
    # so the write target never depends on the caller's cwd; an absolute path is
    # taken as given.
    return (Path(__file__).parent / sys.argv[3]) if len(sys.argv) > 3 else None


def read_input(data_path: Path) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(data_path.read_text())
    return raw


def parse_input(raw: dict[str, Any]) -> ProblemInstance:
    jobs = [
        [
            TaskSpec(
                alternatives=[
                    Alternative(machine=machine, duration=duration)
                    for machine, duration in alternatives
                ]
            )
            for alternatives in job
        ]
        for job in raw["jobs"]
    ]
    return ProblemInstance(jobs=jobs, num_machines=raw["num_machines"])


def solve(instance: ProblemInstance, time_limit_seconds: float, instance_name: str) -> Solution:
    jobs = instance.jobs
    num_machines = instance.num_machines

    horizon = sum(
        max(alt.duration for alt in task_spec.alternatives) for job in jobs for task_spec in job
    )

    build_start = time.monotonic()
    model = cp_model.CpModel()

    Task = collections.namedtuple("Task", "start end")
    tasks: dict[tuple[int, int], Task] = {}
    # Per task, the assignment literal of each alternative -- empty when the task
    # has a single alternative and its machine is therefore forced.
    literals: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    # machine -> [(job_id, task_id, alt_id)] of every alternative landing there.
    machine_to_ops: dict[int, list[tuple[int, int, int]]] = collections.defaultdict(list)

    for job_id, job in enumerate(jobs):
        for task_id, task_spec in enumerate(job):
            alternatives = task_spec.alternatives
            suffix = f"_{job_id}_{task_id}"
            start = model.new_int_var(0, horizon, "start" + suffix)
            end = model.new_int_var(0, horizon, "end" + suffix)
            tasks[job_id, task_id] = Task(start, end)

            for alt_id, alt in enumerate(alternatives):
                machine_to_ops[alt.machine].append((job_id, task_id, alt_id))

            if len(alternatives) == 1:
                model.add(end == start + alternatives[0].duration)
                literals[job_id, task_id] = []
                continue

            task_literals = [
                model.new_bool_var(f"assign{suffix}_{alt_id}")
                for alt_id in range(len(alternatives))
            ]
            model.add_exactly_one(task_literals)
            # Duration is a linear function of the machine choice.
            model.add(
                end
                == start
                + sum(
                    alt.duration * task_literals[alt_id] for alt_id, alt in enumerate(alternatives)
                )
            )
            literals[job_id, task_id] = task_literals

    def assignment_literal(job_id: int, task_id: int, alt_id: int) -> cp_model.IntVar | None:
        """The literal that is true when this task takes this alternative, or None
        when the task has no choice to make."""
        task_literals = literals[job_id, task_id]
        return task_literals[alt_id] if task_literals else None

    # A machine can only work on one task at a time: pairwise reified disjunction,
    # conditional on both tasks having selected this machine.
    pairwise_literals = 0
    for machine in range(num_machines):
        for a, b in itertools.combinations(machine_to_ops[machine], 2):
            job_a, task_a, alt_a = a
            job_b, task_b, alt_b = b
            # Same job: the precedence chain already separates them.
            # Same task: `add_exactly_one` already forbids taking both alternatives.
            if job_a == job_b:
                continue
            guards = [
                literal
                for literal in (
                    assignment_literal(job_a, task_a, alt_a),
                    assignment_literal(job_b, task_b, alt_b),
                )
                if literal is not None
            ]
            before = model.new_bool_var(f"before_m{machine}_{pairwise_literals}")
            pairwise_literals += 1
            model.add(tasks[job_b, task_b].start >= tasks[job_a, task_a].end).only_enforce_if(
                [*guards, before]
            )
            model.add(tasks[job_a, task_a].start >= tasks[job_b, task_b].end).only_enforce_if(
                [*guards, before.Not()]
            )

    # Tasks within a job run in the given order.
    for job_id, job in enumerate(jobs):
        for task_id in range(len(job) - 1):
            model.add(tasks[job_id, task_id + 1].start >= tasks[job_id, task_id].end)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(
        makespan, [tasks[job_id, len(job) - 1].end for job_id, job in enumerate(jobs)]
    )
    model.minimize(makespan)

    def greedy_schedule() -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
        """Earliest-completion-time list scheduling, used as a warm-start hint.

        Unlike the fixed-machine version in ../job_shop, this must also CHOOSE a
        machine: for every job's next unscheduled task it evaluates each
        alternative and dispatches the (task, alternative) pair that would finish
        earliest. Returns the chosen starts and the chosen alternative indices.
        """
        num_jobs = len(jobs)
        job_next_task = [0] * num_jobs
        job_ready = [0] * num_jobs
        machine_ready = [0] * num_machines
        starts: dict[tuple[int, int], int] = {}
        choices: dict[tuple[int, int], int] = {}
        remaining = sum(len(job) for job in jobs)

        while remaining:
            best = None
            for job_id in range(num_jobs):
                task_id = job_next_task[job_id]
                if task_id >= len(jobs[job_id]):
                    continue
                for alt_id, alt in enumerate(jobs[job_id][task_id].alternatives):
                    start = max(job_ready[job_id], machine_ready[alt.machine])
                    completion = start + alt.duration
                    if best is None or completion < best[0]:
                        best = (completion, job_id, task_id, alt_id, alt.machine, start)
            assert best is not None
            _completion, job_id, task_id, alt_id, machine, start = best
            duration = jobs[job_id][task_id].alternatives[alt_id].duration
            starts[job_id, task_id] = start
            choices[job_id, task_id] = alt_id
            job_ready[job_id] = start + duration
            machine_ready[machine] = start + duration
            job_next_task[job_id] += 1
            remaining -= 1

        return starts, choices

    greedy_starts, greedy_choices = greedy_schedule()
    for key, task_vars in tasks.items():
        model.add_hint(task_vars.start, greedy_starts[key])
    for key, task_literals in literals.items():
        for alt_id, literal in enumerate(task_literals):
            model.add_hint(literal, alt_id == greedy_choices[key])
    build_seconds = time.monotonic() - build_start

    solver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = time_limit_seconds
    status = solver.solve(model)

    status_map = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }

    def chosen_alternative(job_id: int, task_id: int) -> Alternative:
        """The alternative the solver selected for one task."""
        alternatives = jobs[job_id][task_id].alternatives
        task_literals = literals[job_id, task_id]
        if not task_literals:
            return alternatives[0]
        for alt_id, literal in enumerate(task_literals):
            if solver.boolean_value(literal):
                return alternatives[alt_id]
        raise AssertionError(f"no alternative selected for job {job_id} task {task_id}")

    schedule = None
    objective = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        schedule = []
        for job_id, job in enumerate(jobs):
            for task_id in range(len(job)):
                alt = chosen_alternative(job_id, task_id)
                schedule.append(
                    ScheduleEntry(
                        job=job_id,
                        task=task_id,
                        machine=alt.machine,
                        start=solver.value(tasks[job_id, task_id].start),
                        duration=alt.duration,
                        end=solver.value(tasks[job_id, task_id].end),
                    )
                )
        objective = solver.value(makespan)

    stats: dict[str, object] = {
        "formulation": "pairwise_disjunctive",
        "instance": instance_name,
        "time_limit": time_limit_seconds,
        "build_seconds": round(build_seconds, 3),
        "wall_time": round(solver.wall_time, 3),
        "pairwise_before_literals": pairwise_literals,
        "num_booleans": solver.num_booleans,
        "num_conflicts": solver.num_conflicts,
        "num_branches": solver.num_branches,
        "model_variables": len(model.proto.variables),
        "model_constraints": len(model.proto.constraints),
    }

    bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)
    best_objective_bound = float(solver.best_objective_bound) if status in bound_states else None

    return Solution(
        status=status_map.get(status, "error"),
        schedule=schedule,
        objective=objective,
        best_objective_bound=best_objective_bound,
        instance_name=instance_name,
        stats=stats,
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    # The printed `solution` must CONTAIN the schedule, not describe it: the
    # checked MCP tools build the checker's payload from this stdout object, so a
    # summary that merely points at a saved file leaves the checker with nothing
    # to grade and it reports an ungradeable payload. The cost is real -- a
    # 500-task behnke solution is ~40 KB of tool response -- and it is the price
    # of an in-band verification pass. It carries no path to the saved file
    # either: the name is derivable from the formulation and instance already in
    # `stats`, and an absolute path would bake this machine's filesystem into
    # every committed artifact under results/.
    payload_solution: dict[str, Any] = {}
    if solution.schedule is not None:
        payload_solution = {
            "makespan": solution.objective,
            "schedule": [entry.model_dump() for entry in solution.schedule],
            "instance": solution.instance_name,
            "num_tasks": len(solution.schedule),
        }
    return {
        "status": solution.status,
        "objective": solution.objective,
        "solution": payload_solution,
        "best_objective_bound": solution.best_objective_bound,
        "stats": solution.stats,
    }


def write_output(
    payload: dict[str, Any],
    results_dir: Path | None,
    data_path: Path,
    formulation: str = "pairwise_disjunctive",
) -> None:
    if results_dir is not None:
        result_path = results_dir / f"{formulation}__{data_path.stem}.json"
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(payload), encoding="utf-8")
    # The result is ALWAYS printed verbatim, and written to a file only when the
    # caller opted in.
    print(json.dumps(payload))


def main() -> None:
    data_path = _data_path()
    solution = solve(parse_input(read_input(data_path)), _time_limit_seconds(), data_path.name)
    write_output(serialize_solution(solution), _results_dir(), data_path)


if __name__ == "__main__":
    main()
