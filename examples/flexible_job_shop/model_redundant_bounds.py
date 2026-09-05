"""Reference CP-SAT script: flexible job shop with redundant global bounds.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. The core encoding is IDENTICAL to model.py -- optional intervals per
alternative, `add_exactly_one` on the presence literals, `add_no_overlap` per
machine -- so the only difference between the two files is the block of
redundant constraints marked below.

Three implied-but-not-propagated facts are stated explicitly:

1. MACHINE LOAD. All work assigned to one machine runs sequentially inside
   [0, makespan], so `sum(d_a * lit_a for alternatives on m) <= makespan`.
2. JOB LENGTH. A job's tasks run in sequence, each taking at least its
   cheapest alternative, so `makespan >= sum(min duration)` over the job.
3. GLOBAL ENERGY. At most `num_machines` tasks can run at once, expressed as
   `add_cumulative(all task intervals, demands=1, capacity=num_machines)`.

None of these change the feasible set: every one is a consequence of
constraints model.py already has. They are here because CP-SAT's disjunctive
propagator reasons about machine exclusion COMBINATORIALLY, while (1) and (2)
are linear and feed the LP relaxation directly -- and on a minimization
problem it is usually the lower bound, not finding good solutions, that is
slow.

HONEST CAVEAT: this is a bundle of three changes, not an ablation. Its results
show that redundant bounds help on this instance family, but not which of the
three carried it. Separating them would need three more variants and three
times the benchmark budget.

Measured (single worker, seed 42, 600s cap; raw runs in results/ -- mk01
current, mk15 and behnke predating a later stdout change that added
num_tasks, kept rather than re-solved because no change since touched the
model itself): whether this pays off depends entirely on scale.
- mk01: optimal 40 in 0.1s.
- mk15: best 363 against model.py's 347, bound 332 against 333 -- no gain and
  a small loss. Conflicts fell 4x (141k vs 588k) at equal bound quality, which
  says each node simply became more expensive: the add_cumulative over 284
  variable-duration intervals cost more propagation time than its reasoning
  returned. The textbook trick loses here.
- behnke lar04_1: bound 344 against model.py's 77 -- a 4.5x improvement, and
  the only nontrivial lower bound any of the three produced at this scale. The
  incumbent is simultaneously the worst of the three (624, versus a greedy
  heuristic's 427), because the added propagation crowds out search.

So the machine-load inequality buys BOUNDS, not SOLUTIONS -- and at 60
machines that trade is worth making, while at 15 it is not.

On the 344: the FJSPLib catalog lists 103 as this instance's best known lower
bound. All three constraints here are provably implied by FJSP, and this
model's bounds are sound wherever ground truth exists (mk01: 344's counterpart
is 40, exactly the optimum; mk15: 332 <= the true 333). Treat 344 as a
legitimate bound from this run, NOT as a claimed improvement on the
literature: that would require multi-seed replication, an independent solver,
and confirmation that the catalog figure is current.

Run from the repository root:
    uv run examples/flexible_job_shop/model_redundant_bounds.py data_mk01.json 10
"""

import collections
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

    Task = collections.namedtuple("Task", "start end duration interval")
    tasks: dict[tuple[int, int], Task] = {}
    machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)
    presences: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    # machine -> linear terms for its total assigned processing time. A forced
    # alternative contributes a plain int; a chosen one contributes d * literal.
    machine_load_terms: dict[int, list[cp_model.LinearExprT]] = collections.defaultdict(list)

    for job_id, job in enumerate(jobs):
        for task_id, task_spec in enumerate(job):
            alternatives = task_spec.alternatives
            suffix = f"_{job_id}_{task_id}"
            durations = [alt.duration for alt in alternatives]
            start = model.new_int_var(0, horizon, "start" + suffix)
            end = model.new_int_var(0, horizon, "end" + suffix)

            if len(alternatives) == 1:
                machine, fixed_duration = alternatives[0].machine, alternatives[0].duration
                interval = model.new_interval_var(start, fixed_duration, end, "interval" + suffix)
                machine_to_intervals[machine].append(interval)
                machine_load_terms[machine].append(fixed_duration)
                tasks[job_id, task_id] = Task(
                    start, end, model.new_constant(fixed_duration), interval
                )
                presences[job_id, task_id] = []
                continue

            duration = model.new_int_var(min(durations), max(durations), "duration" + suffix)
            interval = model.new_interval_var(start, duration, end, "interval" + suffix)

            literals = []
            for alt_id, alt in enumerate(alternatives):
                alt_suffix = f"{suffix}_{alt_id}"
                literal = model.new_bool_var("presence" + alt_suffix)
                alt_start = model.new_int_var(0, horizon, "alt_start" + alt_suffix)
                alt_end = model.new_int_var(0, horizon, "alt_end" + alt_suffix)
                alt_interval = model.new_optional_interval_var(
                    alt_start, alt.duration, alt_end, literal, "alt_interval" + alt_suffix
                )
                model.add(alt_start == start).only_enforce_if(literal)
                model.add(alt_end == end).only_enforce_if(literal)
                model.add(duration == alt.duration).only_enforce_if(literal)
                machine_to_intervals[alt.machine].append(alt_interval)
                machine_load_terms[alt.machine].append(alt.duration * literal)
                literals.append(literal)

            model.add_exactly_one(literals)
            tasks[job_id, task_id] = Task(start, end, duration, interval)
            presences[job_id, task_id] = literals

    # A machine can only work on one task at a time.
    for machine in range(num_machines):
        model.add_no_overlap(machine_to_intervals[machine])

    # Tasks within a job run in the given order.
    for job_id, job in enumerate(jobs):
        for task_id in range(len(job) - 1):
            model.add(tasks[job_id, task_id + 1].start >= tasks[job_id, task_id].end)

    makespan = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(
        makespan, [tasks[job_id, len(job) - 1].end for job_id, job in enumerate(jobs)]
    )

    # --- redundant constraints: the ONLY difference from model.py ------------------

    # (1) No machine can be busy longer than the makespan.
    for machine in range(num_machines):
        terms = machine_load_terms[machine]
        if terms:
            model.add(sum(terms) <= makespan)

    # (2) A job cannot finish faster than its cheapest route through its tasks.
    for job in jobs:
        model.add(
            makespan
            >= sum(min(alt.duration for alt in task_spec.alternatives) for task_spec in job)
        )

    # (3) At most num_machines tasks can be in progress simultaneously.
    model.add_cumulative(
        [task.interval for task in tasks.values()],
        [1] * len(tasks),
        num_machines,
    )

    # --- end redundant constraints -------------------------------------------------

    model.minimize(makespan)
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
        literals = presences[job_id, task_id]
        if not literals:
            return alternatives[0]
        for alt_id, literal in enumerate(literals):
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
        "formulation": "redundant_bounds",
        "instance": instance_name,
        "time_limit": time_limit_seconds,
        "build_seconds": round(build_seconds, 3),
        "wall_time": round(solver.wall_time, 3),
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
    formulation: str = "redundant_bounds",
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
