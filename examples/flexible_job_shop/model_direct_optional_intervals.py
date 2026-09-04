"""Reference CP-SAT script: flexible job shop, lean optional-interval encoding.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. This is model_composite.py with EXACTLY ONE change -- how an
alternative's optional interval is attached -- so any difference between the two
files' results is attributable to the encoding alone.

THE CHANGE. model.py, model_redundant_bounds.py and model_composite.py all
follow the canonical OR-Tools `flexible_jobshop_sat.py` pattern: per alternative
they create a private `alt_start`/`alt_end` pair, wrap them in an optional
interval, and channel them back to the task's own variables:

    alt_start = new_int_var(...); alt_end = new_int_var(...)
    new_optional_interval_var(alt_start, d, alt_end, lit)
    add(alt_start == start).only_enforce_if(lit)
    add(alt_end   == end  ).only_enforce_if(lit)
    add(duration  == d    ).only_enforce_if(lit)

That costs 2 integer variables and 3 enforced constraints per alternative, plus
a main interval and a main duration variable per task. None of it is necessary
for pure FJSP: `add_exactly_one` already guarantees exactly one alternative is
present, so the optional interval can hang directly on the task's OWN start and
end. The present one enforces `end == start + its size`; the absent ones enforce
nothing and stay invisible to `add_no_overlap`.

    new_optional_interval_var(start, d, end, lit)

Measured model sizes (pre-presolve, `len(model.proto.{variables,constraints})`;
"canonical" is model_composite.py, the file this one was forked from, so the
encoding is the only difference between the columns):

    instance      canonical                direct                saved
    mk01            450 vars /   548 con     210 /   212     -53% / -61%
    mk15          3,183     / 3,972        1,365 / 1,365     -57% / -66%
    behnke       29,281     / 38,561      10,261 / 10,281    -65% / -73%

WHY THE CANONICAL FORM EXISTS ANYWAY: the private copies matter when
alternatives need different timing semantics -- machine-dependent setup times,
transfer lags, per-machine calendars -- because then the alternative's interval
is genuinely not the task's interval. problem.txt defers every one of those
extensions, so here the copies are pure overhead. Reintroduce them the moment
setup times arrive.

OPEN QUESTION this file is meant to answer: CP-SAT's presolve may already
collapse the channeling variables, in which case a 3x smaller INPUT model buys
nothing at search time. A smaller model is not automatically a faster one, and
the pre-presolve numbers above deliberately prove nothing about runtime.

Everything else is inherited unchanged from model_composite.py: the machine-load
inequality (the redundant constraint that measurably drove the bound) and the
greedy earliest-completion-time warm start. The global `add_cumulative` is
absent here for the same reason it is absent there -- it was measured to cost
far more than it returned.

Run from the repository root:
    uv run examples/flexible_job_shop/model_direct_optional_intervals.py data_mk01.json 10
"""

import collections
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

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
    return Path(__file__).parent / (sys.argv[1] if len(sys.argv) > 1 else "data_mk01.json")


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
    return cast(dict[str, Any], json.loads(data_path.read_text()))


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
    machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)
    presences: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    machine_load_terms: dict[int, list[cp_model.LinearExprT]] = collections.defaultdict(list)

    for job_id, job in enumerate(jobs):
        for task_id, task_spec in enumerate(job):
            alternatives = task_spec.alternatives
            suffix = f"_{job_id}_{task_id}"
            start = model.new_int_var(0, horizon, "start" + suffix)
            end = model.new_int_var(0, horizon, "end" + suffix)

            if len(alternatives) == 1:
                machine, fixed_duration = alternatives[0].machine, alternatives[0].duration
                interval = model.new_interval_var(start, fixed_duration, end, "interval" + suffix)
                machine_to_intervals[machine].append(interval)
                machine_load_terms[machine].append(fixed_duration)
                tasks[job_id, task_id] = Task(start, end)
                presences[job_id, task_id] = []
                continue

            literals = []
            for alt_id, alt in enumerate(alternatives):
                alt_suffix = f"{suffix}_{alt_id}"
                isPresented = model.new_bool_var("presence" + alt_suffix)
                # The direct form: the optional interval IS the task's interval while
                # this alternative is present, so it needs no private copies and no
                # channeling constraints. When absent it enforces nothing.
                alt_interval = model.new_optional_interval_var(
                    start, alt.duration, end, isPresented, "alt_interval" + alt_suffix
                )
                machine_to_intervals[alt.machine].append(alt_interval)
                machine_load_terms[alt.machine].append(alt.duration * isPresented)
                literals.append(isPresented)

            model.add_exactly_one(literals)
            tasks[job_id, task_id] = Task(start, end)
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

    # Inherited from model_composite.py: no machine can be busy longer than the
    # makespan. Linear, so it reaches the LP relaxation that the disjunctive
    # propagator cannot.
    for machine in range(num_machines):
        terms = machine_load_terms[machine]
        if terms:
            model.add(sum(terms) <= makespan)

    model.minimize(makespan)

    def greedy_schedule() -> tuple[dict[tuple[int, int], int], dict[tuple[int, int], int]]:
        """Earliest-completion-time list scheduling, used as a warm-start hint.

        Identical to the routines in model_pairwise_disjunctive.py and
        model_composite.py, so the hint is the same wherever it is used.
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
        for alt_id, isPresented in enumerate(task_literals):
            model.add_hint(isPresented, alt_id == greedy_choices[key])
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
        for alt_id, isPresented in enumerate(literals):
            if solver.boolean_value(isPresented):
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
        "formulation": "direct_optional_intervals",
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
    formulation: str = "direct_optional_intervals",
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
