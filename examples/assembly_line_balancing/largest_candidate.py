"""Comparison script: the largest-candidate rule for simple assembly line balancing.

Not an optimizer. It builds one line balance with a rule a planner could follow
by hand, so its station count can be set against model.py's optimum:

    Open a station. Repeatedly put on it the longest task whose direct
    predecessors all sit on this or an earlier station and whose time still fits
    in what is left of the cycle time (ties to the lower task id). When no task
    fits, open the next station.

It is the rule model.py's greedy_station_count runs for its upper bound on m,
kept here as its own copy so that this script returns the stations and not only
their count. Each station lists its tasks in the order the rule placed them.
Output has the same shape as model.py's, with status "feasible", or
"infeasible" when a task takes longer than the cycle time and so fits on no
station.

Run from the repository root:
    uv run examples/assembly_line_balancing/largest_candidate.py JACKSON_c10.json
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


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
    # stations[j - 1] lists the task ids on station j, in the order placed.
    stations: list[list[int]] | None = None


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


def solve(instance: ProblemInstance) -> Solution:
    cycle_time: int = instance.cycle_time
    time_of: dict[int, int] = {task.id: task.time for task in instance.tasks}
    if any(time > cycle_time for time in time_of.values()):
        return Solution(status="infeasible")
    predecessors: dict[int, set[int]] = {task.id: set() for task in instance.tasks}
    for before, after in instance.precedences:
        predecessors[after].add(before)

    placed: set[int] = set()
    stations: list[list[int]] = []
    while len(placed) < len(time_of):
        station: list[int] = []
        load: int = 0
        while True:
            candidates: list[int] = [
                task
                for task in time_of
                if task not in placed
                and predecessors[task] <= placed
                and load + time_of[task] <= cycle_time
            ]
            if not candidates:
                break
            chosen: int = max(candidates, key=lambda task: (time_of[task], -task))
            station.append(chosen)
            placed.add(chosen)
            load += time_of[chosen]
        # Every task fits an empty station, so an empty one means no unplaced
        # task has all its predecessors placed: the graph has a cycle.
        if not station:
            raise ValueError("the precedence graph has a cycle")
        stations.append(station)
    return Solution(status="feasible", objective=len(stations), stations=stations)


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload_solution: dict[str, Any] = {}
    if solution.stations is not None:
        payload_solution = {"stations": solution.stations}
    return {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": None,
        "solution": payload_solution,
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(serialize_solution(solve(parse_input(read_input()))))


if __name__ == "__main__":
    main()
