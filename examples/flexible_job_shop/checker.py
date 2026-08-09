"""Checker script for the flexible job shop models in this directory.

Validates that an emitted schedule is feasible for the FJSP instance supplied
via `payload["problem"]`: every task appears exactly once, its reported
(machine, duration) matches ONE OF ITS ELIGIBLE ALTERNATIVES, tasks within a
job run in order, no machine runs two tasks at once, and the reported
objective equals the schedule's makespan.

The eligible-alternative rule is the substantive difference from
../job_shop/checker.py, where each task has exactly one legal (machine,
duration) pair. Here the model CHOOSES, so the checker must accept any listed
alternative and reject an unlisted one -- catching both a bogus machine and a
machine paired with the wrong machine's duration.

`payload["problem"]` accepts EITHER form:
  - inline instance JSON (what ../job_shop/checker.py expects), or
  - a bare data filename, resolved next to this checker.
The filename form exists because the MCP tools carry `problem` as inline text
with no path variant, and data_behnke_lar04_1.json is 452 KB -- inlining it on
every call would be pure waste. Passing the name keeps `problem` an
INDEPENDENT channel: the model script does not get to tell the checker which
instance to grade it against.

THE FILENAME FORM REQUIRES A PATH-BASED CHECKER RUN -- `checker_path`, i.e.
`run_cpsat_python_file_checked` or `submit_cpsat_python_file_job`, which run
this file in place. Handing the same source to a tool that takes the checker as
INLINE TEXT (`run_cpsat_python`'s or `run_cpsat_python_experiment`'s `checker`)
copies it into a temporary directory, so `__file__` no longer sits next to the
data files and every bare filename resolves to nothing. Inline-checker callers
must inline the instance JSON too; the resolver's not-found error says so.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str). The checker admits solver_status in {"optimal",
  "feasible", "timeout"} -- mirroring pyexec/eligibility.py's
  DIAGNOSTIC_ACCEPT_STATUSES -- and treats every other value as ungradeable.
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}
- "accepted" with an empty errors list is the only passing verdict.

The two failing verdicts split by WHAT failed: "error" means the payload could
not be graded at all -- an unusable instance, or a solution/solver_status that
is not a well-formed schedule claim -- while "rejected" means a well-formed
schedule WAS graded against the instance and violates it. ../job_shop/checker.py
explains why the caller needs them kept apart.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# job -> task -> list of (machine, duration) alternatives
Jobs = list[list[list[tuple[int, int]]]]


def _is_int(value: object) -> bool:
    """True only for a genuine int. `bool` is an `int` subclass in Python, so a JSON
    `true`/`false` left unguarded would sail through every downstream check as 1/0:
    it indexes, compares, and adds identically. This guard is the only place such a
    value can be caught."""
    return isinstance(value, int) and not isinstance(value, bool)


def _resolve_problem(problem: object) -> tuple[dict[str, Any] | None, str, str | None]:
    """Turn payload["problem"] into an instance dict.

    Returns (instance, source_label, error). Inline JSON is used directly; any
    other string is treated as a data filename resolved next to this checker.
    A filename containing a path separator is rejected rather than followed --
    the payload names one of this directory's data files, not an arbitrary path.

    Resolving next to `__file__` only reaches the data files when this checker
    runs IN PLACE (`checker_path`); an inline-checker tool copies the source to
    a temp directory, where the lookup necessarily fails. That is the likeliest
    cause of a not-found error, so the message names it.
    """
    if not isinstance(problem, str):
        return None, "", "payload.problem is missing or not a string"

    text = problem.strip()
    if not text:
        return None, "", "payload.problem is empty"

    if text.startswith("{"):
        try:
            instance = json.loads(text)
        except json.JSONDecodeError as exc:
            return None, "", f"payload.problem is not valid JSON: {exc}"
        if not isinstance(instance, dict):
            return None, "", "payload.problem JSON is not an object"
        return instance, "inline", None

    if "/" in text or "\\" in text or text in {".", ".."}:
        return None, "", f"payload.problem filename {text!r} must be a bare filename"

    path = Path(__file__).parent / text
    if not path.is_file():
        return (
            None,
            "",
            f"payload.problem names {text!r}, which is not a file in {path.parent}. "
            "The filename form needs this checker to run from its own directory "
            "(checker_path); an inline checker is copied to a temp directory, so "
            "pass the instance JSON inline instead.",
        )
    try:
        instance = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return None, "", f"payload.problem file {text!r} is not valid JSON: {exc}"
    if not isinstance(instance, dict):
        return None, "", f"payload.problem file {text!r} does not contain a JSON object"
    return instance, f"file:{text}", None


def _parse_instance(instance: dict[str, Any]) -> tuple[Jobs | None, int | None, str | None]:
    """Parse and validate the FJSP instance. Returns (jobs, num_machines, error)."""
    num_machines = instance.get("num_machines")
    if not _is_int(num_machines):
        return None, None, "problem instance num_machines missing or not an int"
    if num_machines < 1:
        return None, None, f"problem instance num_machines {num_machines} is not positive"

    raw_jobs = instance.get("jobs")
    if not isinstance(raw_jobs, list):
        return None, None, "problem instance jobs missing or not a list"
    # An instance with no tasks would accept an empty schedule with objective 0 as
    # trivially feasible, turning a serialization slip into a passing verdict.
    if not raw_jobs:
        return None, None, "problem instance jobs is empty"

    jobs: Jobs = []
    for job_id, raw_job in enumerate(raw_jobs):
        if not isinstance(raw_job, list):
            return None, None, f"problem instance jobs[{job_id}] is not a list"
        if not raw_job:
            return None, None, f"problem instance jobs[{job_id}] has no tasks"
        tasks: list[list[tuple[int, int]]] = []
        for task_id, raw_task in enumerate(raw_job):
            where = f"problem instance jobs[{job_id}][{task_id}]"
            if not isinstance(raw_task, list) or not raw_task:
                return None, None, f"{where} is not a non-empty list of alternatives"
            alternatives: list[tuple[int, int]] = []
            for alt_id, raw_alt in enumerate(raw_task):
                if (
                    not isinstance(raw_alt, list)
                    or len(raw_alt) != 2
                    or not all(_is_int(v) for v in raw_alt)
                ):
                    return (
                        None,
                        None,
                        f"{where}[{alt_id}] is not a [machine, duration] pair of ints",
                    )
                machine, duration = raw_alt[0], raw_alt[1]
                if not 0 <= machine < num_machines:
                    return (
                        None,
                        None,
                        f"{where}[{alt_id}] machine {machine} is outside range(0, {num_machines})",
                    )
                if duration < 0:
                    return None, None, f"{where}[{alt_id}] duration {duration} is negative"
                alternatives.append((machine, duration))
            tasks.append(alternatives)
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
    instance, source, resolve_error = _resolve_problem(payload.get("problem"))
    if resolve_error is not None:
        return {"status": "error", "errors": [resolve_error], "details": {}}
    assert instance is not None

    jobs, num_machines, instance_error = _parse_instance(instance)
    if instance_error is not None:
        return {"status": "error", "errors": [instance_error], "details": {}}
    assert jobs is not None and num_machines is not None

    details: dict[str, Any] = {
        "instance_source": source,
        "num_jobs": len(jobs),
        "num_machines": num_machines,
        "num_tasks": sum(len(job) for job in jobs),
    }

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

        start, duration, end, machine = (
            entry["start"],
            entry["duration"],
            entry["end"],
            entry["machine"],
        )
        # The FJSP rule: the pair must be one the instance actually offers.
        alternatives = jobs[job_id][task_id]
        if (machine, duration) not in alternatives:
            errors.append(
                f"job {job_id} task {task_id} uses (machine {machine}, "
                f"duration {duration}), which is not among its alternatives "
                f"{[list(a) for a in alternatives]}"
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
