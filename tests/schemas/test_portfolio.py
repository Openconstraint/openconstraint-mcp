from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.minizinc import SolveResult
from openconstraint_mcp.schemas.portfolio import (
    PortfolioAttempt,
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from openconstraint_mcp.shared.save_target import text_sha256


def _portfolio_winner_result() -> SolveResult:
    return SolveResult(
        status="optimal",
        solver="org.chuffed.chuffed",
        return_code=0,
        timed_out=False,
        stdout="x=2 total=22\n",
        stderr="",
        elapsed_ms=120,
        statistics={"failures": "0"},
        solution={"x": 2},
        solutions=[{"x": 2}],
        objective=22,
    )


_PORTFOLIO_CONTROLS = PortfolioSolveControls(
    free_search=False, parallel=None, all_solutions=False, num_solutions=None
)


def test_portfolio_attempt_round_trips() -> None:
    attempt = PortfolioAttempt(
        index=1,
        model_index=3,
        solver="org.gecode.gecode",
        seed=2,
        timeout_ms=5000,
        state="succeeded",
        job_id="job-2",
        job_state="succeeded",
        result_status="satisfied",
        objective=None,
        elapsed_ms=80,
        message=None,
    )
    assert attempt.model_dump(mode="json") == {
        "index": 1,
        "model_index": 3,
        "solver": "org.gecode.gecode",
        "seed": 2,
        "timeout_ms": 5000,
        "state": "succeeded",
        "job_id": "job-2",
        "job_state": "succeeded",
        "result_status": "satisfied",
        "objective": None,
        "elapsed_ms": 80,
        "message": None,
        "checker_status": None,
        "diagnostic": None,
    }


def test_portfolio_solve_result_winner_with_cancelled_loser_round_trips() -> None:
    # The common case: a decisive winner plus a sibling cancelled when the winner
    # was selected. The winner attempt's result_status mirrors the winning result.
    winner = _portfolio_winner_result()
    result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=winner,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="org.chuffed.chuffed",
                seed=None,
                timeout_ms=5000,
                state="succeeded",
                job_id="job-0",
                job_state="succeeded",
                result_status="optimal",
                objective=22,
                elapsed_ms=120,
            ),
            PortfolioAttempt(
                index=1,
                model_index=0,
                solver="org.gecode.gecode",
                seed=None,
                timeout_ms=5000,
                state="cancelled",
                job_id="job-1",
                job_state="cancelled",
                message="Cancelled by client",
            ),
        ],
        elapsed_ms=130,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "winner"
    assert dumped["winner_index"] == 0
    assert dumped["winner"]["status"] == "optimal"
    assert [a["state"] for a in dumped["attempts"]] == ["succeeded", "cancelled"]
    assert dumped["attempts"][1]["result_status"] is None
    assert dumped["selection_policy"] == "first-decisive-result"


def test_portfolio_solve_result_timeout_with_incumbent_is_a_winner() -> None:
    # A timeout attempt is result-bearing: when it is the best available, it is a
    # valid winner carrying its partial SolveResult.
    incumbent = SolveResult(
        status="timeout",
        solver="cp-sat",
        return_code=None,
        timed_out=True,
        stdout="x=1\n",
        stderr="",
        elapsed_ms=5000,
        solution={"x": 1},
        solutions=[{"x": 1}],
    )
    result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=incumbent,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="cp-sat",
                seed=None,
                timeout_ms=5000,
                state="timeout",
                job_id="job-0",
                job_state="timeout",
                result_status="timeout",
                elapsed_ms=5000,
            )
        ],
        elapsed_ms=5010,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    assert result.winner is not None
    assert result.winner.status == "timeout"
    assert result.winner.solution == {"x": 1}


def test_portfolio_solve_result_all_failed_has_no_winner() -> None:
    result = PortfolioSolveResult(
        status="no_winner",
        winner_index=None,
        winner=None,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="cp-sat",
                seed=None,
                timeout_ms=5000,
                state="failed",
                job_id="job-0",
                job_state="failed",
                message="MiniZincExecutionError: boom",
            )
        ],
        elapsed_ms=42,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )
    dumped = result.model_dump(mode="json")
    assert dumped["status"] == "no_winner"
    assert dumped["winner_index"] is None
    assert dumped["winner"] is None


def test_portfolio_solve_result_rejects_winner_status_without_a_winner() -> None:
    with pytest.raises(ValidationError):
        PortfolioSolveResult(
            status="winner",
            winner_index=None,
            winner=None,
            attempts=[],
            elapsed_ms=1,
            selection_policy="first-decisive-result",
            models_sha256=[],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_portfolio_solve_result_rejects_no_winner_carrying_a_winner() -> None:
    with pytest.raises(ValidationError):
        PortfolioSolveResult(
            status="no_winner",
            winner_index=0,
            winner=_portfolio_winner_result(),
            attempts=[],
            elapsed_ms=1,
            selection_policy="first-decisive-result",
            models_sha256=[],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_portfolio_solve_result_rejects_attempt_model_index_out_of_range() -> None:
    # The winner's attempt is one of `self.attempts`, so validating every attempt
    # automatically covers the winner too. Here the sole attempt (and winner)
    # claims model_index=1, but models_sha256 has only one entry (valid index 0).
    with pytest.raises(ValidationError, match="model_index"):
        PortfolioSolveResult(
            status="winner",
            winner_index=0,
            winner=_portfolio_winner_result(),
            attempts=[
                PortfolioAttempt(
                    index=0,
                    model_index=1,
                    solver="org.chuffed.chuffed",
                    timeout_ms=5000,
                    state="succeeded",
                    job_id="job-0",
                    job_state="succeeded",
                    result_status="optimal",
                    objective=22,
                )
            ],
            elapsed_ms=130,
            selection_policy="first-decisive-result",
            models_sha256=["m0-hash"],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=_PORTFOLIO_CONTROLS,
        )


def test_empty_string_data_hashes_distinctly_from_none() -> None:
    # PortfolioSolveResult.data_sha256/checker_sha256 are None iff the race ran
    # with no data/checker supplied. An empty-string input, if the solve path ever
    # accepts one, must still hash to sha256("") — a real digest, never collapsing
    # to the None sentinel used for "not supplied".
    empty_hash = text_sha256("")
    assert empty_hash == hashlib.sha256(b"").hexdigest()
    assert empty_hash is not None


def _portfolio_job_result() -> PortfolioSolveResult:
    return PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=_portfolio_winner_result(),
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="org.chuffed.chuffed",
                timeout_ms=5000,
                state="succeeded",
                job_id="job-0",
                job_state="succeeded",
                result_status="optimal",
                objective=22,
            )
        ],
        elapsed_ms=130,
        selection_policy="first-decisive-result",
        models_sha256=["m0-hash"],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_PORTFOLIO_CONTROLS,
    )


def test_portfolio_job_status_succeeded_round_trips_with_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj1",
        state="succeeded",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=1000,
        started_at_ms=1001,
        finished_at_ms=1140,
        elapsed_ms=139,
        result=_portfolio_job_result(),
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "succeeded"
    assert dumped["result"]["winner"]["status"] == "optimal"
    assert dumped["message"] is None


def test_portfolio_job_status_running_serializes_without_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj-run",
        state="running",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=5,
        started_at_ms=6,
    )
    dumped = status.model_dump(mode="json")
    assert dumped["state"] == "running"
    assert dumped["result"] is None


def test_portfolio_job_status_cancelled_has_message_and_no_result() -> None:
    status = PortfolioJobStatus(
        job_id="pj-c",
        state="cancelled",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=5,
        started_at_ms=6,
        finished_at_ms=7,
        elapsed_ms=1,
        message="Cancelled by client",
    )
    assert status.result is None
    assert status.message == "Cancelled by client"


def test_portfolio_job_status_rejects_succeeded_without_a_result() -> None:
    # Enforced invariant: result is present exactly when state == "succeeded".
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad",
            state="succeeded",
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
        )


def test_portfolio_job_status_rejects_cancelled_carrying_a_result() -> None:
    # A non-result-bearing state must not carry a result (only `succeeded` does).
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad2",
            state="cancelled",
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
            result=_portfolio_job_result(),
        )


def test_portfolio_job_status_failed_has_message_and_no_result() -> None:
    # `failed` is terminal for a race whose aggregate result could not be built.
    status: PortfolioJobStatus = PortfolioJobStatus(
        job_id="pj-f",
        state="failed",
        per_attempt_timeout_ms=5000,
        submitted_at_ms=5,
        started_at_ms=6,
        finished_at_ms=7,
        elapsed_ms=1,
        message="Could not build the portfolio result: RuntimeError: boom",
    )
    assert (status.state, status.result) == ("failed", None)


def test_portfolio_job_status_rejects_unknown_state() -> None:
    # `timeout` is not a portfolio job state — each attempt reports its own timeout.
    with pytest.raises(ValidationError):
        PortfolioJobStatus(
            job_id="pj-bad3",
            state="timeout",  # type: ignore[arg-type]
            per_attempt_timeout_ms=5000,
            submitted_at_ms=1,
        )
