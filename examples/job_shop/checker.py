"""Checker script for model.py.

Validates that the emitted job shop schedule is feasible for the job shop
instance supplied via `payload["problem"]`: every task appears exactly once
with the right machine and duration, tasks within a job run in order, no
machine runs two tasks at once, and the reported objective matches the
schedule's makespan. Because the instance rides in via the payload, this
checker validates ANY job shop instance (ft06, ft10, or another), not just
one hardcoded benchmark.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str). The checker admits solver_status in {"optimal",
  "feasible", "timeout"} -- mirroring pyexec/eligibility.py's
  DIAGNOSTIC_ACCEPT_STATUSES -- and treats every other value as ungradeable.
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}
- "accepted" with an empty errors list is the only passing verdict.

The two failing verdicts are split by WHAT failed, because the caller acts on
them differently. "error" means the payload could not be graded at all -- an
unusable instance, or a solution/solver_status that is not a well-formed
schedule claim. "rejected" means a well-formed schedule WAS graded against the
instance and violates it. Collapsing the two is actively harmful: a client told
"rejected" for a missing `solution.schedule` key is being pointed at the
constraint model when the bug is in the model's print statement, and the
plausible next move is to "fix" correct scheduling constraints. Both verdicts
still fail the server's verification gate, so nothing unsafe is accepted
either way -- the split only buys the caller an accurate diagnosis.
../flexible_job_shop/checker.py draws the same line.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

from typing_extensions import TypeIs

Jobs = list[list[tuple[int, int]]]


def _is_int(value: object) -> TypeIs[int]:
    """True only for a genuine int. `bool` is an `int` subclass in Python, so a JSON
    `true`/`false` left unguarded would sail through every downstream check as 1/0:
    it indexes, compares, and adds identically. This guard is the only place such a
    value can be caught."""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_instance(problem: object) -> tuple[Jobs | None, int | None, str | None]:
    """Parse the job shop instance out of payload["problem"].

    Returns (jobs, num_machines, error) where error is None on success.
    """
    if not isinstance(problem, str):
        return None, None, "payload.problem is missing or not a string"

    try:
        instance = json.loads(problem)
    except json.JSONDecodeError as exc:
        return None, None, f"payload.problem is not valid JSON: {exc}"

    if not isinstance(instance, dict):
        return None, None, "problem instance is not a JSON object"

    num_machines = instance.get("num_machines")
    if not _is_int(num_machines):
        return None, None, "problem instance num_machines missing or not an int"
    if num_machines < 1:
        return None, None, f"problem instance num_machines {num_machines} is not positive"

    raw_jobs = instance.get("jobs")
    if not isinstance(raw_jobs, list):
        return None, None, "problem instance jobs missing or not a list"
    # An instance with no tasks would accept an empty schedule with objective 0 as
    # trivially feasible, turning a serialization slip (jobs dropped or truncated on
    # the way into `problem`) into a passing verdict. Reject it as unusable ground
    # truth instead; model.py cannot solve these either — an empty `jobs` makes its
    # makespan max-equality unsatisfiable, and an empty job has no final task to
    # select.
    if not raw_jobs:
        return None, None, "problem instance jobs is empty"

    jobs: Jobs = []
    for job_id, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, list):
            return None, None, f"problem instance jobs[{job_id}] is not a list"
        if not raw_job:
            return None, None, f"problem instance jobs[{job_id}] has no tasks"
        tasks: list[tuple[int, int]] = []
        for task_id, raw_task in enumerate(raw_job):
            if (
                not isinstance(raw_task, list)
                or len(raw_task) != 2
                or not all(_is_int(v) for v in raw_task)
            ):
                return (
                    None,
                    None,
                    f"problem instance jobs[{job_id}][{task_id}] is not a [machine, "
                    "duration] pair of ints",
                )
            machine, duration = raw_task[0], raw_task[1]
            if not 0 <= machine < num_machines:
                return (
                    None,
                    None,
                    f"problem instance jobs[{job_id}][{task_id}] machine {machine} "
                    f"is outside range(0, {num_machines})",
                )
            if duration < 0:
                return (
                    None,
                    None,
                    f"problem instance jobs[{job_id}][{task_id}] duration {duration} is negative",
                )
            tasks.append((machine, duration))
        jobs.append(tasks)

    return jobs, num_machines, None


def _load_schedule(solution: object) -> tuple[list[dict[str, Any]] | None, list[str]]:
    if not isinstance(solution, dict):
        return None, ["solution is not a dict"]
    schedule = solution.get("schedule")
    if not isinstance(schedule, list):
        return None, ["solution.schedule must be a list"]

    errors: list[str] = []
    for i, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            errors.append(f"schedule[{i}] is not a dict")
            continue
        for key in ("job", "task", "machine", "start", "duration", "end"):
            if not _is_int(entry.get(key)):
                errors.append(f"schedule[{i}].{key} missing or not an int")
    return (None, errors) if errors else (schedule, errors)


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    jobs, num_machines, instance_error = _parse_instance(payload.get("problem"))
    if instance_error is not None:
        return {"status": "error", "errors": [instance_error], "details": {}}
    assert jobs is not None and num_machines is not None

    details: dict[str, Any] = {"num_jobs": len(jobs), "num_machines": num_machines}

    # Gate: the payload has to be a well-formed schedule CLAIM before grading it
    # against the instance means anything. A missing solver_status or a solution
    # that is not a list of int-valued entries is a serialization fault in the
    # producer, not an infeasible schedule, so it is an "error" -- see the
    # verdict split in the module docstring. "timeout" is included alongside
    # "optimal"/"feasible" because a timed-out run can still hand back a
    # well-formed recovered incumbent (see pyexec/eligibility.py's
    # DIAGNOSTIC_ACCEPT_STATUSES, which this set mirrors); it asserts no
    # optimality claim, so grading it is safe.
    protocol_errors: list[str] = []
    solver_status = payload.get("solver_status")
    if solver_status not in {"optimal", "feasible", "timeout"}:
        protocol_errors.append(
            f"solver_status is {solver_status!r}, expected optimal, feasible, or timeout"
        )

    schedule, schedule_errors = _load_schedule(payload.get("solution"))
    protocol_errors.extend(schedule_errors)

    if protocol_errors:
        return {"status": "error", "errors": protocol_errors, "details": details}
    assert schedule is not None

    errors: list[str] = []
    seen: set[tuple[int, int]] = set()
    by_machine: dict[int, list[tuple[int, int, int]]] = {}
    max_end = 0

    for entry in schedule:
        job_id, task_id = entry["job"], entry["task"]
        key = (job_id, task_id)
        if key in seen:
            errors.append(f"job {job_id} task {task_id} appears more than once")
            continue
        seen.add(key)

        if not (0 <= job_id < len(jobs)) or not (0 <= task_id < len(jobs[job_id])):
            errors.append(f"job {job_id} task {task_id} is out of range")
            continue

        expected_machine, expected_duration = jobs[job_id][task_id]
        start, duration, end, machine = (
            entry["start"],
            entry["duration"],
            entry["end"],
            entry["machine"],
        )
        if machine != expected_machine:
            errors.append(
                f"job {job_id} task {task_id} runs on machine {machine}, "
                f"expected {expected_machine}"
            )
        if duration != expected_duration:
            errors.append(
                f"job {job_id} task {task_id} has duration {duration}, expected {expected_duration}"
            )
        if start < 0 or end != start + duration:
            errors.append(f"job {job_id} task {task_id} has inconsistent start/duration/end")

        by_machine.setdefault(machine, []).append((start, end, job_id))
        max_end = max(max_end, end)

    missing = {
        (job_id, task_id) for job_id, job in enumerate(jobs) for task_id in range(len(job))
    } - seen
    if missing:
        errors.append(f"missing tasks: {sorted(missing)}")

    for job_id in range(len(jobs)):
        job_entries = sorted((e for e in schedule if e["job"] == job_id), key=lambda e: e["task"])
        for prev, nxt in zip(job_entries, job_entries[1:], strict=False):
            if nxt["start"] < prev["end"]:
                errors.append(
                    f"job {job_id} task {nxt['task']} starts before task {prev['task']} ends"
                )

    for machine, intervals in by_machine.items():
        intervals.sort()
        for (_start_a, end_a, job_a), (start_b, _end_b, job_b) in zip(
            intervals, intervals[1:], strict=False
        ):
            if start_b < end_a:
                errors.append(f"machine {machine} overlaps job {job_a} and job {job_b}")

    objective = payload.get("objective")
    if not isinstance(objective, int | float) or isinstance(objective, bool):
        errors.append(
            f"objective must be a number equal to the schedule makespan {max_end}, "
            f"got {objective!r}"
        )
    elif objective != max_end:
        errors.append(f"objective {objective} does not match schedule makespan {max_end}")

    status = "accepted" if not errors else "rejected"
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
        payload = json.load(payload_file)
    print(json.dumps(check_payload(payload)))


if __name__ == "__main__":
    main()
