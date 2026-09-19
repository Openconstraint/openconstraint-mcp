"""Checker script for model.py.

Validates one claimed line balance (simple assembly line balancing, type 1)
against the instance supplied via `payload["problem"]`. It never solves the
problem; it only grades the answer it is given:

- every task of the instance sits on exactly one station, and no unknown task
  appears;
- no station's load (sum of its task times) exceeds the cycle time;
- no task sits on an earlier station than one of its direct predecessors
  (the transitive relations follow from the direct ones);
- the reported objective equals the number of stations listed.

It does not prove that the station count is minimal; details report the
total-work lower bound ceil(sum t_i / ct) for comparison.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str). The checker admits solver_status in {"optimal",
  "feasible", "timeout"} -- mirroring pyexec/eligibility.py's
  DIAGNOSTIC_ACCEPT_STATUSES -- and treats every other value as ungradeable.
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {...}}
- "accepted" with an empty errors list is the only passing verdict.

"error" means the payload could not be graded at all -- an unusable instance, or
a solution/solver_status that is not a well-formed station list. "rejected"
means a well-formed station list WAS graded against the instance and violates it.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

from typing_extensions import TypeIs

# (cycle_time, task times indexed by id - 1, direct precedences (i, j))
Instance = tuple[int, list[int], list[tuple[int, int]]]

ACCEPT_STATUSES: frozenset[str] = frozenset({"optimal", "feasible", "timeout"})


def _is_int(value: object) -> TypeIs[int]:
    """True only for a genuine int: JSON `true`/`false` must not pass as 1/0."""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_instance(problem: object) -> tuple[Instance | None, str | None]:
    """Parse (cycle_time, times, precedences) out of payload["problem"]."""
    if not isinstance(problem, str):
        return None, "payload.problem is missing or not a string"
    try:
        instance: object = json.loads(problem)
    except json.JSONDecodeError as exc:
        return None, f"payload.problem is not valid JSON: {exc}"
    if not isinstance(instance, dict):
        return None, "problem instance is not a JSON object"

    cycle_time: object = instance.get("cycle_time")
    if not _is_int(cycle_time) or cycle_time < 1:
        return None, "problem instance cycle_time missing or not a positive int"

    raw_tasks: object = instance.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        return None, "problem instance tasks missing, not a list, or empty"
    times: list[int] = []
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict) or item.get("id") != index + 1:
            return None, f"problem instance tasks[{index}] is not an object with id {index + 1}"
        time: object = item.get("time")
        if not _is_int(time) or time < 0:
            return None, f"problem instance task {index + 1} time not a non-negative int"
        times.append(time)

    raw_precedences: object = instance.get("precedences")
    if not isinstance(raw_precedences, list):
        return None, "problem instance precedences missing or not a list"
    precedences: list[tuple[int, int]] = []
    for index, pair in enumerate(raw_precedences):
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(_is_int(task) and 1 <= task <= len(times) for task in pair)
        ):
            return None, f"problem instance precedences[{index}] is not a pair of task ids"
        precedences.append((pair[0], pair[1]))
    return (cycle_time, times, precedences), None


def _load_stations(solution: object) -> tuple[list[list[int]] | None, list[str]]:
    if not isinstance(solution, dict):
        return None, ["solution is not a dict"]
    raw_stations: object = solution.get("stations")
    if not isinstance(raw_stations, list):
        return None, ["solution.stations must be a list"]
    errors: list[str] = []
    stations: list[list[int]] = []
    for index, raw in enumerate(raw_stations):
        if not isinstance(raw, list) or not all(_is_int(task) for task in raw):
            errors.append(f"stations[{index}] is not a list of int task ids")
        else:
            stations.append(raw)
    return (None, errors) if errors else (stations, errors)


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    parsed, instance_error = _parse_instance(payload.get("problem"))
    if instance_error is not None:
        return {"status": "error", "errors": [instance_error], "details": {}}
    assert parsed is not None
    cycle_time, times, precedences = parsed

    protocol_errors: list[str] = []
    solver_status: object = payload.get("solver_status")
    if solver_status not in ACCEPT_STATUSES:
        protocol_errors.append(
            f"solver_status is {solver_status!r}, expected optimal, feasible, or timeout"
        )
    stations, station_errors = _load_stations(payload.get("solution"))
    protocol_errors.extend(station_errors)
    if protocol_errors:
        return {"status": "error", "errors": protocol_errors, "details": {}}
    assert stations is not None

    errors: list[str] = []
    station_of: dict[int, int] = {}
    loads: list[int] = []
    for number, tasks in enumerate(stations, start=1):
        load: int = 0
        for task in tasks:
            if not 1 <= task <= len(times):
                errors.append(f"station {number} holds unknown task {task}")
                continue
            if task in station_of:
                errors.append(f"task {task} is on stations {station_of[task]} and {number}")
                continue
            station_of[task] = number
            load += times[task - 1]
        loads.append(load)
        if load > cycle_time:
            errors.append(f"station {number} load {load} exceeds cycle time {cycle_time}")

    missing: list[int] = [task for task in range(1, len(times) + 1) if task not in station_of]
    if missing:
        errors.append(f"tasks {missing} are on no station")

    for i, j in precedences:
        if i in station_of and j in station_of and station_of[i] > station_of[j]:
            errors.append(
                f"task {i} (station {station_of[i]}) must not follow its successor "
                f"{j} (station {station_of[j]})"
            )

    objective: object = payload.get("objective")
    if not isinstance(objective, int | float) or isinstance(objective, bool):
        errors.append(
            f"objective must be a number equal to the station count {len(stations)}, "
            f"got {objective!r}"
        )
    elif objective != len(stations):
        errors.append(f"objective {objective} does not match station count {len(stations)}")

    details: dict[str, Any] = {
        "station_count": len(stations),
        "station_loads": loads,
        "cycle_time": cycle_time,
        "total_work_lower_bound": -(-sum(times) // cycle_time),
    }
    status: str = "accepted" if not errors else "rejected"
    return {"status": status, "errors": errors, "details": details}


def main() -> None:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "status": "error",
                    "errors": ["usage: python checker.py <payload.json>"],
                    "details": {},
                }
            )
        )
        return

    with open(sys.argv[1], encoding="utf-8") as payload_file:
        payload: dict[str, Any] = json.load(payload_file)
    print(json.dumps(check_payload(payload)))


if __name__ == "__main__":
    main()
