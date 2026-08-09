from __future__ import annotations

from openconstraint_mcp.pyexec.core import _result_from_child
from openconstraint_mcp.pyexec.diagnostics import (
    checked_result_diagnostic,
    cpsat_result_diagnostic,
    experiment_attempt_diagnostic,
    experiment_diagnostic,
    output_contract_diagnostic,
    save_failure_diagnostic,
)
from openconstraint_mcp.pyexec.jobs import (
    CpsatJobRegistry,
    _CpsatJobRecord,
    _CpsatJobRequest,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
    CpsatStatus,
)
from openconstraint_mcp.shared.childrun import ChildExecutionResult


def _result(
    status: CpsatStatus,
    *,
    solution: dict | None = None,
    timed_out: bool = False,
    truncated: bool = False,
    return_code: int | None = 0,
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution=solution,
        objective=None,
        stdout="",
        stderr="",
        return_code=return_code,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=10,
    )


# --- cpsat_result_diagnostic ------------------------------------------------


def test_clean_optimal_with_solution_is_none() -> None:
    assert cpsat_result_diagnostic(_result("optimal", solution={"x": 1})) is None


def test_optimal_with_empty_solution_is_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("optimal", solution={}))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_optimal_with_missing_solution_is_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("feasible", solution=None))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_infeasible_maps_to_infeasible() -> None:
    assert cpsat_result_diagnostic(_result("infeasible")).category == "infeasible"  # type: ignore[union-attr]


def test_unknown_maps_to_unknown() -> None:
    assert cpsat_result_diagnostic(_result("unknown")).category == "unknown"  # type: ignore[union-attr]


def test_timeout_with_incumbent() -> None:
    diag = cpsat_result_diagnostic(_result("timeout", solution={"x": 1}, timed_out=True))
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_timeout_without_incumbent() -> None:
    diag = cpsat_result_diagnostic(_result("timeout", timed_out=True))
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"


def test_rejected_partial_enriches_details_without_changing_the_category() -> None:
    diag = cpsat_result_diagnostic(
        _result("timeout", timed_out=True),
        rejected_partial=("objective", "required key is missing"),
    )
    assert diag is not None
    assert diag.category == "timeout_no_incumbent"
    assert diag.details == {
        "truncated": False,
        "rejected_partial_field": "objective",
        "rejected_partial_reason": "required key is missing",
    }


def test_truncation_maps_to_output_truncated() -> None:
    diag = cpsat_result_diagnostic(_result("error", truncated=True, return_code=0))
    assert diag is not None
    assert diag.category == "output_truncated"


def test_timeout_wins_over_truncation() -> None:
    diag = cpsat_result_diagnostic(
        _result("timeout", solution={"x": 1}, timed_out=True, truncated=True)
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"
    assert diag.details == {"truncated": True}


def test_error_maps_to_child_process_error() -> None:
    diag = cpsat_result_diagnostic(_result("error", return_code=1))
    assert diag is not None
    assert diag.category == "child_process_error"


def test_result_from_child_wires_diagnostic_onto_result() -> None:
    # A non-zero exit with no parseable JSON is a child error; the builder sets
    # the diagnostic as its single tail.
    child = ChildExecutionResult(
        stdout="boom",
        stderr="Traceback ...",
        return_code=1,
        timed_out=False,
        truncated=False,
        duration_ms=7,
    )
    result = _result_from_child(child)
    assert result.status == "error"
    assert result.diagnostic is not None
    assert result.diagnostic.category == "child_process_error"


def test_result_from_child_clean_solution_has_no_diagnostic() -> None:
    child = ChildExecutionResult(
        stdout='{"status": "optimal", "solution": {"x": 1}, "objective": 1}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=7,
    )
    result = _result_from_child(child)
    assert result.status == "optimal"
    assert result.diagnostic is None


# --- output_contract_diagnostic ---------------------------------------------


def test_output_contract_diagnostic_is_a_child_process_error() -> None:
    diag = output_contract_diagnostic(
        field="solution", reason="required key is missing", return_code=0
    )
    assert diag.category == "child_process_error"


def test_output_contract_diagnostic_details_are_exactly_field_reason_return_code() -> None:
    diag = output_contract_diagnostic(field="objective", reason="must be a number", return_code=0)
    assert diag.details == {
        "field": "objective",
        "reason": "must be a number",
        "return_code": 0,
    }


def test_output_contract_diagnostic_message_names_the_field() -> None:
    diag = output_contract_diagnostic(
        field="status", reason="required key is missing", return_code=0
    )
    assert "`status`" in diag.message


def test_result_from_child_routes_an_envelope_violation_to_the_field_diagnostic() -> None:
    # The single diagnostic tail picks the field-specific builder over
    # cpsat_result_diagnostic's generic "failed or emitted malformed output".
    child = ChildExecutionResult(
        stdout='{"status": "optimal", "objective": 1}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=7,
    )
    result = _result_from_child(child)
    assert result.diagnostic is not None
    assert result.diagnostic.details == {
        "field": "solution",
        "reason": "required key is missing",
        "return_code": 0,
    }


def _envelope_violation_result() -> CpsatPythonResult:
    """A real executor result for a child whose final block omits `objective`."""
    return _result_from_child(
        ChildExecutionResult(
            stdout='{"status": "optimal", "solution": {"x": 1}}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=7,
        )
    )


# --- checker report diagnostic (via run path result contract) ---------------


def _checker(
    status: str, *, truncated: bool = False, timed_out: bool = False
) -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=timed_out,
        truncated=truncated,
    )


# --- checked_result_diagnostic ----------------------------------------------


def _diagnosed(result: CpsatPythonResult) -> CpsatPythonResult:
    """Attach the run-derived diagnostic the executor sets before any checker runs."""
    result.diagnostic = cpsat_result_diagnostic(result)
    return result


def test_checked_clean_run_with_accepted_checker_is_none() -> None:
    result = _diagnosed(_result("optimal", solution={"x": 1}))
    assert checked_result_diagnostic(result, _checker("accepted")) is None


def test_checked_failed_checker_overrides_a_clean_run() -> None:
    result = _diagnosed(_result("optimal", solution={"x": 1}))
    diag = checked_result_diagnostic(result, _checker("rejected"))
    assert diag is not None
    assert diag.category == "checker_failed"


def test_checked_run_timeout_wins_over_a_failed_checker() -> None:
    result = _diagnosed(_result("timeout", solution={"x": 1}, timed_out=True))
    diag = checked_result_diagnostic(result, _checker("rejected"))
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_checked_run_derived_diagnostic_stands_without_a_checker() -> None:
    result = _diagnosed(_result("infeasible"))
    diag = checked_result_diagnostic(result, None)
    assert diag is not None
    assert diag.category == "infeasible"


def test_checked_absent_result_and_checker_is_none() -> None:
    # The job path passes a None result for a failed/cancelled record.
    assert checked_result_diagnostic(None, None) is None


# --- save_failure_diagnostic ------------------------------------------------


def test_save_failure_checker_rejection_is_checker_failed() -> None:
    diag = save_failure_diagnostic(_result("optimal", solution={"x": 1}), _checker("rejected"))
    assert diag.category == "checker_failed"


def test_save_failure_timeout_result_surfaces_timeout() -> None:
    diag = save_failure_diagnostic(_result("timeout", timed_out=True), None)
    assert diag.category == "timeout_no_incumbent"


def test_save_failure_envelope_violation_keeps_the_offending_field() -> None:
    # The save route must not recompute the diagnostic from the result model:
    # the field/reason lives only on the diagnostic the executor already built.
    diag = save_failure_diagnostic(_envelope_violation_result(), None)
    assert diag.details is not None
    assert diag.details["field"] == "objective"


def test_save_failure_clean_result_rejected_by_gate_is_not_verified() -> None:
    # A clean optimal result that failed a reported/expectation gate: no more
    # specific category, so a generic not_verified.
    diag = save_failure_diagnostic(_result("optimal", solution={"x": 1}), None)
    assert diag.category == "not_verified"


# --- experiment_attempt_diagnostic ------------------------------------------


def test_accepted_attempt_has_no_diagnostic() -> None:
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}), accepted=True, checker_status=None, message=None
    )
    assert diag is None


def test_accepted_timeout_attempt_surfaces_timeout_with_incumbent() -> None:
    diag = experiment_attempt_diagnostic(
        _result("timeout", solution={"x": 1}, timed_out=True),
        accepted=True,
        checker_status=None,
        message=None,
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_rejected_missing_objective_attempt_is_not_verified() -> None:
    # An optimal result rejected by the optimization-mode acceptance gate for a
    # missing objective: cpsat_result_diagnostic is clean, so not_verified with
    # the attempt message.
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}),
        accepted=False,
        checker_status=None,
        message="objective is missing or non-numeric",
    )
    assert diag is not None
    assert diag.category == "not_verified"
    assert diag.message == "objective is missing or non-numeric"


def test_rejected_envelope_violation_attempt_keeps_the_offending_field() -> None:
    # The attempt row carries no stdout and (for a script that ran fine but
    # printed the wrong shape) no stderr_tail, so this diagnostic is the only
    # place the client learns which key to repair.
    diag = experiment_attempt_diagnostic(
        _envelope_violation_result(),
        accepted=False,
        checker_status=None,
        message="solution is missing or empty",
    )
    assert diag is not None
    assert diag.details is not None
    assert diag.details["field"] == "objective"


def test_rejected_by_checker_attempt_is_checker_failed() -> None:
    diag = experiment_attempt_diagnostic(
        _result("optimal", solution={"x": 1}),
        accepted=False,
        checker_status="rejected",
        message="checker rejected",
    )
    assert diag is not None
    assert diag.category == "checker_failed"


# --- experiment_diagnostic --------------------------------------------------


def _attempt_row(status: CpsatStatus) -> CpsatPythonExperimentAttemptResult:
    return CpsatPythonExperimentAttemptResult(
        index=0,
        name="a",
        source_sha256="0" * 64,
        script_timeout_ms=1000,
        status=status,
        objective=None,
        accepted=False,
        timed_out=False,
        truncated=False,
        duration_ms=1,
    )


def test_no_winner_experiment_maps_to_no_winner() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[_attempt_row("infeasible"), _attempt_row("unknown")],
        elapsed_ms=5,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
    )
    diag = experiment_diagnostic(result)
    assert diag is not None
    assert diag.category == "no_winner"
    assert diag.details == {"attempts": 2, "statuses": ["infeasible", "unknown"]}


# --- CpsatPythonJobStatus wrapper diagnostic --------------------------------


def _cpsat_record(
    state: str,
    *,
    result: CpsatPythonResult | None = None,
    checker: CpsatCheckerReport | None = None,
    checker_skipped_reason: str | None = None,
    message: str | None = None,
) -> _CpsatJobRecord:
    return _CpsatJobRecord(
        job_id="job-1",
        request=_CpsatJobRequest(source="print()", script_path=None, script_timeout_ms=1000),
        submitted_at_ms=0,
        state=state,  # type: ignore[arg-type]
        result=result,
        checker_report=checker,
        checker_skipped_reason=checker_skipped_reason,
        message=message,
    )


def test_cpsat_failed_job_maps_to_job_failed() -> None:
    diag = CpsatJobRegistry._job_diagnostic(_cpsat_record("failed", message="worker died"))
    assert diag is not None
    assert diag.category == "job_failed"


def test_cpsat_succeeded_job_derives_from_result() -> None:
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(_cpsat_record("succeeded", result=result))
    assert diag is None


def test_cpsat_job_envelope_violation_keeps_the_offending_field() -> None:
    # The background-job route reaches the client as a CpsatPythonJobStatus with
    # no stdout of its own, so `_job_diagnostic` must carry the field through
    # rather than recomputing the generic child-process message. The violated
    # run is never checker-eligible, hence checker=None + a skipped reason.
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record(
            "succeeded",
            result=_envelope_violation_result(),
            checker_skipped_reason="status is 'error'",
        )
    )
    assert diag is not None
    assert diag.details is not None
    assert diag.details["field"] == "objective"


def test_cpsat_job_checker_rejection_overrides_result_diagnostic() -> None:
    # Clean optimal result, but the job-level checker rejected -> checker_failed.
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("succeeded", result=result, checker=_checker("rejected"))
    )
    assert diag is not None
    assert diag.category == "checker_failed"


def test_cpsat_job_checker_rejection_does_not_mask_timeout_incumbent() -> None:
    result = _result("timeout", solution={"x": 1}, timed_out=True)
    result.diagnostic = cpsat_result_diagnostic(result)
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("timeout", result=result, checker=_checker("rejected"))
    )
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_cpsat_job_checker_skipped_reason_keeps_result_diagnostic() -> None:
    # A skipped checker adds no diagnostic; the result-derived one (None here for
    # a clean optimal) stands.
    result = _result("optimal", solution={"x": 1})
    diag = CpsatJobRegistry._job_diagnostic(
        _cpsat_record("succeeded", result=result, checker_skipped_reason="result not eligible")
    )
    assert diag is None


def test_winner_experiment_surfaces_winner_diagnostic() -> None:
    # A clean optimal winner carries no diagnostic, so neither does the experiment.
    winner = _result("optimal", solution={"x": 1})
    winner.diagnostic = None
    row = _attempt_row("optimal")
    row.name = "w"
    row.accepted = True
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="w",
        winner=winner,
        attempts=[row],
        elapsed_ms=5,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
    )
    assert experiment_diagnostic(result) is None
