"""Reference CP-SAT script: flexible job shop, earliest-start branching.

Loads a flexible job shop data file (default: data_mk01.json) and minimizes
makespan. This is model_direct_optional_intervals.py with EXACTLY ONE change --
two `add_decision_strategy` calls -- so any difference between the two files'
results is attributable to the search order alone. Every model constraint, the
machine-load inequality, the greedy warm start and all solver parameters are
inherited verbatim.

THE CHANGE. The other files supply CP-SAT with a starting POINT (the greedy
`add_hint`) but never with a starting ORDER. This one tells the solver which
task to decide next and what to decide about it: repeatedly branch on the task
whose start can still happen earliest, and assign it exactly that earliest
start.

    add_decision_strategy(starts, CHOOSE_LOWEST_MIN, SELECT_MIN_VALUE)

CHOOSE_LOWEST_MIN picks the variable with the smallest remaining lower bound
(the task that could still go first); SELECT_MIN_VALUE then assigns that lower
bound (start it as early as allowed). In scheduling terms that is the textbook
non-delay dispatching rule, known in the literature as a serial schedule
generation scheme -- but the file is named for what the two constants DO, not
for the paper. It is a good FIRST DIVE for a makespan objective: every decision
left-shifts a task, so the dive bottoms out in a complete, tight schedule
instead of wandering. The second strategy (ends
and makespan, CHOOSE_FIRST/SELECT_MIN_VALUE) is bookkeeping -- it keeps the
union of strategies total, as `add_decision_strategy` requires, and never
actually branches, because propagation fixes an end as soon as its start and
presence literal are known.

WHAT IS DELIBERATELY *NOT* CHANGED: `solver.parameters.search_branching`. It
stays at the default AUTOMATIC_SEARCH, which per sat_parameters.proto fixes
literals with the SAT solver's own heuristics and then branches on integer
variables "using the fixed search specified by the user OR OUR DEFAULT ONE". So
this file replaces exactly one component -- the integer branching heuristic --
and leaves the clause learning, LP relaxation and pseudo-cost machinery in
charge of everything else. Two stronger settings were measured and both LOST:

    mk15, 60 s, single worker, seed 42, best incumbent (bound was 332 in all):

    configuration                                     makespan   conflicts
    no decision strategy (model_direct_optional...)        360       8,476
    this file (strategy + AUTOMATIC_SEARCH)                355       8,625
    strategy + PARTIAL_FIXED_SEARCH                        376      10,302
    strategy + FIXED_SEARCH                                570      60,852
    strategy + FIXED_SEARCH, greedy hint removed           615      63,031

FIXED_SEARCH is the instructive failure. Handing the solver a complete branching
order sounds like more guidance, but it switches OFF the LP- and pseudo-cost-
guided branching that is what actually drives the makespan down; the search then
spends its budget proving small things about a bad dive. At 60 s the lower bound
sat at 332 in every configuration above, which locates the effect: a decision
strategy is primarily a primal heuristic.

NO STRATEGY OVER THE PRESENCE LITERALS, for the same reason. Branching machine
choice first is the natural way to write this, but under AUTOMATIC_SEARCH the SAT
solver owns the literals and a user strategy over them never fires. That is
measured, not assumed: a variant adding
`add_decision_strategy(literals, CHOOSE_FIRST, SELECT_MAX_VALUE)` ahead of the
starts returned an identical 355 on the same mk15 run. Dead code, so it is not
here. It would start mattering under FIXED_SEARCH -- which is exactly the
configuration the table above rules out.

This file adds no variables and no constraints: mk01 stays at 210 / 212, the
same as model_direct_optional_intervals.py. A decision strategy changes only the
order decisions are taken in.

AT A LONGER BUDGET THE GAIN WIDENS RATHER THAN WASHING OUT. Both files rerun on
mk15 at 1200 s, submitted concurrently so they saw identical machine load:

    file                            makespan   bound   conflicts
    model_direct_optional_intervals      345     332      528,687
    this file                            339     333      468,087

So the head-start reading is wrong: 360 -> 355 at 60 s became 345 -> 339 at
1200 s. The 339 schedule was verified `accepted` by checker.py. Note the bound
column too: `best_objective_bound` is a LOWER bound, so this file proved "no
schedule beats 333" while the baseline proved only "no schedule beats 332" --
a strictly stronger statement from the same budget. That is why the claim above
is "primarily" a primal heuristic and not "only": a better incumbent found
sooner also prunes, and here it was worth the last unit of bound.

NEITHER RUN PROVED AN OPTIMUM, and the status field says so -- both report
`feasible`, not `optimal`. Proving optimality needs the two bounds to MEET: a
333 schedule in hand alongside the 333 bound. This run has a 339 schedule, so
what it established is 333 <= optimum <= 339 and nothing narrower. That the
optimum is exactly 333 is FJSPLib's result (recorded in problem.txt and in the
instance's `known_optimal_makespan`), not this run's. Closing the remaining 6
units is primal work -- constructing a better schedule -- not bound work.

OPEN QUESTION still outstanding: behnke, and replication. Every number here is
ONE run per configuration. These scripts are capped by WALL CLOCK, not
`max_deterministic_time`, so `random_seed = 42` fixes the search but not how much
of it fits in the budget -- a rerun under different machine load can land
elsewhere. Two paired runs agreeing in the same direction is encouraging, not a
confidence interval.

Run from the repository root:
    uv run examples/flexible_job_shop/model_earliest_start_branching.py data_mk01.json 10
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
    machine_to_intervals: dict[int, list[cp_model.IntervalVar]] = collections.defaultdict(list)
    presences: dict[tuple[int, int], list[cp_model.IntVar]] = {}
    machine_load_terms: dict[int, list[cp_model.LinearExprT]] = collections.defaultdict(list)

    # NOTE: this loop's variable-creation order is load-bearing. The two
    # `add_decision_strategy` calls below read variable-creation order, not just
    # the final variable set -- so this loop (and every variable it creates) must
    # stay in exactly this sequence, unchanged from model_direct_optional_intervals.py.
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

    # THE CHANGE. Branch on the task whose start can still happen earliest, and give
    # it that earliest start -- a serial schedule generation scheme expressed as a
    # CP-SAT decision strategy. The trailing stage on the ends and the makespan only
    # keeps the strategy total; propagation fixes them once a start and its presence
    # literal are known.
    model.add_decision_strategy(
        [task_vars.start for task_vars in tasks.values()],
        cp_model.CHOOSE_LOWEST_MIN,
        cp_model.SELECT_MIN_VALUE,
    )
    model.add_decision_strategy(
        [task_vars.end for task_vars in tasks.values()] + [makespan],
        cp_model.CHOOSE_FIRST,
        cp_model.SELECT_MIN_VALUE,
    )
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
        "formulation": "earliest_start_branching",
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
    formulation: str = "earliest_start_branching",
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
