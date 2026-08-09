from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.cpsat import (
    CPSAT_MUTATION_NAMES,
    CpsatCheckerReport,
    CpsatCheckerTestReport,
    CpsatExpectation,
    CpsatMutationOutcome,
    CpsatPythonCheckedResult,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    CpsatStatus,
    SaveVerifiedPythonResult,
    cpsat_job_state_for_result,
)

# --- CpsatPythonJobStatus + cpsat_job_state_for_result ----------------------


def _cpsat_result(status: CpsatStatus, *, timed_out: bool = False) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution={"x": 1} if status not in ("error", "infeasible", "unknown", "timeout") else None,
        objective=None,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=False,
        duration_ms=10,
    )


@pytest.mark.parametrize(
    ("status", "timed_out", "expected"),
    [
        ("optimal", False, "succeeded"),
        ("feasible", False, "succeeded"),
        ("infeasible", False, "succeeded"),
        ("unknown", False, "succeeded"),
        # The load-bearing case: error → succeeded (a structured verdict, not a
        # job-machinery failure — D3 / schemas.py:144-159 analogue).
        ("error", False, "succeeded"),
        ("timeout", False, "timeout"),
        # timed_out flag overrides status → always timeout
        ("unknown", True, "timeout"),
    ],
)
def test_cpsat_job_state_for_result_maps_every_status(
    status: CpsatStatus, timed_out: bool, expected: str
) -> None:
    assert cpsat_job_state_for_result(_cpsat_result(status, timed_out=timed_out)) == expected


def test_cpsat_job_state_for_result_error_maps_to_succeeded_not_failed() -> None:
    # Explicit assertion for the D3 semantic: error → succeeded.
    result = _cpsat_result("error")
    assert cpsat_job_state_for_result(result) == "succeeded"


def test_cpsat_job_state_for_result_timeout_maps_to_timeout() -> None:
    result = _cpsat_result("timeout")
    assert cpsat_job_state_for_result(result) == "timeout"


def test_cpsat_python_job_status_succeeded_round_trips_with_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj1",
        state="succeeded",
        script_timeout_ms=30000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1050,
        elapsed_ms=49,
        result=_cpsat_result("optimal"),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["status"] == "optimal"
    assert dumped["message"] is None


def test_cpsat_python_job_status_queued_serializes_without_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-q", state="queued", script_timeout_ms=30000, submitted_at_ms=5
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "queued"
    assert dumped["result"] is None


def test_cpsat_python_job_status_rejects_running_carrying_a_result() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonJobStatus(
            job_id="cj-bad",
            state="running",
            script_timeout_ms=30000,
            submitted_at_ms=1,
            result=_cpsat_result("optimal"),
        )


def test_cpsat_python_job_status_rejects_succeeded_without_a_result() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonJobStatus(
            job_id="cj-bad2",
            state="succeeded",
            script_timeout_ms=30000,
            submitted_at_ms=1,
        )


def test_cpsat_python_job_status_timeout_carries_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-to",
        state="timeout",
        script_timeout_ms=5000,
        submitted_at_ms=1,
        result=_cpsat_result("timeout", timed_out=True),
    )
    assert status.result is not None
    assert status.result.timed_out is True


def test_cpsat_python_job_status_cancelled_has_no_result() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-c",
        state="cancelled",
        script_timeout_ms=5000,
        submitted_at_ms=1,
        message="Cancelled by client",
    )
    assert status.result is None
    assert status.message == "Cancelled by client"


def _job_checker_report(status: str = "accepted") -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def test_cpsat_python_job_status_succeeded_carries_checker_report() -> None:
    status = CpsatPythonJobStatus(
        job_id="cj-ck",
        state="succeeded",
        script_timeout_ms=30000,
        submitted_at_ms=1,
        result=_cpsat_result("optimal"),
        checker=_job_checker_report(),
        checker_timeout_ms=30000,
    )
    assert status.checker is not None
    assert status.checker.status == "accepted"


def test_cpsat_python_job_status_rejects_checker_and_skipped_reason_together() -> None:
    with pytest.raises(ValidationError, match="mutually"):
        CpsatPythonJobStatus(
            job_id="cj-ck-bad",
            state="succeeded",
            script_timeout_ms=30000,
            submitted_at_ms=1,
            result=_cpsat_result("optimal"),
            checker=_job_checker_report(),
            checker_skipped_reason="status='infeasible'",
        )


@pytest.mark.parametrize("state", ["queued", "running", "failed", "cancelled"])
def test_cpsat_python_job_status_rejects_checker_on_non_result_bearing_state(
    state: str,
) -> None:
    with pytest.raises(ValidationError, match="checker"):
        CpsatPythonJobStatus(
            job_id="cj-ck-bad2",
            state=state,  # type: ignore[arg-type]
            script_timeout_ms=30000,
            submitted_at_ms=1,
            checker=_job_checker_report(),
        )


@pytest.mark.parametrize("state", ["queued", "running", "failed", "cancelled"])
def test_cpsat_python_job_status_rejects_skipped_reason_on_non_result_bearing_state(
    state: str,
) -> None:
    with pytest.raises(ValidationError, match="checker"):
        CpsatPythonJobStatus(
            job_id="cj-ck-bad3",
            state=state,  # type: ignore[arg-type]
            script_timeout_ms=30000,
            submitted_at_ms=1,
            checker_skipped_reason="solution is missing or empty",
        )


def test_cpsat_python_job_status_checker_timeout_echo_allowed_while_running() -> None:
    # checker_timeout_ms is a request echo like script_timeout_ms: constant across
    # states, present even before any checker outcome exists.
    status = CpsatPythonJobStatus(
        job_id="cj-ck-run",
        state="running",
        script_timeout_ms=30000,
        submitted_at_ms=1,
        checker_timeout_ms=7000,
    )
    assert status.checker_timeout_ms == 7000


# --- CpsatExpectation schemas -----------------------------------------------


def test_cpsat_expectation_maximize_with_int_threshold() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100)
    assert exp.objective_sense == "maximize"
    assert exp.objective_threshold == 100.0


def test_cpsat_expectation_minimize_with_float_threshold() -> None:
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=3.14)
    assert exp.objective_sense == "minimize"
    assert exp.objective_threshold == 3.14


def test_cpsat_expectation_accepts_zero_threshold() -> None:
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=0)
    assert exp.objective_threshold == 0.0


def test_cpsat_expectation_accepts_negative_threshold() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=-50)
    assert exp.objective_threshold == -50.0


def test_cpsat_expectation_rejects_true() -> None:
    with pytest.raises(ValidationError, match="bool"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=True)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_false() -> None:
    with pytest.raises(ValidationError, match="bool"):
        CpsatExpectation(objective_sense="minimize", objective_threshold=False)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_nan() -> None:
    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=math.nan)


def test_cpsat_expectation_rejects_positive_inf() -> None:
    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="maximize", objective_threshold=math.inf)


def test_cpsat_expectation_rejects_negative_inf() -> None:
    with pytest.raises(ValidationError, match="finite"):
        CpsatExpectation(objective_sense="minimize", objective_threshold=-math.inf)


def test_cpsat_expectation_rejects_unknown_sense() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="unknown", objective_threshold=10.0)  # type: ignore[arg-type]


def test_cpsat_expectation_rejects_missing_threshold() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="maximize")  # type: ignore[call-arg]


def test_cpsat_expectation_rejects_null_threshold() -> None:
    with pytest.raises(ValidationError):
        CpsatExpectation(objective_sense="maximize", objective_threshold=None)  # type: ignore[arg-type]


# --- CpsatCheckerReport schemas ----------------------------------------------


def _make_checker_report(**overrides: object) -> CpsatCheckerReport:
    defaults: dict = {
        "status": "accepted",
        "errors": [],
        "details": None,
        "stdout": "",
        "stderr": "",
        "duration_ms": 42,
        "timed_out": False,
        "truncated": False,
    }
    defaults.update(overrides)
    return CpsatCheckerReport(**defaults)  # type: ignore[arg-type]


def test_cpsat_checker_report_accepted_round_trips() -> None:
    report = _make_checker_report(status="accepted", errors=[])
    dumped = report.model_dump()
    assert dumped["status"] == "accepted"
    assert dumped["errors"] == []
    assert dumped["timed_out"] is False
    assert dumped["truncated"] is False


def test_cpsat_checker_report_rejected_with_errors_round_trips() -> None:
    report = _make_checker_report(
        status="rejected",
        errors=["golfer 3 appears twice in week 1"],
        duration_ms=15,
    )
    dumped = report.model_dump()
    assert dumped["status"] == "rejected"
    assert dumped["errors"] == ["golfer 3 appears twice in week 1"]


def test_cpsat_checker_report_error_round_trips() -> None:
    report = _make_checker_report(status="error", errors=["malformed checker output"])
    assert report.status == "error"


def test_cpsat_checker_report_timeout_round_trips() -> None:
    report = _make_checker_report(status="timeout", errors=[], timed_out=True)
    assert report.status == "timeout"
    assert report.timed_out is True


def test_cpsat_checker_report_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        _make_checker_report(status="passed")  # type: ignore[arg-type]


def test_cpsat_checker_report_with_details_round_trips() -> None:
    report = _make_checker_report(
        status="rejected",
        errors=["constraint violated"],
        details={"week": 1, "pair": [1, 2]},
    )
    assert report.details == {"week": 1, "pair": [1, 2]}


# --- SaveVerifiedPythonResult schemas ----------------------------------------


def _make_save_python_result(**overrides: object) -> SaveVerifiedPythonResult:
    defaults: dict = {
        "status": "optimal",
        "target_dir": None,
        "reason": "status=infeasible",
        "solution": None,
        "objective": None,
        "stdout": "",
        "stderr": "",
        "timed_out": False,
        "truncated": False,
        "duration_ms": 10,
    }
    defaults.update(overrides)
    return SaveVerifiedPythonResult(**defaults)  # type: ignore[arg-type]


def test_save_verified_python_result_defaults_to_none_verification() -> None:
    result = _make_save_python_result()
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation is None
    assert result.expectation_passed is None
    assert result.checker is None


def test_save_verified_python_result_saved_computed_from_reason_and_dir() -> None:
    saved = _make_save_python_result(
        target_dir="/tmp/x",
        reason=None,
        verification_level="reported",
        reported_passed=True,
    )
    not_saved = _make_save_python_result(target_dir=None, reason="status=infeasible")
    assert saved.saved is True
    assert not_saved.saved is False


def test_save_verified_python_result_with_expectation_echoed() -> None:
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)
    result = _make_save_python_result(
        target_dir="/tmp/x",
        reason=None,
        verification_level="expectation",
        reported_passed=True,
        expectation=exp,
        expectation_passed=True,
    )
    assert result.expectation is not None
    assert result.expectation.objective_sense == "maximize"
    assert result.expectation_passed is True


# --- CpsatPythonExperimentResult ---------------------------------------------


def _experiment_winner_result() -> CpsatPythonResult:
    return CpsatPythonResult(
        status="optimal",
        solution={"x": 3},
        objective=3.0,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=5,
    )


def _experiment_attempt_row(**overrides: object) -> CpsatPythonExperimentAttemptResult:
    defaults: dict[str, object] = {
        "index": 0,
        "name": "attempt-0",
        "seed": None,
        "config_sha256": None,
        "source_sha256": "hash0",
        "script_timeout_ms": 5000,
        "status": "optimal",
        "objective": 3.0,
        "accepted": True,
        "checker_status": None,
        "message": None,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 5,
    }
    defaults.update(overrides)
    return CpsatPythonExperimentAttemptResult(**defaults)  # type: ignore[arg-type]


def test_experiment_attempt_result_stderr_tail_defaults_to_none() -> None:
    row = _experiment_attempt_row()
    assert row.stderr_tail is None


def test_experiment_attempt_result_stderr_tail_round_trips() -> None:
    row = _experiment_attempt_row(status="error", accepted=False, stderr_tail="Traceback: boom")
    dumped = row.model_dump()
    assert dumped["stderr_tail"] == "Traceback: boom"


def test_cpsat_python_experiment_result_winner_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "winner"
    assert dumped["winner_name"] == "attempt-0"
    assert dumped["winner_index"] == 0


def test_cpsat_python_experiment_result_warnings_defaults_to_empty_list() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    assert result.warnings == []


def test_cpsat_python_experiment_result_warnings_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="attempt-0",
        winner=_experiment_winner_result(),
        attempts=[_experiment_attempt_row()],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
        warnings=["some warning"],
    )
    dumped = result.model_dump(mode="json")
    assert dumped["warnings"] == ["some warning"]


def test_cpsat_python_experiment_result_no_winner_round_trips() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[_experiment_attempt_row(accepted=False, status="infeasible", objective=None)],
        elapsed_ms=10,
        objective_sense="minimize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["hash0"],
        checker_sha256=None,
        problem_sha256=None,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "no_winner"
    assert dumped["winner_index"] is None
    assert dumped["winner_name"] is None
    assert dumped["winner"] is None


def test_cpsat_python_experiment_result_rejects_winner_status_without_a_winner() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=None,
            winner_name=None,
            winner=None,
            attempts=[],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=[],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_no_winner_carrying_a_winner() -> None:
    with pytest.raises(ValidationError):
        CpsatPythonExperimentResult(
            status="no_winner",
            winner_index=0,
            winner_name="attempt-0",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row()],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_out_of_range_winner_index() -> None:
    with pytest.raises(ValidationError, match="winner_index"):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=1,
            winner_name="attempt-0",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row()],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )


def test_cpsat_python_experiment_result_rejects_winner_name_mismatch() -> None:
    with pytest.raises(ValidationError, match="winner_name"):
        CpsatPythonExperimentResult(
            status="winner",
            winner_index=0,
            winner_name="not-the-right-name",
            winner=_experiment_winner_result(),
            attempts=[_experiment_attempt_row(name="attempt-0")],
            elapsed_ms=1,
            objective_sense="minimize",
            selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
            source_sha256=["hash0"],
            checker_sha256=None,
            problem_sha256=None,
        )


# --- CpsatPythonCheckedResult -----------------------------------------------


def _checked_kwargs() -> dict:
    return {
        "status": "optimal",
        "solution": {"x": 1},
        "objective": 1,
        "stdout": "",
        "stderr": "",
        "return_code": 0,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 5,
        "checker_timeout_ms": 30_000,
    }


def test_cpsat_python_checked_result_rejects_checker_and_skipped_reason_together() -> None:
    report = CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=False,
        truncated=False,
    )
    with pytest.raises(ValidationError, match="mutually exclusive"):
        CpsatPythonCheckedResult(
            **_checked_kwargs(), checker=report, checker_skipped_reason="status='infeasible'"
        )


def test_cpsat_python_checked_result_rejects_neither_checker_nor_skipped_reason() -> None:
    """This tool always requests a check, so a result with no outcome is malformed."""
    with pytest.raises(ValidationError, match="requires checker or checker_skipped_reason"):
        CpsatPythonCheckedResult(**_checked_kwargs())


def test_cpsat_python_checked_result_allows_a_skipped_checker() -> None:
    result = CpsatPythonCheckedResult(
        **_checked_kwargs(), checker_skipped_reason="solution is missing or empty"
    )
    assert result.checker is None


# --- CpsatMutationOutcome / CpsatCheckerTestReport --------------------------


def _mutation_report(status: str) -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[] if status == "accepted" else ["corrupted"],
        stdout="",
        stderr="",
        duration_ms=3,
        timed_out=status == "timeout",
        truncated=False,
    )


def test_cpsat_mutation_outcome_graded_round_trips() -> None:
    outcome = CpsatMutationOutcome(
        name="element_dropped", status="rejected", errors=["corrupted"], duration_ms=3
    )
    dumped = outcome.model_dump()
    assert dumped["name"] == "element_dropped"
    assert dumped["status"] == "rejected"


def test_cpsat_mutation_outcome_carries_no_raw_checker_output() -> None:
    # Up to four mutants run per checked call, each a checker child whose
    # stdout+stderr share a 1 MiB cap and whose `details` is arbitrary JSON.
    # Carrying those would let one tool result flood a client's context; the
    # verdict plus its errors is the whole signal the probe exists to report.
    bulky = {"stdout", "stderr", "details", "report"}

    assert bulky.isdisjoint(CpsatMutationOutcome.model_fields)


def test_cpsat_mutation_outcome_carries_no_separate_applied_field() -> None:
    # A row ran IFF it carries a `status`; a third representation of that
    # two-state fact could only ever restate it or contradict it.
    assert "applied" not in CpsatMutationOutcome.model_fields


def test_cpsat_mutation_outcome_name_schema_lists_fixed_names() -> None:
    schema = CpsatMutationOutcome.model_json_schema()

    assert schema["properties"]["name"]["enum"] == list(CPSAT_MUTATION_NAMES)


def test_cpsat_mutation_outcome_skipped_round_trips() -> None:
    outcome = CpsatMutationOutcome(
        name="objective_perturbed",
        skipped_reason="the run reported no objective",
    )
    assert outcome.model_dump()["skipped_reason"] == "the run reported no objective"


def test_cpsat_mutation_outcome_skipped_omits_the_verdict_under_exclude_none() -> None:
    outcome = CpsatMutationOutcome(name="element_dropped", skipped_reason="no list")
    dumped = outcome.model_dump(exclude_none=True)

    assert ("status" in dumped, "duration_ms" in dumped) == (False, False)


def test_cpsat_mutation_outcome_rejects_a_row_carrying_both_outcomes() -> None:
    with pytest.raises(ValidationError, match="exactly one of status and skipped_reason"):
        CpsatMutationOutcome(
            name="element_dropped",
            skipped_reason="no list",
            status="rejected",
        )


def test_cpsat_mutation_outcome_rejects_a_row_carrying_neither_outcome() -> None:
    """A mutation that could not apply is never silently dropped."""
    with pytest.raises(ValidationError, match="exactly one of status and skipped_reason"):
        CpsatMutationOutcome(name="element_dropped")


def _checker_test_report(*statuses: str) -> CpsatCheckerTestReport:
    mutations = [
        CpsatMutationOutcome(
            name=name,
            status=status,  # type: ignore[arg-type]
            errors=[] if status == "accepted" else ["corrupted"],
            duration_ms=3,
        )
        for name, status in zip(CPSAT_MUTATION_NAMES, statuses, strict=False)
    ]
    return CpsatCheckerTestReport(mutations=mutations)


def test_cpsat_checker_test_report_round_trips() -> None:
    report = _checker_test_report("accepted", "rejected")
    dumped = report.model_dump()
    assert (dumped["rejected_count"], "discriminating" in dumped) == (1, False)


def test_cpsat_checker_test_report_does_not_repeat_the_baseline() -> None:
    # The report only ever rides on a CpsatPythonCheckedResult whose top-level
    # `checker` IS the accepted baseline, so repeating it here would serialize
    # a second copy of a report that can hold a MiB of checker output.
    assert "baseline" not in CpsatCheckerTestReport.model_fields


def test_cpsat_checker_test_report_does_not_count_an_indeterminate_mutant() -> None:
    # A mutant that errors or times out reached no verdict.
    report = _checker_test_report("error", "timeout")

    assert (report.rejected_count, report.accepted_count) == (0, 0)


def test_cpsat_checker_test_report_counts_a_tolerated_mutant() -> None:
    assert _checker_test_report("accepted", "accepted").accepted_count == 2


def test_cpsat_checker_test_report_excludes_a_skipped_row_from_every_count() -> None:
    report = CpsatCheckerTestReport(
        mutations=[CpsatMutationOutcome(name="objective_perturbed", skipped_reason="no list")],
    )

    assert (report.rejected_count, report.accepted_count) == (0, 0)


def test_cpsat_checker_test_report_overrides_a_miscounted_tally() -> None:
    # The counts are derived from `mutations`, so a supplied tally cannot
    # misreport the table it summarizes.
    report = CpsatCheckerTestReport(
        mutations=[CpsatMutationOutcome(name="objective_perturbed", status="accepted")],
        rejected_count=1,
    )

    assert report.rejected_count == 0


def test_cpsat_python_checked_result_defaults_checker_test_to_none() -> None:
    result = CpsatPythonCheckedResult(
        **_checked_kwargs(), checker_skipped_reason="solution is missing or empty"
    )
    assert result.checker_test is None


def test_cpsat_python_checked_result_omits_a_null_checker_test_under_exclude_none() -> None:
    result = CpsatPythonCheckedResult(
        **_checked_kwargs(), checker_skipped_reason="solution is missing or empty"
    )
    assert "checker_test" not in result.model_dump(exclude_none=True)


def test_cpsat_python_checked_result_carries_a_checker_test_report() -> None:
    result = CpsatPythonCheckedResult(
        **_checked_kwargs(),
        checker=_mutation_report("accepted"),
        checker_test=_checker_test_report("rejected"),
    )
    assert result.checker_test is not None
    assert result.checker_test.rejected_count == 1


def test_cpsat_python_checked_result_rejects_a_checker_test_without_a_checker() -> None:
    # A probe reported where no checker ran would be evidence no run ever
    # produced.
    with pytest.raises(ValidationError, match="requires an accepted checker"):
        CpsatPythonCheckedResult(
            **_checked_kwargs(),
            checker_skipped_reason="solution is missing or empty",
            checker_test=_checker_test_report("rejected"),
        )
