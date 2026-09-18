from __future__ import annotations

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.artifacts import SavedModelArtifact
from openconstraint_mcp.schemas.minizinc import (
    CheckerReport,
    CheckResult,
    ModelInspectionResult,
    SaveVerifiedModelResult,
    SolutionCheck,
    SolveControls,
    SolveJobStatus,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
    job_state_for_result,
)


def test_solve_result_round_trips() -> None:
    # A multi-solution optimization result: `solutions` holds the improving
    # sequence in order, `solution` is its last (best) element, `objective` is
    # the best `_objective`, and `statistics` are bare stringified stream values
    # (no raw-token quotes, unlike the old %%%mzn-stat: scrape).
    result = SolveResult(
        status="optimal",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=0 y=1 total=2\nx=2 y=10 total=22\n",
        stderr="",
        elapsed_ms=42,
        statistics={"failures": "0", "method": "maximize"},
        solution={"x": 2, "y": 10},
        solutions=[{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        objective=22,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "optimal",
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        # Additive field; a normal (un-truncated) solve renders it as False.
        "truncated": False,
        "stdout": "x=0 y=1 total=2\nx=2 y=10 total=22\n",
        "stderr": "",
        "elapsed_ms": 42,
        "statistics": {"failures": "0", "method": "maximize"},
        "solution": {"x": 2, "y": 10},
        "solutions": [{"x": 0, "y": 1}, {"x": 2, "y": 10}],
        "objective": 22,
        # An ordinary solve carries no checker; the additive field renders as null,
        # consistent with the other always-emitted nullable fields.
        "checker": None,
        "diagnostic": None,
    }


def test_solve_result_round_trips_satisfaction_has_null_objective() -> None:
    # A satisfaction result carries a solution but no objective: `_objective` is
    # absent from a satisfy model's json section, so `objective` stays None while
    # `solution`/`solutions` still round-trip.
    result = SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=1 y=2\n",
        stderr="",
        elapsed_ms=10,
        solution={"x": 1, "y": 2},
        solutions=[{"x": 1, "y": 2}],
        objective=None,
    )
    dumped = result.model_dump()
    assert dumped["objective"] is None
    assert dumped["solution"] == {"x": 1, "y": 2}
    assert dumped["solutions"] == [{"x": 1, "y": 2}]
    assert dumped["status"] == "satisfied"


def test_solve_result_truncated_defaults_false_and_round_trips_true() -> None:
    # The additive `truncated` field defaults to False (existing callers omit it)
    # and round-trips True for an output-cap kill.
    assert (
        SolveResult(
            status="unknown",
            solver="cp-sat",
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=1,
        ).truncated
        is False
    )
    truncated = SolveResult(
        status="satisfied",
        solver="org.gecode.gecode",
        return_code=None,
        timed_out=False,
        truncated=True,
        stdout="x=1\n",
        stderr="output exceeded the 1 MiB cap; process stopped\n",
        elapsed_ms=7,
        solution={"x": 1},
        solutions=[{"x": 1}],
    )
    assert truncated.model_dump()["truncated"] is True
    assert truncated.return_code is None


def test_analysis_results_truncated_defaults_false() -> None:
    # The additive `truncated` field on the analysis results defaults to False,
    # so existing callers that omit it are unchanged.
    check = CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=1)
    inspection = ModelInspectionResult(
        status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=1
    )
    core = UnsatCoreResult(
        status="no_core", core=[], message="", stdout="", stderr="", elapsed_ms=1
    )
    assert (check.truncated, inspection.truncated, core.truncated) == (False, False, False)


def test_solve_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SolveResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_check_result_round_trips() -> None:
    result = CheckResult(
        status="ok",
        solver="cp-sat",
        stdout="",
        stderr="",
        elapsed_ms=12,
    )
    dumped = result.model_dump()
    assert dumped == {
        "status": "ok",
        "solver": "cp-sat",
        # Additive field; a normal (un-truncated) check renders it as False.
        "truncated": False,
        "stdout": "",
        "stderr": "",
        "elapsed_ms": 12,
        "diagnostic": None,
    }


def test_check_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CheckResult(
            status="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_unsat_core_result_round_trips() -> None:
    result = UnsatCoreResult(
        status="mus_found",
        core=[
            UnsatCoreConstraint(
                line=4,
                column=12,
                end_line=4,
                end_column=20,
                source="x + y > 5",
            )
        ],
        message="findMUS reported a minimal unsatisfiable subset.",
        stdout="MUS: 1 2\n",
        stderr="",
        elapsed_ms=7,
    )

    assert result.model_dump() == {
        "status": "mus_found",
        "core": [
            {
                "line": 4,
                "column": 12,
                "end_line": 4,
                "end_column": 20,
                "source": "x + y > 5",
            }
        ],
        "message": "findMUS reported a minimal unsatisfiable subset.",
        # Additive field; a normal (un-truncated) run renders it as False.
        "truncated": False,
        "stdout": "MUS: 1 2\n",
        "stderr": "",
        "elapsed_ms": 7,
        "diagnostic": None,
    }


def test_unsat_core_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        UnsatCoreResult(
            status="bogus",  # type: ignore[arg-type]
            message="bad status",
            stdout="",
            stderr="",
            elapsed_ms=0,
        )


def test_solve_result_with_checker_round_trips() -> None:
    # A violation solve nests a CheckerReport on the SolveResult: `solutions` still
    # INCLUDES the checker-rejected solution (fact 5), `checker.checks` is
    # index-aligned with it, and `checker.transcript` preserves the raw
    # `--solution-checker` transcript verbatim — the authoritative checker record,
    # while `stdout` is the solution-only text.
    result = SolveResult(
        status="satisfied",
        solver="org.gecode.gecode",
        return_code=0,
        timed_out=False,
        stdout="x=1 y=2\n",
        stderr="",
        elapsed_ms=15,
        solution={"x": 1, "y": 2},
        solutions=[{"x": 1, "y": 2}],
        objective=None,
        checker=CheckerReport(
            status="violation",
            checks=[SolutionCheck(violation=True, output="model inconsistency detected")],
            transcript='{"type":"checker"}\n{"type":"solution"}\n',
        ),
    )

    dumped = result.model_dump()
    assert dumped["checker"] == {
        "status": "violation",
        "checks": [{"violation": True, "output": "model inconsistency detected"}],
        "transcript": '{"type":"checker"}\n{"type":"solution"}\n',
    }
    # The rejected solution stays in `solutions` (a violation does not suppress it).
    assert dumped["solutions"] == [{"x": 1, "y": 2}]
    assert dumped["status"] == "satisfied"


def test_solution_check_round_trips() -> None:
    # The per-solution check: `violation` is the one server-asserted verdict;
    # `output` carries the author CORRECT/INCORRECT text verbatim, unadjudicated.
    check = SolutionCheck(violation=False, output="CORRECT\n")
    assert check.model_dump() == {"violation": False, "output": "CORRECT\n"}


def test_checker_report_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        CheckerReport(
            status="checked",  # type: ignore[arg-type]
            checks=[],
            transcript="",
        )


def test_solver_capabilities_round_trips() -> None:
    caps = SolverCapabilities(
        supports_all_solutions=True,
        supports_free_search=True,
        supports_parallel=True,
        supports_random_seed=True,
        supports_num_solutions=True,
        std_flags=["-a", "-f", "-n", "-p", "-r"],
    )
    assert caps.model_dump() == {
        "supports_all_solutions": True,
        "supports_free_search": True,
        "supports_parallel": True,
        "supports_random_seed": True,
        "supports_num_solutions": True,
        "std_flags": ["-a", "-f", "-n", "-p", "-r"],
    }


def test_solver_info_round_trips_with_capabilities() -> None:
    info = SolverInfo(
        id="org.gecode.gecode",
        name="Gecode",
        version="6.3.0",
        tags=["cp", "int"],
        capabilities=SolverCapabilities(
            supports_all_solutions=True,
            supports_free_search=True,
            supports_parallel=True,
            supports_random_seed=True,
            supports_num_solutions=True,
            std_flags=["-a", "-f", "-n", "-p", "-r"],
        ),
    )
    assert info.model_dump() == {
        "id": "org.gecode.gecode",
        "name": "Gecode",
        "version": "6.3.0",
        "tags": ["cp", "int"],
        "capabilities": {
            "supports_all_solutions": True,
            "supports_free_search": True,
            "supports_parallel": True,
            "supports_random_seed": True,
            "supports_num_solutions": True,
            "std_flags": ["-a", "-f", "-n", "-p", "-r"],
        },
    }


def _passing_check() -> CheckResult:
    return CheckResult(status="ok", solver="cp-sat", stdout="", stderr="", elapsed_ms=5)


def _satisfied_solve() -> SolveResult:
    return SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x=1\n",
        stderr="",
        elapsed_ms=9,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=None,
    )


def test_save_verified_model_result_round_trips_as_json() -> None:
    # A saved result serializes with model_dump(mode="json") — the same mode the
    # server uses for structuredContent — with bare-filename artifact paths.
    result = SaveVerifiedModelResult(
        status="saved",
        message="Verified model saved.",
        target_dir="/home/user/projects/knapsack",
        files=[
            SavedModelArtifact(role="model", path="model.mzn", sha256="ab" * 32),
            SavedModelArtifact(role="solve_result", path="solve-result.json", sha256="cd" * 32),
        ],
        check=_passing_check(),
        solve=_satisfied_solve(),
    )

    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "saved"
    assert dumped["target_dir"] == "/home/user/projects/knapsack"
    assert dumped["files"] == [
        {"role": "model", "path": "model.mzn", "sha256": "ab" * 32},
        {"role": "solve_result", "path": "solve-result.json", "sha256": "cd" * 32},
    ]
    assert dumped["check"]["status"] == "ok"
    assert dumped["solve"]["status"] == "satisfied"


def test_save_verified_model_result_files_default_to_empty_list() -> None:
    result = SaveVerifiedModelResult(
        status="not_verified",
        message="Solve did not verify.",
        target_dir="/home/user/projects/knapsack",
        check=_passing_check(),
        solve=_satisfied_solve(),
    )
    assert result.files == []


def test_save_verified_model_result_check_gate_serializes_with_null_solve() -> None:
    # The check-gate outcome: the compile failed, so no solve ran — `solve` is
    # None and the result still serializes cleanly.
    result = SaveVerifiedModelResult(
        status="not_verified",
        message="Model failed the compile check.",
        target_dir="/home/user/projects/knapsack",
        check=CheckResult(status="error", solver="cp-sat", stdout="", stderr="boom", elapsed_ms=3),
    )

    dumped = result.model_dump(mode="json")
    assert dumped["solve"] is None
    assert dumped["files"] == []
    assert dumped["check"]["status"] == "error"


def test_save_verified_model_result_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        SaveVerifiedModelResult(
            status="written",  # type: ignore[arg-type]
            message="",
            target_dir="/tmp/x",
            check=_passing_check(),
        )


def _job_solve_result(status: SolveStatus, *, timed_out: bool = False) -> SolveResult:
    return SolveResult(
        status=status,
        solver="cp-sat",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        stdout="",
        stderr="",
        elapsed_ms=5,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("satisfied", "succeeded"),
        ("optimal", "succeeded"),
        ("unsatisfiable", "succeeded"),
        ("unknown", "succeeded"),
        ("unbounded", "succeeded"),
        ("unsat_or_unbounded", "succeeded"),
        # The load-bearing case: a structured solver/driver `error` verdict is a
        # SUCCEEDED job (a result was produced), not a job-machinery failure.
        ("error", "succeeded"),
        ("timeout", "timeout"),
    ],
)
def test_job_state_for_result_maps_every_solve_status(status: SolveStatus, expected: str) -> None:
    # The total mapping over all eight SolveStatus values for the
    # result-present paths: only a `timeout` verdict is `timeout`; everything else
    # (including `error`, `unbounded`, `unsat_or_unbounded`) is `succeeded`.
    assert job_state_for_result(_job_solve_result(status)) == expected


def test_job_state_for_result_timed_out_flag_overrides_status_to_timeout() -> None:
    # SolveResult.timed_out is a separate bool from status; a hard subprocess
    # timeout maps to `timeout` even when the partial stream's status is `unknown`.
    assert job_state_for_result(_job_solve_result("unknown", timed_out=True)) == "timeout"


def test_solve_job_status_succeeded_round_trips_with_result() -> None:
    status = SolveJobStatus(
        job_id="abc123",
        state="succeeded",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1050,
        elapsed_ms=49,
        result=_job_solve_result("optimal"),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["status"] == "optimal"
    assert dumped["message"] is None


def test_solve_job_status_queued_serializes_without_result() -> None:
    status = SolveJobStatus(
        job_id="q1", state="queued", solver="cp-sat", timeout_ms=30000, submitted_at_ms=5
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "queued"
    assert dumped["result"] is None
    assert dumped["started_at_ms"] is None


def test_solve_job_status_cancelled_serializes_with_message_and_no_result() -> None:
    status = SolveJobStatus(
        job_id="c1",
        state="cancelled",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=5,
        started_at_ms=6,
        finished_at_ms=7,
        elapsed_ms=1,
        message="cancelled by client",
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "cancelled"
    assert dumped["result"] is None
    assert dumped["message"] == "cancelled by client"


def test_solve_job_status_failed_has_none_result() -> None:
    # A runner exception → failed with result is None (failed ⇒ result is None; the
    # converse fails — queued/running/cancelled are result-less too).
    status = SolveJobStatus(
        job_id="f1",
        state="failed",
        solver="cp-sat",
        timeout_ms=30000,
        submitted_at_ms=5,
        message="boom",
    )
    assert status.result is None


def test_solve_job_status_rejects_unknown_state() -> None:
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="x",
            state="bogus",  # type: ignore[arg-type]
            solver="cp-sat",
            timeout_ms=30000,
            submitted_at_ms=1,
        )


def test_solve_job_status_rejects_failed_carrying_a_result() -> None:
    # The enforced invariant: a non-result-bearing state must not carry a result.
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="f",
            state="failed",
            solver="cp-sat",
            timeout_ms=30000,
            submitted_at_ms=1,
            result=_job_solve_result("error"),
        )


def test_solve_job_status_rejects_succeeded_without_a_result() -> None:
    # The enforced invariant: a result-bearing state must carry a result.
    with pytest.raises(ValidationError):
        SolveJobStatus(
            job_id="s", state="succeeded", solver="cp-sat", timeout_ms=30000, submitted_at_ms=1
        )


def test_solver_info_capabilities_default_is_conservative() -> None:
    # A bare SolverInfo defaults capabilities to all-False booleans and an empty
    # std_flags — the conservative default that keeps Pydantic construction
    # compatible and the missing-config case default-deny.
    info = SolverInfo(id="com.example.unknown", name="Unknown")
    assert info.capabilities.model_dump() == {
        "supports_all_solutions": False,
        "supports_free_search": False,
        "supports_parallel": False,
        "supports_random_seed": False,
        "supports_num_solutions": False,
        "std_flags": [],
    }


def test_solve_controls_defaults_request_no_control() -> None:
    controls = SolveControls()
    assert controls == SolveControls(
        free_search=False, parallel=None, random_seed=None, all_solutions=False, num_solutions=None
    )


def test_solve_controls_strict_mode_rejects_bool_for_parallel() -> None:
    with pytest.raises(ValidationError):
        SolveControls(parallel=True)


def test_solve_controls_is_frozen() -> None:
    controls = SolveControls()
    with pytest.raises(ValidationError):
        controls.free_search = True  # type: ignore[misc]


def test_solve_controls_dump_key_order_is_manifest_order() -> None:
    # The saved manifest's solve_controls splices this dump after timeout_ms, so
    # the declared field order is load-bearing for byte-identical manifests.
    assert list(SolveControls().model_dump()) == [
        "free_search",
        "parallel",
        "random_seed",
        "all_solutions",
        "num_solutions",
    ]
