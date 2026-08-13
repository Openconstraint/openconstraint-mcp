"""Real-runtime check for the solver portfolio.

Proves what the mocked unit tests cannot: a portfolio over the *actual* managed
runtime admits real attempts across *two* model formulations and the shipped
solvers, races the full cross-product, and returns a winner whose ``SolveResult``
is usable exactly like a single ``solve_minizinc_model``. Marked ``integration``
and excluded from ``just check``; run with ``just integration`` on a machine where
``install-runtime`` has placed a runtime with at least two portfolio-safe solvers.
"""

from __future__ import annotations

import time

import pytest

from openconstraint_mcp.jobs.portfolio_registry import PortfolioJobRegistry
from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.minizinc.core import list_solvers

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_real_runtime")]

_TERMINAL_ATTEMPT_STATES = {"succeeded", "timeout", "failed", "cancelled"}

# Two tiny formulations of the SAME instance (x in {2, 3}), each with an output
# item, so every solver returns quickly with a `satisfied` solution and an
# authoritative stdout block. They are alternative encodings, not different problems.
_TINY_MODEL = 'var 1..3: x;\nconstraint x > 1;\nsolve satisfy;\noutput ["x=\\(x)\\n"];\n'
_TINY_MODEL_ALT = 'var 1..3: x;\nconstraint x >= 2;\nsolve satisfy;\noutput ["x=\\(x)\\n"];\n'


def _portfolio_safe_solvers() -> list[str]:
    """Return every shipped full-featured solver present in the runtime (parity)."""
    available = {solver.id for solver in list_solvers().solvers}
    preferred = ["cp-sat", "org.gecode.gecode", "org.chuffed.chuffed"]
    return [solver for solver in preferred if solver in available]


def test_portfolio_job_races_real_runtime_and_returns_usable_winner_on_poll() -> None:
    # Submit returns at once (no blocking past an MCP timeout), and polling drives the
    # real race to a usable PortfolioSolveResult — winner-selection runs on the poll,
    # with no worker pool. Proves what the mocked unit tests cannot: a portfolio over
    # the actual managed runtime races the full models x solvers cross-product and
    # returns a winner whose SolveResult is usable exactly like solve_minizinc_model.
    solvers = _portfolio_safe_solvers()
    if len(solvers) < 2:
        pytest.skip("need >= 2 portfolio-safe solvers in the managed runtime")
    models = [_TINY_MODEL, _TINY_MODEL_ALT]

    registry = JobRegistry()
    portfolios = PortfolioJobRegistry(registry)
    try:
        start = time.monotonic()
        job_id = portfolios.submit(models=models, solvers=solvers, per_attempt_timeout_ms=10000)
        assert time.monotonic() - start < 1.0  # submit did not block on the solves

        deadline = time.monotonic() + 30.0
        status = portfolios.get(job_id)
        while status.state == "running" and time.monotonic() < deadline:
            time.sleep(0.05)
            status = portfolios.get(job_id)

        assert status.state == "succeeded"
        result = status.result
        assert result is not None
        assert result.status == "winner"
        assert result.winner_index is not None
        assert result.winner is not None
        # The winning SolveResult is usable exactly like solve_minizinc_model's.
        assert result.winner.status in ("satisfied", "optimal")
        assert result.winner.solution is not None
        assert result.winner.stdout

        # The plan is the full models x solvers cross-product; every attempt is
        # accounted for and terminal (a winner plus terminal/cancelled losers), and
        # both formulations are represented.
        assert len(result.attempts) == len(models) * len(solvers)
        assert {attempt.model_index for attempt in result.attempts} == {0, 1}
        for attempt in result.attempts:
            assert attempt.state in _TERMINAL_ATTEMPT_STATES
        # The winning attempt names the formulation it ran.
        assert result.attempts[result.winner_index].model_index in (0, 1)
    finally:
        registry.shutdown()
