"""Wrapper-level diagnostics for job/portfolio status models.

Exercises the small diagnostic helpers each registry funnels through, with
fabricated records/results so the classification is deterministic and needs no
subprocess.
"""

from __future__ import annotations

from openconstraint_mcp.jobs.portfolio import _portfolio_result_diagnostic, _to_attempt
from openconstraint_mcp.jobs.registry import JobRegistry, SolveRequest, _JobRecord
from openconstraint_mcp.schemas.diagnostics import Diagnostic
from openconstraint_mcp.schemas.minizinc import SolveControls, SolveJobStatus, SolveResult

_REQUEST = SolveRequest(
    model="",
    solver="cp-sat",
    data=None,
    checker=None,
    timeout_ms=1000,
    controls=SolveControls(),
    extra_args=(),
)


def _record(
    state: str, *, result: SolveResult | None = None, message: str | None = None
) -> _JobRecord:
    return _JobRecord(
        job_id="job-1",
        request=_REQUEST,
        submitted_at_ms=0,
        state=state,  # type: ignore[arg-type]
        result=result,
        message=message,
    )


def _solve(status: str, **kw: object) -> SolveResult:
    defaults: dict[str, object] = {
        "solver": "cp-sat",
        "return_code": 0,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "elapsed_ms": 5,
    }
    defaults.update(kw)
    return SolveResult(status=status, **defaults)  # type: ignore[arg-type]


# --- SolveJobStatus wrapper diagnostic --------------------------------------


def test_failed_job_maps_to_job_failed() -> None:
    diag = JobRegistry._job_diagnostic(_record("failed", message="worker crashed"))
    assert diag is not None
    assert diag.category == "job_failed"
    assert diag.message == "worker crashed"
    assert diag.details == {"job_id": "job-1", "state": "failed"}


def test_cancelled_job_maps_to_cancelled() -> None:
    diag = JobRegistry._job_diagnostic(_record("cancelled"))
    assert diag is not None
    assert diag.category == "cancelled"


def test_succeeded_job_derives_from_result() -> None:
    # A result-derived diagnostic (infeasible) wins; no generic wrapper.
    result = _solve("unsatisfiable", diagnostic=Diagnostic(category="infeasible", message="x"))
    diag = JobRegistry._job_diagnostic(_record("succeeded", result=result))
    assert diag is not None
    assert diag.category == "infeasible"


def test_timeout_job_uses_result_timeout_over_wrapper() -> None:
    result = _solve(
        "timeout",
        timed_out=True,
        solutions=[{"x": 1}],
        diagnostic=Diagnostic(category="timeout_with_incumbent", message="t"),
    )
    diag = JobRegistry._job_diagnostic(_record("timeout", result=result))
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"


def test_running_job_has_no_diagnostic() -> None:
    assert JobRegistry._job_diagnostic(_record("running")) is None


# --- PortfolioAttempt reuses the job diagnostic -----------------------------


def test_portfolio_attempt_reuses_job_status_diagnostic() -> None:
    status = SolveJobStatus(
        job_id="job-1",
        state="failed",
        solver="cp-sat",
        timeout_ms=1000,
        submitted_at_ms=0,
        message="boom",
        diagnostic=Diagnostic(category="job_failed", message="boom"),
    )
    attempt = _to_attempt(0, (0, "cp-sat", None), status)
    assert attempt.diagnostic is not None
    assert attempt.diagnostic.category == "job_failed"


# --- PortfolioSolveResult ----------------------------------------------------


def test_portfolio_no_winner_maps_to_no_winner() -> None:
    from openconstraint_mcp.schemas.portfolio import PortfolioAttempt

    attempts = [
        PortfolioAttempt(index=0, model_index=0, solver="cp-sat", timeout_ms=1000, state="failed"),
        PortfolioAttempt(
            index=1, model_index=0, solver="cp-sat", timeout_ms=1000, state="cancelled"
        ),
    ]
    diag = _portfolio_result_diagnostic("no_winner", None, attempts)
    assert diag is not None
    assert diag.category == "no_winner"
    assert diag.details == {"attempts": 2, "states": ["cancelled", "failed"]}


def test_portfolio_winner_surfaces_winner_diagnostic() -> None:
    winner = _solve("satisfied", solution={"x": 1}, solutions=[{"x": 1}])
    winner.diagnostic = None
    assert _portfolio_result_diagnostic("winner", winner, []) is None


def test_portfolio_timeout_winner_surfaces_timeout() -> None:
    winner = _solve(
        "timeout",
        timed_out=True,
        solutions=[{"x": 1}],
        diagnostic=Diagnostic(category="timeout_with_incumbent", message="t"),
    )
    diag = _portfolio_result_diagnostic("winner", winner, [])
    assert diag is not None
    assert diag.category == "timeout_with_incumbent"
