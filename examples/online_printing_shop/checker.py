"""Standalone feasibility checker for Online Printing Shop schedules.

The checker receives a checker payload path in ``sys.argv[1]``. Its ``problem``
field may contain literal instance JSON or a bare filename resolved beside this
file. It validates the reported schedule but never solves the instance.
"""

from __future__ import annotations

import json
import math
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TypeGuard

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this checker's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class MachineSpec(FrozenModel):
    outages: tuple[tuple[int, int], ...]
    first_setups: dict[str, int]
    transitions: dict[str, dict[str, int]]


class OperationSpec(FrozenModel):
    job: str
    successors: tuple[str, ...]
    machine_options: dict[str, int]
    release_time: int
    theta: Decimal
    fixed: tuple[str, int] | None


class Instance(FrozenModel):
    machines: dict[str, MachineSpec]
    operations: dict[str, OperationSpec]


class ScheduleEntry(FrozenModel):
    operation: str
    job: str
    machine: str
    predecessor: str | None
    setup_start: int
    setup_duration: int
    start: int
    processing_time: int
    theta_completion_time: int
    end: int


class SolutionClaim(FrozenModel):
    makespan: int
    schedule: list[ScheduleEntry]


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _time_map(value: object, where: str) -> tuple[dict[str, int] | None, str | None]:
    if not isinstance(value, dict):
        return None, f"{where} is not an object"
    result: dict[str, int] = {}
    for key, duration in value.items():
        if not isinstance(key, str) or not key:
            return None, f"{where} contains an invalid operation ID"
        if not _is_int(duration) or duration < 0:
            return None, f"{where}[{key!r}] is not a nonnegative int"
        result[key] = duration
    return result, None


def _resolve_problem(problem: object) -> tuple[dict[str, Any] | None, str, str | None]:
    if not isinstance(problem, str):
        return None, "", "payload.problem is missing or not a string"
    text = problem.strip()
    if not text:
        return None, "", "payload.problem is empty"

    if text.startswith("{"):
        try:
            instance = json.loads(text, parse_float=Decimal)
        except (json.JSONDecodeError, InvalidOperation) as exc:
            return None, "", f"payload.problem is not valid JSON: {exc}"
        source = "inline"
    else:
        if "/" in text or "\\" in text or text in {".", ".."}:
            return None, "", f"payload.problem filename {text!r} must be a bare filename"
        path = Path(__file__).parent / text
        if not path.is_file():
            return None, "", f"payload.problem names no sibling file {text!r}"
        try:
            instance = json.loads(path.read_text(encoding="utf-8"), parse_float=Decimal)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, InvalidOperation) as exc:
            return None, "", f"payload.problem file {text!r} is not valid JSON: {exc}"
        source = f"file:{text}"

    if not isinstance(instance, dict):
        return None, "", "problem instance is not a JSON object"
    return instance, source, None


def _parse_instance(raw: dict[str, Any]) -> tuple[Instance | None, str | None]:
    raw_machines = raw.get("machines")
    if not isinstance(raw_machines, dict) or not raw_machines:
        return None, "problem instance machines is not a non-empty object"

    machines: dict[str, MachineSpec] = {}
    for machine_id, raw_machine in raw_machines.items():
        where = f"problem instance machines[{machine_id!r}]"
        if not isinstance(machine_id, str) or not machine_id:
            return None, "problem instance contains an invalid machine ID"
        if not isinstance(raw_machine, dict):
            return None, f"{where} is not an object"

        raw_outages = raw_machine.get("unavailability")
        if not isinstance(raw_outages, list):
            return None, f"{where}.unavailability is not a list"
        outages: list[tuple[int, int]] = []
        for index, raw_outage in enumerate(raw_outages):
            if not isinstance(raw_outage, dict):
                return None, f"{where}.unavailability[{index}] is not an object"
            start, end = raw_outage.get("start"), raw_outage.get("end")
            if not _is_int(start) or not _is_int(end) or start < 0 or end <= start:
                return None, f"{where}.unavailability[{index}] has invalid endpoints"
            if outages and start < outages[-1][1]:
                return None, f"{where}.unavailability is not ordered and nonoverlapping"
            outages.append((start, end))

        raw_setups = raw_machine.get("setup_times")
        if not isinstance(raw_setups, dict):
            return None, f"{where}.setup_times is not an object"
        first, error = _time_map(raw_setups.get("first"), f"{where}.setup_times.first")
        if error is not None:
            return None, error
        assert first is not None
        raw_transitions = raw_setups.get("transitions")
        if not isinstance(raw_transitions, dict):
            return None, f"{where}.setup_times.transitions is not an object"
        transitions: dict[str, dict[str, int]] = {}
        for predecessor, raw_targets in raw_transitions.items():
            if not isinstance(predecessor, str) or not predecessor:
                return None, f"{where}.setup_times.transitions has an invalid source"
            targets, error = _time_map(
                raw_targets, f"{where}.setup_times.transitions[{predecessor!r}]"
            )
            if error is not None:
                return None, error
            assert targets is not None
            transitions[predecessor] = targets
        machines[machine_id] = MachineSpec(
            outages=tuple(outages), first_setups=first, transitions=transitions
        )

    raw_operations = raw.get("operations")
    if not isinstance(raw_operations, dict) or not raw_operations:
        return None, "problem instance operations is not a non-empty object"

    operations: dict[str, OperationSpec] = {}
    for operation_id, raw_operation in raw_operations.items():
        where = f"problem instance operations[{operation_id!r}]"
        if not isinstance(operation_id, str) or not operation_id:
            return None, "problem instance contains an invalid operation ID"
        if not isinstance(raw_operation, dict):
            return None, f"{where} is not an object"

        job = raw_operation.get("job")
        if not isinstance(job, str) or not job:
            return None, f"{where}.job is not a non-empty string"
        raw_successors = raw_operation.get("successors")
        if (
            not isinstance(raw_successors, list)
            or not all(isinstance(value, str) and value for value in raw_successors)
            or len(raw_successors) != len(set(raw_successors))
        ):
            return None, f"{where}.successors is not a list of distinct operation IDs"

        raw_options = raw_operation.get("machine_options")
        if not isinstance(raw_options, dict) or not raw_options:
            return None, f"{where}.machine_options is not a non-empty object"
        options: dict[str, int] = {}
        for machine_id, raw_option in raw_options.items():
            if (
                not isinstance(machine_id, str)
                or not machine_id
                or not isinstance(raw_option, dict)
            ):
                return None, f"{where}.machine_options contains an invalid option"
            processing_time = raw_option.get("processing_time")
            if not _is_int(processing_time) or processing_time < 0:
                return None, f"{where}.machine_options[{machine_id!r}] has invalid processing_time"
            options[machine_id] = processing_time

        release_time = raw_operation.get("release_time")
        if not _is_int(release_time) or release_time < 0:
            return None, f"{where}.release_time is not a nonnegative int"
        raw_theta = raw_operation.get("theta")
        if isinstance(raw_theta, bool):
            return None, f"{where}.theta is not in (0, 1]"
        try:
            theta = raw_theta if isinstance(raw_theta, Decimal) else Decimal(str(raw_theta))
        except (InvalidOperation, ValueError):
            return None, f"{where}.theta is not in (0, 1]"
        if not theta.is_finite() or not Decimal(0) < theta <= Decimal(1):
            return None, f"{where}.theta is not in (0, 1]"

        raw_fixed = raw_operation.get("fixed")
        fixed: tuple[str, int] | None = None
        if raw_fixed is not None:
            if not isinstance(raw_fixed, dict):
                return None, f"{where}.fixed is not an object"
            fixed_machine, fixed_start = raw_fixed.get("machine"), raw_fixed.get("start")
            if (
                not isinstance(fixed_machine, str)
                or not fixed_machine
                or not _is_int(fixed_start)
                or fixed_start < 0
            ):
                return None, f"{where}.fixed is invalid"
            fixed = (fixed_machine, fixed_start)

        operations[operation_id] = OperationSpec(
            job=job,
            successors=tuple(raw_successors),
            machine_options=options,
            release_time=release_time,
            theta=theta,
            fixed=fixed,
        )

    operation_ids = set(operations)
    machine_ids = set(machines)
    indegree = dict.fromkeys(operation_ids, 0)
    for operation_id, operation in operations.items():
        unknown_machines = set(operation.machine_options) - machine_ids
        if unknown_machines:
            return None, f"operation {operation_id!r} references unknown machines"
        if operation.fixed is not None:
            fixed_machine, fixed_start = operation.fixed
            if (
                fixed_machine not in operation.machine_options
                or fixed_start < operation.release_time
            ):
                return None, f"operation {operation_id!r} has an invalid fixed assignment"
        for successor_operation_id in operation.successors:
            successor = operations.get(successor_operation_id)
            if successor is None or successor.job != operation.job:
                return None, f"operation {operation_id!r} has an invalid successor"
            indegree[successor_operation_id] += 1

    ready = [operation_id for operation_id, degree in indegree.items() if degree == 0]
    visited = 0
    while ready:
        operation_id = ready.pop()
        visited += 1
        for successor_operation_id in operations[operation_id].successors:
            indegree[successor_operation_id] -= 1
            if indegree[successor_operation_id] == 0:
                ready.append(successor_operation_id)
    if visited != len(operations):
        return None, "problem instance precedence graph is cyclic"

    for machine_id, machine in machines.items():
        eligible = {
            operation_id
            for operation_id, operation in operations.items()
            if machine_id in operation.machine_options
        }
        if set(machine.first_setups) != eligible:
            return None, f"machine {machine_id!r} has an incomplete first setup map"
        expected_sources = eligible if len(eligible) > 1 else set()
        if set(machine.transitions) != expected_sources:
            return None, f"machine {machine_id!r} has incomplete setup transition sources"
        for source, targets in machine.transitions.items():
            if set(targets) != eligible - {source}:
                return None, f"machine {machine_id!r} has an incomplete setup transition row"

    return Instance(machines=machines, operations=operations), None


def _load_solution(value: object) -> tuple[SolutionClaim | None, list[str]]:
    if not isinstance(value, dict):
        return None, ["solution is not a dict"]
    makespan = value.get("makespan")
    if not _is_int(makespan):
        return None, ["solution.makespan missing or not an int"]
    raw_schedule = value.get("schedule")
    if not isinstance(raw_schedule, list):
        return None, ["solution.schedule must be a list"]

    errors: list[str] = []
    schedule: list[ScheduleEntry] = []
    string_fields = ("operation", "job", "machine")
    int_fields = (
        "setup_start",
        "setup_duration",
        "start",
        "processing_time",
        "theta_completion_time",
        "end",
    )
    for index, raw_entry in enumerate(raw_schedule):
        if not isinstance(raw_entry, dict):
            errors.append(f"schedule[{index}] is not a dict")
            continue
        for key in string_fields:
            if not isinstance(raw_entry.get(key), str):
                errors.append(f"schedule[{index}].{key} missing or not a string")
        predecessor = raw_entry.get("predecessor")
        if predecessor is not None and not isinstance(predecessor, str):
            errors.append(f"schedule[{index}].predecessor must be a string or null")
        for key in int_fields:
            if not _is_int(raw_entry.get(key)):
                errors.append(f"schedule[{index}].{key} missing or not an int")
        if errors:
            continue
        schedule.append(
            ScheduleEntry(
                operation=raw_entry["operation"],
                job=raw_entry["job"],
                machine=raw_entry["machine"],
                predecessor=predecessor,
                setup_start=raw_entry["setup_start"],
                setup_duration=raw_entry["setup_duration"],
                start=raw_entry["start"],
                processing_time=raw_entry["processing_time"],
                theta_completion_time=raw_entry["theta_completion_time"],
                end=raw_entry["end"],
            )
        )
    return (None, errors) if errors else (SolutionClaim(makespan=makespan, schedule=schedule), [])


def _advance_active(start: int, duration: int, outages: tuple[tuple[int, int], ...]) -> int:
    current = start
    remaining = duration
    if remaining == 0:
        return current
    for outage_start, outage_end in outages:
        if outage_end <= current:
            continue
        if outage_start <= current < outage_end:
            current = outage_end
        available = outage_start - current
        if remaining <= available:
            return current + remaining
        if available > 0:
            remaining -= available
        current = outage_end
    return current + remaining


def _required_processing(theta: Decimal, processing_time: int) -> int:
    """Return ceil(theta * processing_time) exactly, whatever the decimal context.

    Decimal *stores* every digit but *multiplies* at the active context precision
    (28 significant digits by default), which rounds a product just above an
    integer down onto it. Reading theta as a coefficient/exponent ratio keeps the
    ceiling exact for any theta an instance file states, and matches models.py.
    """

    numerator, denominator = theta.as_integer_ratio()
    return -(-numerator * processing_time // denominator)


def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> bool:
    return start_a < end_b and start_b < end_a


def _check_machine_chain(
    machine_id: str,
    entries: list[ScheduleEntry],
    machine: MachineSpec | None,
) -> list[str]:
    errors: list[str] = []
    roots = [entry for entry in entries if entry.predecessor is None]
    if len(roots) != 1:
        errors.append(f"machine {machine_id!r} must have exactly one null predecessor")

    by_operation = {entry.operation: entry for entry in entries}
    successor_by_predecessor: dict[str, ScheduleEntry] = {}
    for entry in entries:
        predecessor = entry.predecessor
        if predecessor is None:
            continue
        predecessor_entry = by_operation.get(predecessor)
        if predecessor_entry is None:
            errors.append(
                f"operation {entry.operation!r} predecessor {predecessor!r} is not on "
                f"machine {machine_id!r}"
            )
        elif predecessor in successor_by_predecessor:
            errors.append(f"machine {machine_id!r} predecessor {predecessor!r} is reused")
        else:
            successor_by_predecessor[predecessor] = entry

    chain: list[ScheduleEntry] = []
    if len(roots) == 1:
        current: ScheduleEntry | None = roots[0]
        visited: set[str] = set()
        while current is not None and current.operation not in visited:
            visited.add(current.operation)
            chain.append(current)
            current = successor_by_predecessor.get(current.operation)
        if len(visited) != len(entries):
            errors.append(f"machine {machine_id!r} predecessor links do not form one chain")

    for previous, current in zip(chain, chain[1:], strict=False):
        if current.setup_start < previous.end:
            errors.append(
                f"operation {current.operation!r} starts setup before predecessor "
                f"{previous.operation!r} finishes"
            )

    if machine is not None:
        for index, entry in enumerate(chain):
            if index == 0:
                expected_setup = machine.first_setups.get(entry.operation)
            else:
                expected_setup = machine.transitions.get(chain[index - 1].operation, {}).get(
                    entry.operation
                )
            if expected_setup is None:
                errors.append(
                    f"operation {entry.operation!r} has no setup value on machine {machine_id!r}"
                )
            elif entry.setup_duration != expected_setup:
                errors.append(
                    f"operation {entry.operation!r} setup duration {entry.setup_duration} "
                    f"does not match {expected_setup}"
                )

    for index, first in enumerate(entries):
        for second in entries[index + 1 :]:
            if _overlap(first.setup_start, first.end, second.setup_start, second.end):
                errors.append(
                    f"machine {machine_id!r} occupancy overlaps operations "
                    f"{first.operation!r} and {second.operation!r}"
                )
    return errors


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    raw_instance, source, error = _resolve_problem(payload.get("problem"))
    if error is not None:
        return {"status": "error", "errors": [error], "details": {}}
    assert raw_instance is not None
    instance, error = _parse_instance(raw_instance)
    if error is not None:
        return {"status": "error", "errors": [error], "details": {}}
    assert instance is not None

    details: dict[str, Any] = {
        "instance_source": source,
        "num_machines": len(instance.machines),
        "num_operations": len(instance.operations),
    }
    # "timeout" is included alongside "optimal"/"feasible" because a timed-out
    # run can still hand back a well-formed recovered incumbent (see
    # pyexec/eligibility.py's DIAGNOSTIC_ACCEPT_STATUSES, which this set
    # mirrors); it asserts no optimality claim, so grading it is safe.
    protocol_errors: list[str] = []
    solver_status = payload.get("solver_status")
    if solver_status not in {"optimal", "feasible", "timeout"}:
        protocol_errors.append(
            f"solver_status is {solver_status!r}, expected optimal, feasible, or timeout"
        )
    solution, solution_errors = _load_solution(payload.get("solution"))
    protocol_errors.extend(solution_errors)
    if protocol_errors:
        return {"status": "error", "errors": protocol_errors, "details": details}
    assert solution is not None

    errors: list[str] = []
    entries: dict[str, ScheduleEntry] = {}
    expected_theta_completion_time: dict[str, int] = {}
    expected_end: dict[str, int] = {}
    for entry in solution.schedule:
        if entry.operation in entries:
            errors.append(f"operation {entry.operation!r} appears more than once")
            continue
        entries[entry.operation] = entry
        operation = instance.operations.get(entry.operation)
        if operation is None:
            errors.append(f"unknown operation {entry.operation!r}")
            continue
        machine = instance.machines.get(entry.machine)
        if machine is None or entry.machine not in operation.machine_options:
            errors.append(
                f"operation {entry.operation!r} uses ineligible machine {entry.machine!r}"
            )
            continue

        expected_processing = operation.machine_options[entry.machine]
        if entry.job != operation.job:
            errors.append(
                f"operation {entry.operation!r} has job {entry.job!r}, expected {operation.job!r}"
            )
        if entry.processing_time != expected_processing:
            errors.append(
                f"operation {entry.operation!r} processing time {entry.processing_time} "
                f"does not match {expected_processing}"
            )
        if entry.setup_duration < 0 or entry.setup_start != entry.start - entry.setup_duration:
            errors.append(f"operation {entry.operation!r} has inconsistent setup timing")
        if entry.setup_start < 0:
            errors.append(f"operation {entry.operation!r} has negative setup_start")
        if entry.start < operation.release_time:
            errors.append(f"operation {entry.operation!r} starts before its release time")
        if operation.fixed is not None and (entry.machine, entry.start) != operation.fixed:
            errors.append(f"operation {entry.operation!r} does not preserve its fixed assignment")

        for outage_start, outage_end in machine.outages:
            if outage_start <= entry.start < outage_end:
                errors.append(f"operation {entry.operation!r} starts during an outage")
            if outage_start < entry.end <= outage_end:
                errors.append(f"operation {entry.operation!r} ends during an outage")
            if _overlap(entry.setup_start, entry.start, outage_start, outage_end):
                errors.append(f"operation {entry.operation!r} setup overlaps an outage")

        end = _advance_active(entry.start, expected_processing, machine.outages)
        theta_completion_work = _required_processing(operation.theta, expected_processing)
        theta_completion_time = _advance_active(entry.start, theta_completion_work, machine.outages)
        expected_end[entry.operation] = end
        expected_theta_completion_time[entry.operation] = theta_completion_time
        if entry.end != end:
            errors.append(
                f"operation {entry.operation!r} end {entry.end} does not match active "
                f"processing completion {end}"
            )
        if entry.theta_completion_time != theta_completion_time:
            errors.append(
                f"operation {entry.operation!r} theta_completion_time "
                f"{entry.theta_completion_time} does not match {theta_completion_time}"
            )

    missing = set(instance.operations) - set(entries)
    if missing:
        errors.append(f"missing operations: {sorted(missing)}")

    by_machine: dict[str, list[ScheduleEntry]] = {}
    for entry in entries.values():
        by_machine.setdefault(entry.machine, []).append(entry)
    for machine_id, machine_entries in by_machine.items():
        errors.extend(
            _check_machine_chain(machine_id, machine_entries, instance.machines.get(machine_id))
        )

    for operation_id, operation in instance.operations.items():
        predecessor_entry = entries.get(operation_id)
        if predecessor_entry is None:
            continue
        for successor_operation_id in operation.successors:
            successor_entry = entries.get(successor_operation_id)
            if successor_entry is None:
                continue
            theta_completion_time = expected_theta_completion_time.get(
                operation_id, predecessor_entry.theta_completion_time
            )
            end = expected_end.get(operation_id, predecessor_entry.end)
            if successor_entry.start < theta_completion_time:
                errors.append(
                    f"successor {successor_operation_id!r} starts before "
                    f"operation {operation_id!r} is ready"
                )
            if successor_entry.end < end:
                errors.append(
                    f"successor {successor_operation_id!r} ends before "
                    f"operation {operation_id!r} ends"
                )

    max_end = max((entry.end for entry in solution.schedule), default=0)
    if solution.makespan != max_end:
        errors.append(
            f"solution.makespan {solution.makespan} does not match schedule makespan {max_end}"
        )
    objective = payload.get("objective")
    if (
        not isinstance(objective, int | float)
        or isinstance(objective, bool)
        or (isinstance(objective, float) and not math.isfinite(objective))
    ):
        errors.append(f"objective must equal schedule makespan {max_end}, got {objective!r}")
    elif objective != max_end:
        errors.append(f"objective {objective} does not match schedule makespan {max_end}")

    return {
        "status": "accepted" if not errors else "rejected",
        "errors": errors,
        "details": details,
    }


def main() -> None:
    if len(sys.argv) != 2:
        result = {
            "status": "error",
            "errors": ["usage: python checker.py <payload.json>"],
            "details": {},
        }
    else:
        try:
            with open(sys.argv[1], encoding="utf-8") as payload_file:
                payload = json.load(payload_file)
            result = (
                check_payload(payload)
                if isinstance(payload, dict)
                else {"status": "error", "errors": ["payload is not an object"], "details": {}}
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            result = {"status": "error", "errors": [f"cannot read payload: {exc}"], "details": {}}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
