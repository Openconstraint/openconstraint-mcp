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
    # (i, j): task i is a direct predecessor of task j.
    precedences: list[tuple[int, int]]


class Solution(FrozenModel):
    status: str
    objective: int | None = None
    best_objective_bound: float | None = None
    # stations[j - 1] lists the task ids on station j, for j = 1..m.
    stations: list[list[int]] | None = None


class Precomputed(FrozenModel):
    """The instance-only quantities the tightened formulations use, per task index."""

    all_predecessors: list[frozenset[int]]
    all_successors: list[frozenset[int]]
    earliest: list[int]  # E_i
    tail: list[int]  # L_i


def _formulation() -> Formulation:
    value: str = sys.argv[2] if len(sys.argv) > 2 else "full"
    if value not in FORMULATIONS:
        raise SystemExit(f"formulation must be one of {', '.join(FORMULATIONS)}, got {value!r}")
    return value


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
    for i, j in precedences:
        if not (1 <= i <= len(tasks) and 1 <= j <= len(tasks)) or i == j:
            raise ValueError(f"precedence {i}->{j} does not join two distinct known tasks")
    return ProblemInstance(cycle_time=raw["cycle_time"], tasks=tasks, precedences=precedences)


def topological_order(num_tasks: int, precedences: list[tuple[int, int]]) -> list[int]:
    """Task indices (0-based) in an order where every predecessor comes first."""
    successors: list[list[int]] = [[] for _ in range(num_tasks)]
    indegree: list[int] = [0] * num_tasks
    for i, j in precedences:
        successors[i - 1].append(j - 1)
        indegree[j - 1] += 1
    order: list[int] = [index for index in range(num_tasks) if indegree[index] == 0]
    for index in order:  # grows while iterating: a queue without the import
        for successor in successors[index]:
            indegree[successor] -= 1
            if indegree[successor] == 0:
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
    times: list[int] = [task.time for task in instance.tasks]
    ct: int = instance.cycle_time
    direct_predecessors: list[list[int]] = [[] for _ in range(num_tasks)]
    for i, j in instance.precedences:
        direct_predecessors[j - 1].append(i - 1)

    order: list[int] = topological_order(num_tasks, instance.precedences)
    predecessors: list[set[int]] = [set() for _ in range(num_tasks)]
    for index in order:
        for parent in direct_predecessors[index]:
            predecessors[index] |= predecessors[parent] | {parent}
    successors: list[set[int]] = [set() for _ in range(num_tasks)]
    for index in range(num_tasks):
        for ancestor in predecessors[index]:
            successors[ancestor].add(index)

    earliest: list[int] = [
        -(-(times[i] + sum(times[k] for k in predecessors[i])) // ct) for i in range(num_tasks)
    ]
    tail: list[int] = [
        (times[i] - 1 + sum(times[k] for k in successors[i])) // ct for i in range(num_tasks)
    ]
    return Precomputed(
        all_predecessors=[frozenset(item) for item in predecessors],
        all_successors=[frozenset(item) for item in successors],
        earliest=earliest,
        tail=tail,
    )


def greedy_station_count(instance: ProblemInstance) -> int:
    """Stations used by a station-oriented greedy: open a station, then keep
    adding the longest task whose predecessors are all placed and that still
    fits, until none fits. Feasible whenever every t_i <= ct, so its count is an
    upper bound ub on m. (The paper takes ub from 100 runs of a randomized greedy,
    p. 60; one deterministic run is enough for a valid bound.)
    """
    ct: int = instance.cycle_time
    num_tasks: int = len(instance.tasks)
    if any(task.time > ct for task in instance.tasks):
        return num_tasks  # infeasible either way; the model proves it
    remaining_predecessors: list[int] = [0] * num_tasks
    successors: list[list[int]] = [[] for _ in range(num_tasks)]
    for i, j in instance.precedences:
        remaining_predecessors[j - 1] += 1
        successors[i - 1].append(j - 1)
    available: set[int] = {i for i in range(num_tasks) if remaining_predecessors[i] == 0}
    stations: int = 0
    while available:
        stations += 1
        load: int = 0
        while True:
            fitting: list[int] = [i for i in available if load + instance.tasks[i].time <= ct]
            if not fitting:
                break
            chosen: int = max(fitting, key=lambda i: (instance.tasks[i].time, -i))
            available.remove(chosen)
            load += instance.tasks[chosen].time
            for successor in successors[chosen]:
                remaining_predecessors[successor] -= 1
                if remaining_predecessors[successor] == 0:
                    available.add(successor)
    return max(stations, 1)


def distance_pairs(instance: ProblemInstance, pre: Precomputed) -> dict[tuple[int, int], int]:
    """D_ij (10) for every transitive predecessor pair, pruned by (4'').

    D_ij = floor((t_i + t_j - 1 + sum over k in S~_i & P~_j of t_k) / ct): the
    tasks from i to j must span at least D_ij + 1 stations. D is not
    subadditive, so a pair is dropped only when some intermediate k gives
    D_ik + D_kj >= D_ij; the dropped constraint then follows from the pairs
    kept for the shorter intervals i..k and k..j.
    """
    times: list[int] = [task.time for task in instance.tasks]
    ct: int = instance.cycle_time
    distance: dict[tuple[int, int], int] = {}
    for j in range(len(times)):
        for i in pre.all_predecessors[j]:
            between: frozenset[int] = pre.all_successors[i] & pre.all_predecessors[j]
            distance[i, j] = (times[i] + times[j] - 1 + sum(times[k] for k in between)) // ct
    return {
        (i, j): d
        for (i, j), d in distance.items()
        if not any(
            d <= distance[i, k] + distance[k, j]
            for k in pre.all_successors[i] & pre.all_predecessors[j]
        )
    }


def solve(
    instance: ProblemInstance,
    formulation: Formulation = "full",
    time_limit_seconds: float | None = None,
) -> Solution:
    num_tasks: int = len(instance.tasks)
    ct: int = instance.cycle_time
    pre: Precomputed = precompute(instance)
    ub: int = greedy_station_count(instance)
    tightened: bool = formulation != "base"

    model: cp_model.CpModel = cp_model.CpModel()
    m: cp_model.IntVar = model.new_int_var(1, ub, "m")  # (6)
    # x[i] = station of task i + 1, domain (5); (9)'s constant half E_i <= x_i
    # goes straight into the domain.
    x: list[cp_model.IntVar] = [
        model.new_int_var(min(pre.earliest[i], ub) if tightened else 1, ub, f"x_{i + 1}")
        for i in range(num_tasks)
    ]

    # (2): the tasks on station j take at most ct. (x_i = j) is reified into a
    # Boolean that joins the sum as 0/1, as the paper's CP Optimizer syntax does.
    for station in range(1, ub + 1):
        on_station: list[cp_model.IntVar] = []
        for i in range(num_tasks):
            here: cp_model.IntVar = model.new_bool_var(f"on_{i + 1}_{station}")
            model.add(x[i] == station).only_enforce_if(here)
            model.add(x[i] != station).only_enforce_if(~here)
            on_station.append(here)
        model.add(
            cp_model.LinearExpr.weighted_sum(on_station, [task.time for task in instance.tasks])
            <= ct
        )

    # (3): a task with no successor sits within the m stations in use.
    for i in range(num_tasks):
        if not pre.all_successors[i]:
            model.add(x[i] <= m)

    if tightened:
        # (9): L_i more stations must follow task i's.
        for i in range(num_tasks):
            model.add(x[i] <= m - pre.tail[i])

    if formulation == "full":
        # (4') pruned by (4''), replacing (4).
        for (i, j), d in distance_pairs(instance, pre).items():
            model.add(x[i] + d <= x[j])
    else:
        # (4)
        for i, j in instance.precedences:
            model.add(x[i - 1] <= x[j - 1])

    model.minimize(m)  # (1)

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
        objective = solver.value(m)
        stations = [[] for _ in range(objective)]
        for i in range(num_tasks):
            stations[solver.value(x[i]) - 1].append(i + 1)

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
