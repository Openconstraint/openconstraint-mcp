from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_CHECKER_PATH = Path(__file__).parent.parent / "examples" / "job_shop" / "checker.py"
_DATA_DIR = Path(__file__).parent.parent / "examples" / "job_shop" / "data"


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("job_shop_checker", _CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _load_instance(name: str) -> tuple[str, list[list[tuple[int, int]]]]:
    """Read a data file and return (raw JSON text, parsed jobs list)."""
    text = (_DATA_DIR / name).read_text(encoding="utf-8")
    jobs = json.loads(text)["jobs"]
    return text, jobs


def _valid_schedule(
    jobs: list[list[tuple[int, int]]],
) -> tuple[list[dict[str, int]], int]:
    """A trivially feasible schedule: every task laid end-to-end on one global
    clock, so no machine overlaps and every job's tasks stay in order. The
    makespan equals the sum of all durations."""
    schedule: list[dict[str, int]] = []
    clock = 0
    for job_id, job in enumerate(jobs):
        for task_id, (machine, duration) in enumerate(job):
            schedule.append(
                {
                    "job": job_id,
                    "task": task_id,
                    "machine": machine,
                    "start": clock,
                    "duration": duration,
                    "end": clock + duration,
                }
            )
            clock += duration
    return schedule, clock


def _payload(
    problem: str | None, schedule: list[dict[str, int]], objective: object
) -> dict[str, Any]:
    return {
        "problem": problem,
        "solution": {"schedule": schedule},
        "objective": objective,
        "solver_status": "feasible",
    }


def test_accepts_valid_ft06_schedule() -> None:
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["status"] == "accepted", result["errors"]
    assert result["details"] == {"num_jobs": len(jobs), "num_machines": 6}


def test_accepts_valid_ft10_schedule() -> None:
    """Same checker, a different (larger) instance: the parity proof that the
    checker is instance-agnostic rather than hardcoded to ft06."""
    problem, jobs = _load_instance("data_ft10.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["status"] == "accepted", result["errors"]
    assert result["details"] == {"num_jobs": len(jobs), "num_machines": 10}


def _ft06_payload(objective: object) -> dict[str, Any]:
    problem, jobs = _load_instance("data_ft06.json")
    schedule, _ = _valid_schedule(jobs)
    return _payload(problem, schedule, objective)


_FT06_MAKESPAN = _valid_schedule(_load_instance("data_ft06.json")[1])[1]


@pytest.mark.parametrize("objective", [_FT06_MAKESPAN, float(_FT06_MAKESPAN)])
def test_accepts_objective_equal_to_makespan(objective: object) -> None:
    result = _checker.check_payload(_ft06_payload(objective))
    assert result["status"] == "accepted", result["errors"]


@pytest.mark.parametrize(
    "objective",
    [
        None,  # missing objective must not silently pass
        "55",  # numeric-looking string is still non-numeric
        "55.5",  # fractional string, likewise rejected
        _FT06_MAKESPAN + 0.9,  # fractional value that int() would truncate to a match
        True,  # bool is an int subclass but not a valid objective
        _FT06_MAKESPAN - 1,  # correct type, wrong value
    ],
)
def test_rejects_invalid_objective(objective: object) -> None:
    result = _checker.check_payload(_ft06_payload(objective))
    assert result["status"] == "rejected"


def test_rejects_tampered_schedule_wrong_machine() -> None:
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    schedule[0]["machine"] = (schedule[0]["machine"] + 1) % 6
    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["status"] == "rejected"


def test_rejects_schedule_entry_machine_out_of_range() -> None:
    """A machine id outside range(num_machines) must not raise a KeyError inside
    check_payload; by_machine.setdefault protects against it, and the verdict
    should be a normal "rejected" (invalid schedule), not "error"."""
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    schedule[0]["machine"] = 999
    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["status"] == "rejected"


def test_rejects_tampered_schedule_overlap() -> None:
    """ft06 already puts job 0's and job 2's FIRST tasks on the same machine, so
    sliding job 2's onto job 0's start time changes nothing but the timing: machine
    assignments stay correct, start/duration/end stay consistent, and the makespan is
    untouched. Being first tasks, neither has a predecessor whose end the precedence
    rule could catch. Asserting the exact error list keeps the overlap check the only
    thing standing between this schedule and `accepted`."""
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    first_of_job_0 = next(e for e in schedule if e["job"] == 0 and e["task"] == 0)
    first_of_job_2 = next(e for e in schedule if e["job"] == 2 and e["task"] == 0)
    # Guard the instance property the isolation depends on, so a swapped data file
    # makes this test fail loudly instead of quietly testing something weaker.
    assert first_of_job_0["machine"] == first_of_job_2["machine"]

    first_of_job_2["start"] = first_of_job_0["start"]
    first_of_job_2["end"] = first_of_job_2["start"] + first_of_job_2["duration"]

    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["errors"] == ["machine 2 overlaps job 0 and job 2"]


def test_missing_problem_yields_error_status() -> None:
    _, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(None, schedule, makespan))
    assert result["status"] == "error"


def test_unparseable_problem_yields_error_status() -> None:
    _, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload("not valid json", schedule, makespan))
    assert result["status"] == "error"


def test_instance_with_negative_duration_yields_error_status() -> None:
    """A malformed instance (negative task duration) is not valid ground truth,
    so the checker must not accept a schedule laid against it."""
    problem = json.dumps({"num_machines": 1, "jobs": [[[0, -1]]]})
    schedule = [{"job": 0, "task": 0, "machine": 0, "start": 0, "duration": -1, "end": -1}]
    result = _checker.check_payload(_payload(problem, schedule, 0))
    assert result["status"] == "error"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job", False),
        ("task", False),
        ("start", False),
        ("machine", True),
        ("duration", True),
        ("end", True),
    ],
)
def test_bool_in_schedule_field_yields_error_status(field: str, value: bool) -> None:
    """`bool` is an `int` subclass, so an unguarded `true`/`false` behaves as 1/0 in
    every downstream comparison, index, and sum — the type guard is the only place it
    can be caught. Each parameter substitutes a bool that is NUMERICALLY EQUAL to the
    correct value, so nothing but the bool exclusion itself can produce the verdict.
    A JSON boolean where an int belongs is a serialization fault in the producer, not
    an infeasible schedule, hence `error` rather than `rejected`."""
    problem = json.dumps({"num_machines": 2, "jobs": [[[1, 1]]]})
    entry: dict[str, Any] = {"job": 0, "task": 0, "machine": 1, "start": 0, "duration": 1, "end": 1}
    entry[field] = value
    result = _checker.check_payload(_payload(problem, [entry], 1))
    assert result["status"] == "error"


def test_missing_schedule_key_yields_error_status() -> None:
    """A solution with no `schedule` key cannot be graded at all. Reporting it as
    `rejected` would point the caller at the constraint model when the bug is in the
    model's print statement."""
    _, jobs = _load_instance("data_ft06.json")
    problem = (_DATA_DIR / "data_ft06.json").read_text(encoding="utf-8")
    result = _checker.check_payload(
        {
            "problem": problem,
            "solution": {"makespan": 55, "num_tasks": len(jobs)},
            "objective": 55,
            "solver_status": "optimal",
        }
    )
    assert result["status"] == "error"
    assert result["errors"] == ["solution.schedule must be a list"]


def test_solution_not_a_dict_yields_error_status() -> None:
    problem = json.dumps({"num_machines": 1, "jobs": [[[0, 3]]]})
    result = _checker.check_payload(
        {"problem": problem, "solution": [], "objective": 3, "solver_status": "optimal"}
    )
    assert result["status"] == "error"


def test_schedule_entry_missing_field_yields_error_status() -> None:
    problem = json.dumps({"num_machines": 1, "jobs": [[[0, 3]]]})
    entry: dict[str, int] = {"job": 0, "task": 0, "machine": 0, "start": 0, "duration": 3}
    result = _checker.check_payload(_payload(problem, [entry], 3))
    assert result["status"] == "error"


@pytest.mark.parametrize("solver_status", ["unknown", "infeasible", "error", None])
def test_unsolved_solver_status_yields_error_status(solver_status: object) -> None:
    """A status outside {optimal, feasible, timeout} does not claim a gradeable
    solution, so the checker cannot pronounce on feasibility. "timeout" is
    deliberately absent from this test's parametrize list: a timed-out run can
    still carry a well-formed recovered incumbent, which the checker CAN grade
    (see test_timeout_with_valid_schedule_is_accepted)."""
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    payload = _payload(problem, schedule, makespan)
    payload["solver_status"] = solver_status
    result = _checker.check_payload(payload)
    assert result["status"] == "error"


def test_timeout_with_valid_schedule_is_accepted() -> None:
    """A timeout with a recovered, well-formed schedule asserts no optimality claim,
    so the checker must grade it like any other feasible solution."""
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    payload = _payload(problem, schedule, makespan)
    payload["solver_status"] = "timeout"
    result = _checker.check_payload(payload)
    assert result["status"] == "accepted", result["errors"]


def test_timeout_with_infeasible_schedule_is_rejected() -> None:
    """Proves the checker actually grades a timeout payload rather than waving it
    through: an infeasible schedule under solver_status="timeout" must still be
    "rejected", not "accepted" or "error"."""
    problem, jobs = _load_instance("data_ft06.json")
    schedule, makespan = _valid_schedule(jobs)
    schedule[0]["machine"] = (schedule[0]["machine"] + 1) % 6
    payload = _payload(problem, schedule, makespan)
    payload["solver_status"] = "timeout"
    result = _checker.check_payload(payload)
    assert result["status"] == "rejected"


def test_error_verdict_carries_instance_details() -> None:
    """A protocol-gate error still reports which instance it tried to grade against,
    so the caller can tell "wrong output shape" from "wrong instance loaded"."""
    problem, jobs = _load_instance("data_ft06.json")
    result = _checker.check_payload(
        {"problem": problem, "solution": {}, "objective": 55, "solver_status": "optimal"}
    )
    assert result["details"] == {"num_jobs": len(jobs), "num_machines": 6}


def test_instance_with_no_jobs_yields_error_status() -> None:
    """An instance whose `jobs` list is empty would accept an empty schedule with
    objective 0 as trivially feasible — so a `problem` that lost its instance on the
    way in (serialization slip, truncation) would pass the checker gate. That is the
    one verdict a checker must never give, so an empty instance is an error."""
    problem = json.dumps({"request": "schedule the shop", "num_machines": 1, "jobs": []})
    result = _checker.check_payload(_payload(problem, [], 0))
    assert result["status"] == "error"


def test_instance_with_an_empty_job_yields_error_status() -> None:
    """A job with no tasks is likewise not solvable ground truth: model.py raises a
    KeyError selecting that job's final task, so the checker must not certify a
    schedule laid against an instance the model cannot solve."""
    problem = json.dumps({"num_machines": 1, "jobs": [[], [[0, 3]]]})
    schedule = [{"job": 1, "task": 0, "machine": 0, "start": 0, "duration": 3, "end": 3}]
    result = _checker.check_payload(_payload(problem, schedule, 3))
    assert result["status"] == "error"


def test_instance_with_machine_out_of_range_yields_error_status() -> None:
    """A task assigned to a machine id outside range(num_machines) makes the
    instance self-inconsistent, so it is an error, not an acceptable schedule."""
    problem = json.dumps({"num_machines": 1, "jobs": [[[999, 5]]]})
    schedule = [{"job": 0, "task": 0, "machine": 999, "start": 0, "duration": 5, "end": 5}]
    result = _checker.check_payload(_payload(problem, schedule, 5))
    assert result["status"] == "error"


@pytest.mark.parametrize("num_machines", [-1, 0])
def test_instance_with_non_positive_num_machines_yields_error_status(
    num_machines: int,
) -> None:
    """An empty job list runs the per-task machine-range check zero times, so
    num_machines has to be validated on its own — otherwise a malformed instance
    plus an empty schedule and objective 0 is trivially self-consistent."""
    problem = json.dumps({"num_machines": num_machines, "jobs": []})
    result = _checker.check_payload(_payload(problem, [], 0))
    assert result["status"] == "error"
