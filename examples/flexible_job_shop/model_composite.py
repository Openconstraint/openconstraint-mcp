"""Reference CP-SAT script: flexible job shop, composite of what actually won.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. This file exists because the three-way comparison between model.py,
model_pairwise_disjunctive.py and model_redundant_bounds.py produced three
findings that each came from a DIFFERENT file, so no single model held all of
them:

1. model.py's optional-interval encoding gave the best incumbents and by far
   the cheapest model (14k booleans on mk15 against the pairwise encoding's
   73k). Kept as the base.
2. model_redundant_bounds.py was the only formulation to produce a nontrivial
   lower bound at 60 machines: 344 against model.py's 77. Its MACHINE-LOAD
   inequality is kept.
3. model_pairwise_disjunctive.py's greedy warm start turned out to be the
   single largest lever in the whole experiment -- on the behnke instance the
   greedy schedule alone (427) beat model.py's 600s search (504) and
   model_redundant_bounds.py's (624). Kept.

So: optional intervals + machine-load bound + greedy warm start.

Deliberately NOT carried over from model_redundant_bounds.py:
- the global `add_cumulative` over all task intervals, suspected of costing
  more propagation time than its reasoning returned (that file explored only
  2,703 conflicts on behnke against model.py's 626,912); and
- the per-job `makespan >= sum(min duration)` bound, which is redundant with
  what the base encoding already derives on its own -- model.py's behnke bound
  of 77 was EXACTLY the max-job-min-length value.

That omission makes this file a partial ablation as well as a composite: if the
behnke bound still lands near 344, the machine-load inequality was what carried
model_redundant_bounds.py's bound win, and the cumulative was dead weight.

Measured (single worker, seed 42, 600s cap; raw runs in results/ -- mk01
current, mk15 and behnke predating a later stdout change that added
num_tasks, kept rather than re-solved because no change since touched the
model itself): the ablation answered YES, and the composite is the only run
to improve on its own warm start.
- mk01: optimal 40 in 0.1s.
- mk15: best 349, bound 332 -- between model.py's 347/333 and
  model_redundant_bounds.py's 363/332. At 15 machines the machine-load bound
  still buys nothing, so this is model.py plus overhead.
- behnke lar04_1: best 418, bound 344. The bound MATCHES
  model_redundant_bounds.py's 344 with the add_cumulative removed, so the
  machine-load inequality carried that file's entire bound win and the
  cumulative was dead weight -- which is exactly what this file was built to
  test. The incumbent is the headline: 418 beats the greedy warm start it was
  handed (427), where model_pairwise_disjunctive.py merely tied it and the two
  formulations without the load bound finished at 504 and 624. Combining the
  cheap encoding, the load bound and the warm start is what made search
  productive at 60 machines.

Run from the repository root:
    uv run examples/flexible_job_shop/model_composite.py data_mk01.json 10
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

    Task = collections.namedtuple("Task", "start end duration")
    tasks: dict[tuple[int, int], Task] = {}
    machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)
    presences: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    # machine -> linear terms for its total assigned processing time.
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
                tasks[job_id, task_id] = Task(start, end, model.new_constant(fixed_duration))
                presences[job_id, task_id] = []
                continue

            duration = model.new_int_var(min(durations), max(durations), "duration" + suffix)
            model.new_interval_var(start, duration, end, "interval" + suffix)

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
            tasks[job_id, task_id] = Task(start, end, duration)
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

    # Kept from model_redundant_bounds.py: no machine can be busy longer than the
    # makespan. Linear, so it feeds the LP relaxation the disjunctive propagator
    # cannot reach.
    for machine in range(num_machines):
        terms = machine_load_terms[machine]
        if terms:
            model.add(sum(terms) <= makespan)

    model.minimize(makespan)

    def greedy_schedule() -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
        """Earliest-completion-time list scheduling, used as a warm-start hint.

        Identical to the routine in model_pairwise_disjunctive.py so the hint is
        the same across the two files that use it: for every job's next unscheduled
        task it evaluates each alternative and dispatches the (task, alternative)
        pair that would finish earliest, choosing the machine as well as the time.
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
    for key, task_literals in presences.items():
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
        "formulation": "composite",
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
    formulation: str = "composite",
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
