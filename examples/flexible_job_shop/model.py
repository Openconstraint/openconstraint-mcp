"""Reference CP-SAT script: flexible job shop scheduling (optional intervals).

Loads a flexible job shop data file (default: data_mk01.json, the 10x6
Brandimarte instance whose proven optimum is 40) and minimizes makespan.

Formulation: the canonical OR-Tools encoding. Every task gets a start, an end
and a duration variable. Every *alternative* gets a presence literal and an
OPTIONAL interval whose start/end/duration are pinned to the task's own
variables while that literal is true; `add_exactly_one` over the literals
picks one machine. Each machine then gets a single `add_no_overlap` over the
optional intervals that could land on it, so the machine-exclusion constraint
is enforced by CP-SAT's disjunctive propagator and is automatically inactive
for alternatives that were not selected.

A task with exactly one alternative skips the optional machinery entirely and
uses a plain fixed-duration interval: there is no choice to model, and a
non-optional interval is cheaper to propagate. On mk01 and mk15 that covers
29% and 23% of tasks respectively; on the behnke instance it covers none.

Measured (single worker, seed 42, 600s cap; raw runs in results/ -- mk01
current, mk15 and behnke predating a later stdout change that added
num_tasks, kept rather than re-solved because no change since touched the
model itself):
- mk01: optimal 40 in 0.1s.
- mk15: best 347, bound 333. The bound REACHES the known optimum of 333, so
  the shortfall is finding the matching schedule, not proving it -- which is
  what num_workers=1 costs, since it drops the LNS workers that specialise in
  improving incumbents. Best incumbent of the three formulations.
- behnke lar04_1: best 504, bound 77. That bound is exactly the trivial
  max-job-min-length value, i.e. `no_overlap` contributed nothing globally
  across 60 machines. And 504 is 18% WORSE than a greedy dispatching
  heuristic's 427, so at this scale this model is not worth its runtime.

Run from the repository root:
    uv run examples/flexible_job_shop/model.py data_mk01.json 10
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

    # Serializing every task at its slowest alternative is a valid upper bound.
    horizon = sum(
        max(alt.duration for alt in task_spec.alternatives) for job in jobs for task_spec in job
    )

    build_start = time.monotonic()
    model = cp_model.CpModel()

    Task = collections.namedtuple("Task", "start end duration")
    tasks: dict[tuple[int, int], Task] = {}
    machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)
    # Per task, the presence literal of each alternative -- empty for a task with a
    # single alternative, whose machine is not a decision.
    presences: dict[tuple[int, int], list[cp_model.IntVar]] = {}

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
                # Pin the chosen alternative to the task's own timing.
                model.add(alt_start == start).only_enforce_if(literal)
                model.add(alt_end == end).only_enforce_if(literal)
                model.add(duration == alt.duration).only_enforce_if(literal)
                machine_to_intervals[alt.machine].append(alt_interval)
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
        "formulation": "optional_intervals",
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
    formulation: str = "optional_intervals",
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
