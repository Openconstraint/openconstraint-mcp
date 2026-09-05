from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_CHECKER_PATH = Path(__file__).parent.parent / "examples" / "flexible_job_shop" / "checker.py"
_DATA_DIR = Path(__file__).parent.parent / "examples" / "flexible_job_shop" / "data"


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("flexible_job_shop_checker", _CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()

# job -> task -> list of [machine, duration] alternatives
Jobs = list[list[list[list[int]]]]


def _load_instance(name: str) -> tuple[str, Jobs]:
    """Read a data file and return (raw JSON text, parsed jobs list)."""
    text = (_DATA_DIR / name).read_text(encoding="utf-8")
    return text, json.loads(text)["jobs"]


def _valid_schedule(jobs: Jobs, alt_index: int = 0) -> tuple[list[dict[str, int]], int]:
    """A trivially feasible schedule: every task takes its `alt_index`-th eligible
    alternative (clamped for tasks with fewer) and is laid end-to-end on one global
    clock. No machine overlaps, every job's tasks stay in order, and the makespan is
    the sum of the chosen durations."""
    schedule: list[dict[str, int]] = []
    clock = 0
    for job_id, job in enumerate(jobs):
        for task_id, alternatives in enumerate(job):
            machine, duration = alternatives[min(alt_index, len(alternatives) - 1)]
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
    problem: str | None,
    schedule: list[dict[str, int]],
    objective: object,
    solver_status: str = "feasible",
) -> dict[str, Any]:
    return {
        "problem": problem,
        "solution": {"schedule": schedule},
        "objective": objective,
        "solver_status": solver_status,
    }


def _instance(num_machines: int, jobs: Jobs) -> str:
    return json.dumps({"num_machines": num_machines, "jobs": jobs})


# --------------------------------------------------------------------------
# accepted
# --------------------------------------------------------------------------


def test_accepts_valid_mk01_schedule() -> None:
    problem, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(problem, schedule, makespan))
    assert result["status"] == "accepted", result["errors"]


def test_accepts_instance_named_by_bare_filename() -> None:
    """The filename form of payload["problem"] resolves next to the checker, so a
    452 KB instance need not be inlined on every call."""
    _, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload("data_mk01.json", schedule, makespan))
    assert result["details"]["instance_source"] == "file:data_mk01.json"


def test_accepts_a_non_first_alternative() -> None:
    """The FJSP rule cuts both ways: any LISTED alternative is legal, not just the
    first one, so a model that routes a task to its second machine is accepted."""
    problem = _instance(2, [[[[0, 5], [1, 7]]]])
    schedule = [{"job": 0, "task": 0, "machine": 1, "start": 0, "duration": 7, "end": 7}]
    result = _checker.check_payload(_payload(problem, schedule, 7))
    assert result["status"] == "accepted", result["errors"]


# --------------------------------------------------------------------------
# rejected — a well-formed schedule that was graded and violates the instance
# --------------------------------------------------------------------------


def test_rejects_machine_not_among_alternatives() -> None:
    problem = _instance(3, [[[[0, 5], [1, 7]]]])
    schedule = [{"job": 0, "task": 0, "machine": 2, "start": 0, "duration": 5, "end": 5}]
    result = _checker.check_payload(_payload(problem, schedule, 5))
    assert result["status"] == "rejected"


def test_rejects_eligible_machine_paired_with_another_machines_duration() -> None:
    """The substantive FJSP check: machine 0 is eligible and duration 7 is a real
    duration in this task's alternatives, but the PAIR (0, 7) is not offered.
    Checking machine and duration independently would let this through."""
    problem = _instance(2, [[[[0, 5], [1, 7]]]])
    schedule = [{"job": 0, "task": 0, "machine": 0, "start": 0, "duration": 7, "end": 7}]
    result = _checker.check_payload(_payload(problem, schedule, 7))
    assert result["status"] == "rejected"


def test_rejects_machine_overlap() -> None:
    """Two single-task jobs share the only machine; starting both at 0 keeps every
    other property valid (durations legal, start/end consistent, makespan matches),
    so the overlap check is the only thing standing between this and `accepted`."""
    problem = _instance(1, [[[[0, 3]]], [[[0, 3]]]])
    schedule = [
        {"job": 0, "task": 0, "machine": 0, "start": 0, "duration": 3, "end": 3},
        {"job": 1, "task": 0, "machine": 0, "start": 0, "duration": 3, "end": 3},
    ]
    result = _checker.check_payload(_payload(problem, schedule, 3))
    assert result["errors"] == ["machine 0 overlaps job 0 and job 1"]


def test_rejects_precedence_violation() -> None:
    problem = _instance(2, [[[[0, 3]], [[1, 3]]]])
    schedule = [
        {"job": 0, "task": 0, "machine": 0, "start": 3, "duration": 3, "end": 6},
        {"job": 0, "task": 1, "machine": 1, "start": 0, "duration": 3, "end": 3},
    ]
    result = _checker.check_payload(_payload(problem, schedule, 6))
    assert result["status"] == "rejected"


def test_rejects_objective_not_equal_to_makespan() -> None:
    problem, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(problem, schedule, makespan - 1))
    assert result["status"] == "rejected"


def test_rejects_missing_task() -> None:
    problem, jobs = _load_instance("data_mk01.json")
    schedule, _ = _valid_schedule(jobs)
    dropped = schedule.pop()
    result = _checker.check_payload(_payload(problem, schedule, dropped["start"]))
    assert result["status"] == "rejected"


def test_rejects_duplicate_task() -> None:
    problem = _instance(1, [[[[0, 3]]]])
    entry = {"job": 0, "task": 0, "machine": 0, "start": 0, "duration": 3, "end": 3}
    result = _checker.check_payload(_payload(problem, [entry, dict(entry)], 3))
    assert result["status"] == "rejected"


# --------------------------------------------------------------------------
# error — the payload could not be graded at all
# --------------------------------------------------------------------------


def test_compact_summary_solution_yields_error_status() -> None:
    """The regression this split exists for. A producer whose `solution` DESCRIBES
    the schedule (makespan, task count, a pointer to a saved file) instead of
    CONTAINING it hands the checker nothing to grade — the shape every model in this
    directory emitted before they were changed to print the schedule itself. That is
    a serialization mismatch in the producer, not an infeasible schedule; reporting
    it as `rejected` points the caller at the constraint model and invites it to
    "fix" scheduling logic that was never wrong."""
    payload = {
        "problem": "data_mk01.json",
        "solution": {
            "makespan": 40,
            "num_tasks": 55,
            "instance": "data_mk01.json",
            "result_file": "results/direct_optional_intervals__data_mk01.json",
        },
        "objective": 40,
        "solver_status": "optimal",
    }
    result = _checker.check_payload(payload)
    assert result["status"] == "error"
    assert result["errors"] == ["solution.schedule must be a list"]


def test_solution_not_a_dict_yields_error_status() -> None:
    result = _checker.check_payload(
        {
            "problem": _instance(1, [[[[0, 3]]]]),
            "solution": [],
            "objective": 3,
            "solver_status": "optimal",
        }
    )
    assert result["status"] == "error"


def test_schedule_entry_missing_field_yields_error_status() -> None:
    problem = _instance(1, [[[[0, 3]]]])
    entry: dict[str, int] = {"job": 0, "task": 0, "machine": 0, "start": 0, "duration": 3}
    result = _checker.check_payload(_payload(problem, [entry], 3))
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
    can be caught. Each parameter substitutes a bool NUMERICALLY EQUAL to the correct
    value, so nothing but the bool exclusion itself can produce the verdict. A JSON
    boolean where an int belongs is a serialization fault, hence `error`."""
    problem = _instance(2, [[[[1, 1]]]])
    entry: dict[str, Any] = {"job": 0, "task": 0, "machine": 1, "start": 0, "duration": 1, "end": 1}
    entry[field] = value
    result = _checker.check_payload(_payload(problem, [entry], 1))
    assert result["status"] == "error"


@pytest.mark.parametrize("solver_status", ["unknown", "infeasible", "error", None])
def test_unsolved_solver_status_yields_error_status(solver_status: object) -> None:
    """A status outside {optimal, feasible, timeout} does not claim a gradeable
    solution, so the checker cannot pronounce on feasibility. "timeout" is
    deliberately absent from this test's parametrize list: a timed-out run can
    still carry a well-formed recovered incumbent, which the checker CAN grade
    (see test_timeout_with_valid_schedule_is_accepted)."""
    problem, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    payload = _payload(problem, schedule, makespan)
    payload["solver_status"] = solver_status
    result = _checker.check_payload(payload)
    assert result["status"] == "error"


def test_timeout_with_valid_schedule_is_accepted() -> None:
    """A timeout with a recovered, well-formed schedule asserts no optimality claim,
    so the checker must grade it like any other feasible solution."""
    problem, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(problem, schedule, makespan, "timeout"))
    assert result["status"] == "accepted", result["errors"]


def test_timeout_with_infeasible_schedule_is_rejected() -> None:
    """Proves the checker actually grades a timeout payload rather than waving it
    through: an infeasible schedule under solver_status="timeout" must still be
    "rejected", not "accepted" or "error"."""
    problem = _instance(3, [[[[0, 5], [1, 7]]]])
    schedule = [{"job": 0, "task": 0, "machine": 2, "start": 0, "duration": 5, "end": 5}]
    result = _checker.check_payload(_payload(problem, schedule, 5, "timeout"))
    assert result["status"] == "rejected"


def test_error_verdict_carries_instance_details() -> None:
    """A protocol-gate error still reports which instance it tried to grade against,
    so the caller can tell "wrong output shape" from "wrong instance loaded"."""
    result = _checker.check_payload(
        {
            "problem": "data_mk01.json",
            "solution": {},
            "objective": 40,
            "solver_status": "optimal",
        }
    )
    assert result["details"] == {
        "instance_source": "file:data_mk01.json",
        "num_jobs": 10,
        "num_machines": 6,
        "num_tasks": 55,
    }


def test_missing_problem_yields_error_status() -> None:
    _, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload(None, schedule, makespan))
    assert result["status"] == "error"


def test_problem_with_path_separator_yields_error_status() -> None:
    """A filename is resolved next to the checker; a path is refused rather than
    followed, so the payload cannot reach an arbitrary file."""
    _, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = _checker.check_payload(_payload("../job_shop/data_ft06.json", schedule, makespan))
    assert result["status"] == "error"


def test_bare_filename_from_a_copied_checker_names_the_path_based_requirement(
    tmp_path: Path,
) -> None:
    """The filename form resolves next to `__file__`, so it only works when the
    checker runs IN PLACE. `run_cpsat_python`/`run_cpsat_python_experiment` take the
    checker as inline text and copy it to a temp directory, where no data file is a
    sibling -- the case the other filename tests miss by importing the repository
    file. Loading a COPY reproduces that execution context, and the resulting error
    must point the caller at `checker_path` rather than merely reporting a missing
    file, since the filename is right and only the run mode is wrong."""
    copied = tmp_path / "checker.py"
    copied.write_text(_CHECKER_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    spec = importlib.util.spec_from_file_location("copied_fjs_checker", copied)
    assert spec is not None and spec.loader is not None
    copied_checker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(copied_checker)

    _, jobs = _load_instance("data_mk01.json")
    schedule, makespan = _valid_schedule(jobs)
    result = copied_checker.check_payload(_payload("data_mk01.json", schedule, makespan))

    assert result["status"] == "error"
    assert "checker_path" in result["errors"][0]


def test_alternative_pair_of_wrong_arity_yields_error_status() -> None:
    """A malformed alternative is not valid ground truth, so no schedule laid
    against it can be certified."""
    result = _checker.check_payload(_payload(_instance(1, [[[[0, 3, 9]]]]), [], 0))
    assert result["status"] == "error"


def test_instance_with_no_jobs_yields_error_status() -> None:
    """An empty instance would accept an empty schedule with objective 0 as trivially
    feasible, turning a serialization slip into a passing verdict."""
    result = _checker.check_payload(_payload(_instance(1, []), [], 0))
    assert result["status"] == "error"


def test_instance_with_alternative_machine_out_of_range_yields_error_status() -> None:
    result = _checker.check_payload(_payload(_instance(1, [[[[999, 5]]]]), [], 0))
    assert result["status"] == "error"
