"""Reference CP-SAT script: simple assembly line balancing, type 1.

Assign every task of one product to a station of a paced line. Each task has an
integer processing time; the cycle time ct is fixed, so the tasks on one station
may take at most ct in total. Tasks are ordered by precedence: a task may not
sit on an earlier station than any of its predecessors. Minimize the number of
stations m.

The CP model is the one of Bukchin & Raviv, "Constraint programming for solving
various assembly line balancing problems", Omega 78 (2018) 57-68, Section 2
(pp. 59-60): one integer variable x_i per task holds its station number, and m
is minimized directly. Three formulations of that model are selectable, so each
tightening can be measured against the one before it:

- "base":   (1)-(6) -- capacity (2), terminal tasks within m (3), and direct
            precedence x_i <= x_j (4).
- "bounds": base plus the station window E_i <= x_i <= m - L_i (7)-(9).
- "full":   bounds with (4) replaced by the pairwise distances
            x_i + D_ij <= x_j (10)/(4') over all transitive predecessor pairs,
            minus the pairs implied through an intermediate task (4'').

Loads a JSON instance from parsed/ (default: five_task_line.json) and prints one
JSON result. Optional arguments: the formulation (default "full") and a CP-SAT
time limit in seconds; without a limit the search runs until it proves
optimality.
Run from the repository root:
    uv run examples/assembly_line_balancing/model.py five_task_line.json
    uv run examples/assembly_line_balancing/model.py JACKSON_c10.json base 10
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal

from ortools.sat.python import cp_model
from pydantic import BaseModel, ConfigDict

Formulation = Literal["base", "bounds", "full"]
FORMULATIONS: tuple[Formulation, ...] = ("base", "bounds", "full")


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class Task(FrozenModel):
    id: int
    time: int


class ProblemInstance(FrozenModel):
    cycle_time: int
    tasks: list[Task]
    # (before, after): task `before` is a direct predecessor of task `after`.
    precedences: list[tuple[int, int]]


class Solution(FrozenModel):
    status: str
    objective: int | None = None
    best_objective_bound: float | None = None
    # stations[j - 1] lists the task ids on station j, for j = 1..m.
    stations: list[list[int]] | None = None


class Precomputed(FrozenModel):
    """The instance-only quantities the tightened formulations use.

    Every list is indexed by task id (1..n); index 0 is an unused placeholder,
    so no lookup needs a `- 1`.
    """

    all_predecessors: list[frozenset[int]]  # P~_i
    all_successors: list[frozenset[int]]  # S~_i
    earliest_station: list[int]  # E_i (7)
    stations_after: list[int]  # L_i (8)


def _formulation() -> Formulation:
    value: str = sys.argv[2] if len(sys.argv) > 2 else "full"
    for formulation in FORMULATIONS:
        if formulation == value:
            return formulation
    raise SystemExit(f"formulation must be one of {', '.join(FORMULATIONS)}, got {value!r}")


def _time_limit_seconds() -> float | None:
    return float(sys.argv[3]) if len(sys.argv) > 3 else None


def read_input() -> dict[str, Any]:
    filename: str = sys.argv[1] if len(sys.argv) > 1 else "five_task_line.json"
    data_path: Path = Path(__file__).parent / "parsed" / filename
    raw: dict[str, Any] = json.loads(data_path.read_text(encoding="utf-8"))
    return raw


def parse_input(raw: dict[str, Any]) -> ProblemInstance:
    tasks: list[Task] = [Task(id=item["id"], time=item["time"]) for item in raw["tasks"]]
    ids: list[int] = [task.id for task in tasks]
    if ids != list(range(1, len(tasks) + 1)):
        raise ValueError("task ids must be 1..n in order")
    precedences: list[tuple[int, int]] = [(pair[0], pair[1]) for pair in raw["precedences"]]
    for before, after in precedences:
        if not (1 <= before <= len(tasks) and 1 <= after <= len(tasks)) or before == after:
            raise ValueError(f"precedence {before}->{after} does not join two distinct known tasks")
    return ProblemInstance(cycle_time=raw["cycle_time"], tasks=tasks, precedences=precedences)


def task_times(instance: ProblemInstance) -> list[int]:
    """Processing times indexed by task id; index 0 is a placeholder 0."""
    return [0] + [task.time for task in instance.tasks]


def topological_order(instance: ProblemInstance) -> list[int]:
    """Task ids in an order where every predecessor comes first."""
    num_tasks: int = len(instance.tasks)
    direct_successors: list[list[int]] = [[] for _ in range(num_tasks + 1)]
    unplaced_predecessors: list[int] = [0] * (num_tasks + 1)
    for before, after in instance.precedences:
        direct_successors[before].append(after)
        unplaced_predecessors[after] += 1
    order: list[int] = [
        task for task in range(1, num_tasks + 1) if unplaced_predecessors[task] == 0
    ]
    for task in order:  # grows while iterating: a queue without the import
        for successor in direct_successors[task]:
            unplaced_predecessors[successor] -= 1
            if unplaced_predecessors[successor] == 0:
                order.append(successor)
    if len(order) != num_tasks:
        raise ValueError("the precedence graph has a cycle")
    return order


def precompute(instance: ProblemInstance) -> Precomputed:
    """Transitive predecessor/successor sets and the bounds (7) and (8).

    E_i = ceil((t_i + sum of all predecessors' times) / ct): those tasks fill the
    first x_i stations. L_i = floor((t_i - 1 + sum of all successors' times) / ct)
    is the number of stations that must follow x_i; the -1 keeps a total that is
    an exact multiple of ct from counting one station too many (p. 59).
    """
    num_tasks: int = len(instance.tasks)
    times: list[int] = task_times(instance)
    cycle_time: int = instance.cycle_time
    direct_predecessors: list[list[int]] = [[] for _ in range(num_tasks + 1)]
    for before, after in instance.precedences:
        direct_predecessors[after].append(before)

    predecessors: list[set[int]] = [set() for _ in range(num_tasks + 1)]
    for task in topological_order(instance):
        for predecessor in direct_predecessors[task]:
            predecessors[task] |= predecessors[predecessor] | {predecessor}
    successors: list[set[int]] = [set() for _ in range(num_tasks + 1)]
    for task in range(1, num_tasks + 1):
        for predecessor in predecessors[task]:
            successors[predecessor].add(task)

    task_ids: range = range(1, num_tasks + 1)
    earliest_station: list[int] = [0] + [
        -(-(times[task] + sum(times[other] for other in predecessors[task])) // cycle_time)
        for task in task_ids
    ]
    stations_after: list[int] = [0] + [
        (times[task] - 1 + sum(times[other] for other in successors[task])) // cycle_time
        for task in task_ids
    ]
    return Precomputed(
        all_predecessors=[frozenset(item) for item in predecessors],
        all_successors=[frozenset(item) for item in successors],
        earliest_station=earliest_station,
        stations_after=stations_after,
    )


def greedy_station_count(instance: ProblemInstance) -> int:
    """Stations used by a station-oriented greedy: open a station, then keep
    adding the longest task whose predecessors are all placed and that still
    fits, until none fits. Feasible whenever every t_i <= ct, so its count is an
    upper bound ub on m. (The paper takes ub from 100 runs of a randomized greedy,
    p. 60; one deterministic run is enough for a valid bound.)
    """
    cycle_time: int = instance.cycle_time
    num_tasks: int = len(instance.tasks)
    times: list[int] = task_times(instance)
    if any(time > cycle_time for time in times):
        return num_tasks  # infeasible either way; the model proves it
    unplaced_predecessors: list[int] = [0] * (num_tasks + 1)
    direct_successors: list[list[int]] = [[] for _ in range(num_tasks + 1)]
    for before, after in instance.precedences:
        unplaced_predecessors[after] += 1
        direct_successors[before].append(after)
    available: set[int] = {
        task for task in range(1, num_tasks + 1) if unplaced_predecessors[task] == 0
    }
    opened: int = 0
    while available:
        opened += 1
        load: int = 0
        while True:
            fitting: list[int] = [task for task in available if load + times[task] <= cycle_time]
            if not fitting:
                break
            chosen: int = max(fitting, key=lambda task: (times[task], -task))
            available.remove(chosen)
            load += times[chosen]
            for successor in direct_successors[chosen]:
                unplaced_predecessors[successor] -= 1
                if unplaced_predecessors[successor] == 0:
                    available.add(successor)
    return max(opened, 1)


def station_gaps(instance: ProblemInstance, precomputed: Precomputed) -> dict[tuple[int, int], int]:
    """D_ij (10) for every transitive predecessor pair, pruned by (4''), keyed by
    (before, after) task ids.

    D_ij = floor((t_i + t_j - 1 + sum over k in S~_i & P~_j of t_k) / ct): the
    tasks from i to j must span at least D_ij + 1 stations. D is not
    subadditive, so a pair is dropped only when some intermediate k gives
    D_ik + D_kj >= D_ij; the dropped constraint then follows from the pairs
    kept for the shorter intervals i..k and k..j.
    """
    times: list[int] = task_times(instance)
    cycle_time: int = instance.cycle_time
    gap_of: dict[tuple[int, int], int] = {}
    for after in range(1, len(times)):
        for before in precomputed.all_predecessors[after]:
            between: frozenset[int] = (
                precomputed.all_successors[before] & precomputed.all_predecessors[after]
            )
            gap_of[before, after] = (
                times[before] + times[after] - 1 + sum(times[middle] for middle in between)
            ) // cycle_time
    return {
        (before, after): gap
        for (before, after), gap in gap_of.items()
        if not any(
            gap <= gap_of[before, middle] + gap_of[middle, after]
            for middle in precomputed.all_successors[before] & precomputed.all_predecessors[after]
        )
    }


def solve(
    instance: ProblemInstance,
    formulation: Formulation = "full",
    time_limit_seconds: float | None = None,
) -> Solution:
    task_ids: range = range(1, len(instance.tasks) + 1)
    times: list[int] = task_times(instance)
    cycle_time: int = instance.cycle_time
    precomputed: Precomputed = precompute(instance)
    max_stations: int = greedy_station_count(instance)  # ub
    use_station_window: bool = formulation != "base"

    model: cp_model.CpModel = cp_model.CpModel()
    num_stations: cp_model.IntVar = model.new_int_var(1, max_stations, "m")  # m, domain (6)
    # station_of[task] = x_i, the station of that task, domain (5); (9)'s constant
    # half E_i <= x_i goes straight into the domain. station_of[0] is a fixed
    # placeholder so the list is indexed by task id; no constraint uses it.
    station_of: list[cp_model.IntVar] = [model.new_constant(0)] + [
        model.new_int_var(
            min(precomputed.earliest_station[task], max_stations) if use_station_window else 1,
            max_stations,
            f"x_{task}",
        )
        for task in task_ids
    ]

    # (2): the tasks on station j take at most ct. (x_i = j) is reified into a
    # Boolean that joins the sum as 0/1, as the paper's CP Optimizer syntax does.
    for station in range(1, max_stations + 1):
        on_this_station: list[cp_model.IntVar] = []
        for task in task_ids:
            is_on: cp_model.IntVar = model.new_bool_var(f"on_{task}_{station}")
            model.add(station_of[task] == station).only_enforce_if(is_on)
            model.add(station_of[task] != station).only_enforce_if(~is_on)
            on_this_station.append(is_on)
        model.add(cp_model.LinearExpr.weighted_sum(on_this_station, times[1:]) <= cycle_time)

    # (3): a task with no successor sits within the m stations in use.
    for task in task_ids:
        if not precomputed.all_successors[task]:
            model.add(station_of[task] <= num_stations)

    if use_station_window:
        # (9): L_i more stations must follow the task's own.
        for task in task_ids:
            model.add(station_of[task] <= num_stations - precomputed.stations_after[task])

    if formulation == "full":
        # (4') pruned by (4''), replacing (4).
        for (before, after), gap in station_gaps(instance, precomputed).items():
            model.add(station_of[before] + gap <= station_of[after])
    else:
        # (4)
        for before, after in instance.precedences:
            model.add(station_of[before] <= station_of[after])

    model.minimize(num_stations)  # (1)

    solver: cp_model.CpSolver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = 1
    if time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = time_limit_seconds
    status: cp_model.CpSolverStatus = solver.solve(model)

    status_map: dict[cp_model.CpSolverStatus, str] = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }

    stations: list[list[int]] | None = None
    objective: int | None = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        objective = solver.value(num_stations)
        stations = [
            [task for task in task_ids if solver.value(station_of[task]) == station]
            for station in range(1, objective + 1)
        ]

    bound_states: tuple[cp_model.CpSolverStatus, ...] = (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
        cp_model.UNKNOWN,
    )
    best_objective_bound: float | None = (
        float(solver.best_objective_bound) if status in bound_states else None
    )
    return Solution(
        status=status_map.get(status, "error"),
        objective=objective,
        best_objective_bound=best_objective_bound,
        stations=stations,
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload_solution: dict[str, Any] = {}
    if solution.stations is not None:
        payload_solution = {"stations": solution.stations}
    return {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": solution.best_objective_bound,
        "solution": payload_solution,
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(
        serialize_solution(solve(parse_input(read_input()), _formulation(), _time_limit_seconds()))
    )


if __name__ == "__main__":
    main()
