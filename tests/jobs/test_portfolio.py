"""Unit tests for the solver-portfolio admission + winner-selection engine.

The synchronous ``solve_portfolio`` is gone; the background ``PortfolioJobRegistry``
now owns the portfolio workflow. These tests drive ``_admit_portfolio`` (plan
validation, capability gate, cross-product expansion, atomic admission) directly,
and ``_race`` runs a plan through the registry's event path — the race settles on
attempt terminal events and ``_race`` only waits for it — without a runtime
(``run_prepared_solve`` is mocked).
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

# The portfolio engine retained for the background path; these are package-internal
# helpers, not a public API.
# noinspection PyProtectedMember
from openconstraint_mcp.jobs.portfolio import (
    _admit_portfolio,
    _first_decisive_index,
    _PortfolioAdmission,
)
from openconstraint_mcp.jobs.portfolio_registry import PortfolioJobRegistry
from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.minizinc.core import DEFAULT_SOLVE_TIMEOUT_MS
from openconstraint_mcp.schemas.minizinc import (
    CheckerReport,
    SolveJobStatus,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
)
from openconstraint_mcp.schemas.portfolio import (
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from openconstraint_mcp.shared.job_errors import JobRejectedError
from openconstraint_mcp.shared.save_target import text_sha256


def _solve_result(
    status: str = "satisfied", *, solver: str = "cp-sat", checker: CheckerReport | None = None
) -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver=solver,
        return_code=0,
        timed_out=False,
        stdout="x = 1;\n",
        stderr="",
        elapsed_ms=3,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=22 if status == "optimal" else None,
        checker=checker,
    )


class _FakeProc:
    """Opaque handle stand-in; no ``pid``, so real termination must stay patched."""


@pytest.fixture(autouse=True)
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every proc in this file is a ``_FakeProc``; the real group-aware
    terminate would probe ``os.getpgid``/``os.killpg`` on it. Tests that
    assert termination re-patch a recorder over this.
    """
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _patch_solve(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.jobs.registry.run_prepared_solve", fake)


def _seed_from_args(extra_args: tuple[str, ...]) -> int | None:
    """Read the ``-r`` seed a worker's prepared argv carries (``None`` when unseeded)."""
    if "-r" not in extra_args:
        return None
    return int(extra_args[extra_args.index("-r") + 1])


def _patch_capabilities(
    monkeypatch: pytest.MonkeyPatch, caps_by_id: dict[str, SolverCapabilities]
) -> None:
    solvers = [
        SolverInfo(id=solver_id, name=solver_id, capabilities=caps)
        for solver_id, caps in caps_by_id.items()
    ]
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.list_solvers", lambda: SolverList(solvers=solvers)
    )


# The optional plan fields the engine requires explicitly (the synchronous tool used
# to default them); the tests fill them here so each call reads like the old API.
_ADMIT_DEFAULTS: dict[str, Any] = {
    "data": None,
    "checker": None,
    "seed_count": 1,
    "seeds": None,
    "per_attempt_timeout_ms": DEFAULT_SOLVE_TIMEOUT_MS,
    "solve_controls": PortfolioSolveControls(
        free_search=False, parallel=None, all_solutions=False, num_solutions=None
    ),
}


def _ignore_attempt_event(index: int, status: SolveJobStatus) -> None:
    """A no-op attempt listener for tests that exercise admission only."""


def _admit(registry: JobRegistry, **kwargs: Any) -> _PortfolioAdmission:
    """Admit a portfolio plan, filling the optional fields the engine requires."""
    return _admit_portfolio(
        registry,
        on_attempt_terminal=_ignore_attempt_event,
        on_attempt_error=lambda index, message: None,
        **{**_ADMIT_DEFAULTS, **kwargs},
    )


def _race(registry: JobRegistry, **kwargs: Any) -> PortfolioSolveResult:
    """Submit a plan as a background portfolio and wait until the race settles itself.

    The attempts' terminal events drive selection and loser cancellation; ``get`` only
    reads, so the deadline wait below adds no selection of its own.
    """
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(registry)
    job_id: str = portfolios.submit(**{**_ADMIT_DEFAULTS, **kwargs})
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        status: PortfolioJobStatus = portfolios.get(job_id)
        if status.state != "running":
            assert status.result is not None
            return status.result
        time.sleep(0.01)
    raise AssertionError("portfolio race did not resolve within 10s")


def test_portfolio_returns_decisive_winner_and_cancels_loser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cp-sat reaches a decisive optimum immediately; gecode blocks until cancelled.
    # The portfolio selects cp-sat (index 0) and cancels the still-running loser.
    release = threading.Event()

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "cp-sat":
            return _solve_result("optimal", solver="cp-sat")
        release.wait(timeout=5)
        return _solve_result("satisfied", solver=solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    registry = JobRegistry(max_running_jobs=4)
    try:
        model_text = "solve satisfy;"
        data_text = "n = 3;"
        checker_text = "% checker\n"
        result = _race(
            registry,
            models=[model_text],
            solvers=["cp-sat", "org.gecode.gecode"],
            data=data_text,
            checker=checker_text,
        )
        assert result.status == "winner"
        assert result.winner_index == 0
        assert result.winner is not None
        assert result.winner.status == "optimal"
        assert result.attempts[0].state == "succeeded"
        assert result.attempts[0].result_status == "optimal"
        # The race settles on the decisive attempt and cancels the loser right after,
        # so the loser's end state is read from its solve job, not the settle snapshot.
        loser_job_id: str | None = result.attempts[1].job_id
        assert loser_job_id is not None
        deadline: float = time.monotonic() + 3.0
        while registry.get(loser_job_id).state == "running" and time.monotonic() < deadline:
            time.sleep(0.005)
        assert registry.get(loser_job_id).state == "cancelled"
        assert result.selection_policy == "first-decisive-result"
        assert result.models_sha256 == [text_sha256(model_text)]
        assert result.data_sha256 == text_sha256(data_text)
        assert result.checker_sha256 == text_sha256(checker_text)
    finally:
        release.set()
        registry.shutdown()


def test_portfolio_attempt_surfaces_checker_status(monkeypatch: pytest.MonkeyPatch) -> None:
    # checker_status is purely observational: a checker-rejected attempt still wins
    # the race exactly as an unchecked one would — no gating on the checker verdict.
    violation = CheckerReport(status="violation", checks=[], transcript="checker output")

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver, checker=violation)

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        result = _race(
            registry, models=["solve satisfy;"], solvers=["cp-sat"], checker="% checker\n"
        )
        assert result.status == "winner"
        assert result.winner_index == 0
        assert result.attempts[0].checker_status == "violation"
    finally:
        registry.shutdown()


def test_portfolio_result_records_shared_solve_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    # The shared search controls every attempt ran with are provenance, captured
    # at admission and recorded verbatim on the result (see PortfolioSolveControls).
    _patch_capabilities(
        monkeypatch,
        {"cp-sat": SolverCapabilities(supports_free_search=True, supports_parallel=True)},
    )

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        result = _race(
            registry,
            models=["solve satisfy;"],
            solvers=["cp-sat"],
            solve_controls=PortfolioSolveControls(
                free_search=True, parallel=2, all_solutions=False, num_solutions=None
            ),
        )
        assert result.solve_controls == PortfolioSolveControls(
            free_search=True, parallel=2, all_solutions=False, num_solutions=None
        )
    finally:
        registry.shutdown()


def test_portfolio_expands_models_solvers_seeds_cross_product(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # models x solvers x seeds is the full cross-product: every (model_index, solver,
    # seed) triple appears exactly once. seed_count > 1 needs -r on every solver.
    _patch_capabilities(
        monkeypatch,
        {
            "s1": SolverCapabilities(supports_random_seed=True),
            "s2": SolverCapabilities(supports_random_seed=True),
        },
    )

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("satisfied", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry()
    try:
        result = _race(
            registry,
            models=["model A", "model B"],
            solvers=["s1", "s2"],
            seed_count=2,
        )
        triples = {(a.model_index, a.solver, a.seed) for a in result.attempts}
        assert triples == {
            (m_idx, solver, seed) for solver in ("s1", "s2") for seed in (1, 2) for m_idx in (0, 1)
        }
        assert len(result.attempts) == 8
    finally:
        registry.shutdown()


def test_portfolio_plan_interleaves_models_first(monkeypatch: pytest.MonkeyPatch) -> None:
    # The model index varies fastest (D1): when len(models) <= max_running_jobs, the
    # first len(models) attempts cover every distinct formulation, so each gets a
    # first-wave slot before any model is repeated on another solver.
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("satisfied", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        models = ["model A", "model B", "model C"]
        result = _race(
            registry,
            models=models,
            solvers=["cp-sat", "org.gecode.gecode"],
        )
        first_wave = [attempt.model_index for attempt in result.attempts[: len(models)]]
        assert first_wave == [0, 1, 2]
    finally:
        registry.shutdown()


def test_portfolio_winner_carries_its_model_index(monkeypatch: pytest.MonkeyPatch) -> None:
    # Only the second formulation reaches a decisive verdict, so the winning attempt
    # reports model_index 1 — the winning formulation is identifiable from the result.
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if model == "model B":
            return _solve_result("optimal", solver=solver)
        return SolveResult(
            status="unknown",
            solver=solver,
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=2,
        )

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        result = _race(registry, models=["model A", "model B"], solvers=["cp-sat"])
        assert result.status == "winner"
        assert result.winner_index is not None
        assert result.attempts[result.winner_index].model_index == 1
        assert result.winner is not None
        assert result.winner.status == "optimal"
    finally:
        registry.shutdown()


def test_portfolio_no_winner_when_all_attempts_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise RuntimeError("managed binary blew up")

    _patch_solve(monkeypatch, _boom)
    registry = JobRegistry()
    try:
        result = _race(registry, models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"])
        assert result.status == "no_winner"
        assert result.winner_index is None
        assert result.winner is None
        assert all(attempt.state == "failed" for attempt in result.attempts)
    finally:
        registry.shutdown()


def test_portfolio_picks_timeout_incumbent_over_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    # No decisive result: a timeout WITH an incumbent solution outranks a bare
    # `unknown`, so the timeout attempt is the best-available winner (D6).
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "cp-sat":
            return SolveResult(
                status="timeout",
                solver="cp-sat",
                return_code=None,
                timed_out=True,
                stdout="x=1\n",
                stderr="",
                elapsed_ms=5,
                solution={"x": 1},
                solutions=[{"x": 1}],
            )
        return SolveResult(
            status="unknown",
            solver=solver,
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=4,
        )

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        result = _race(registry, models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"])
        assert result.status == "winner"
        assert result.winner_index == 0
        assert result.winner is not None
        assert result.winner.status == "timeout"
        assert result.winner.solution == {"x": 1}
    finally:
        registry.shutdown()


def test_portfolio_admits_plan_wider_than_running_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the portfolio-side cap removed, a plan wider than max_running_jobs is no
    # longer rejected: every attempt is admitted (within registry capacity) and the
    # excess queues — concurrency never exceeds the running cap, but all attempts are
    # accounted for. A 3-attempt plan on a 2-running registry is admitted and wins.
    concurrency_lock = threading.Lock()
    running = 0
    peak = 0

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        nonlocal running, peak
        on_start(_FakeProc())
        with concurrency_lock:
            running += 1
            peak = max(peak, running)
        try:
            time.sleep(0.05)  # hold the slot so concurrent running is observable
            return _solve_result("satisfied", solver=solver)
        finally:
            with concurrency_lock:
                running -= 1

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=2, max_queued_jobs=16)  # capacity 18
    try:
        result = _race(
            registry,
            models=["solve satisfy;"],
            solvers=["cp-sat", "org.gecode.gecode", "org.chuffed.chuffed"],
        )
        assert result.status == "winner"
        # Admitted, not rejected: the 3-attempt plan exceeds the running cap of 2.
        assert len(result.attempts) == 3
        # The running cap held — at least one attempt had to queue.
        assert peak <= 2
    finally:
        registry.shutdown()


def test_portfolio_rejects_unsupported_control_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an unsupported control")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="free_search"):
            _admit(
                registry,
                models=["solve satisfy;"],
                solvers=["cp-sat"],
                solve_controls=PortfolioSolveControls(
                    free_search=True, parallel=None, all_solutions=False, num_solutions=None
                ),
            )
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_portfolio_rejects_parallel_below_one_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The solver supports -p, so the capability gate passes; the range check that
    # builds each attempt's argv must still reject the plan before any job is admitted.
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities(supports_parallel=True)})

    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an out-of-range control")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="parallel must be >= 1"):
            _admit(
                registry,
                models=["solve satisfy;"],
                solvers=["cp-sat"],
                solve_controls=PortfolioSolveControls(
                    free_search=False, parallel=0, all_solutions=False, num_solutions=None
                ),
            )
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_portfolio_rejects_num_solutions_below_one_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an out-of-range control")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="num_solutions must be >= 1"):
            _admit(
                registry,
                models=["solve satisfy;"],
                solvers=["org.chuffed.chuffed"],
                solve_controls=PortfolioSolveControls(
                    free_search=False, parallel=None, all_solutions=False, num_solutions=0
                ),
            )
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_portfolio_rejects_whole_plan_when_a_later_solver_lacks_num_solutions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first attempt (chuffed) builds valid -n argv; the second (cp-sat) is not
    # allowlisted for -n, so the whole plan fails before any attempt is admitted.
    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run when any attempt's control is invalid")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="solver 'cp-sat' does not support num_solutions"):
            _admit(
                registry,
                models=["solve satisfy;"],
                solvers=["org.chuffed.chuffed", "cp-sat"],
                solve_controls=PortfolioSolveControls(
                    free_search=False, parallel=None, all_solutions=False, num_solutions=2
                ),
            )
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_portfolio_expands_seeds_when_seed_count_gt_one(monkeypatch: pytest.MonkeyPatch) -> None:
    # seed_count > 1 expands one solver into seeded attempts 1..N; every solver must
    # support -r. Each attempt carries its seed.
    _patch_capabilities(
        monkeypatch, {"org.gecode.gecode": SolverCapabilities(supports_random_seed=True)}
    )
    seeds_seen: list[int | None] = []

    def _fake_solve(
        model: str, *, extra_args: tuple[str, ...], on_start: Any, **kw: Any
    ) -> SolveResult:
        on_start(_FakeProc())
        seeds_seen.append(_seed_from_args(extra_args))
        return _solve_result("satisfied", solver="org.gecode.gecode")

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=4)
    try:
        result = _race(
            registry, models=["solve satisfy;"], solvers=["org.gecode.gecode"], seed_count=2
        )
        assert result.status == "winner"
        assert [attempt.seed for attempt in result.attempts] == [1, 2]
        assert sorted(s for s in seeds_seen if s is not None) == [1, 2]
    finally:
        registry.shutdown()


def test_portfolio_uses_explicit_seeds_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # Explicit seeds are used verbatim, in caller order, with the model index varying
    # fastest inside each (solver, seed) group. No extra unseeded attempt is added.
    _patch_capabilities(
        monkeypatch,
        {
            "s1": SolverCapabilities(supports_random_seed=True),
            "s2": SolverCapabilities(supports_random_seed=True),
        },
    )
    seen: list[tuple[str, int | None, str]] = []

    def _fake_solve(
        model: str, *, solver: str, extra_args: tuple[str, ...], on_start: Any, **kw: Any
    ) -> SolveResult:
        on_start(_FakeProc())
        seen.append((solver, _seed_from_args(extra_args), model))
        return SolveResult(
            status="unknown",
            solver=solver,
            return_code=0,
            timed_out=False,
            stdout="",
            stderr="",
            elapsed_ms=2,
        )

    _patch_solve(monkeypatch, _fake_solve)
    registry = JobRegistry(max_running_jobs=1)
    try:
        models = ["model A", "model B"]
        result = _race(registry, models=models, solvers=["s1", "s2"], seeds=[42, 123, 999])
        expected = [
            (solver, seed, model)
            for solver in ("s1", "s2")
            for seed in (42, 123, 999)
            for model in models
        ]
        assert seen == expected
        assert [(a.solver, a.seed, a.model_index) for a in result.attempts] == [
            (solver, seed, m_idx)
            for solver in ("s1", "s2")
            for seed in (42, 123, 999)
            for m_idx in (0, 1)
        ]
        assert all(attempt.seed is not None for attempt in result.attempts)
    finally:
        registry.shutdown()


def test_portfolio_explicit_seeds_require_random_seed_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an unsupported seed control")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="random_seed"):
            _admit(registry, models=["solve satisfy;"], solvers=["cp-sat"], seeds=[42])
        assert registry.list() == []
    finally:
        registry.shutdown()


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"models": [], "solvers": ["cp-sat"]}, "models must not be empty"),
        ({"models": ["solve satisfy;"], "solvers": []}, "solvers must not be empty"),
        (
            {"models": ["solve satisfy;"], "solvers": ["cp-sat"], "seed_count": 0},
            "seed_count must be >= 1",
        ),
        (
            {"models": ["solve satisfy;"], "solvers": ["cp-sat"], "seeds": []},
            "seeds must not be empty",
        ),
        (
            {
                "models": ["solve satisfy;"],
                "solvers": ["cp-sat"],
                "seed_count": 2,
                "seeds": [42],
            },
            "seeds cannot be combined",
        ),
        (
            {"models": ["solve satisfy;"], "solvers": ["cp-sat"], "seeds": [42, 42]},
            "seeds must not contain duplicates",
        ),
        (
            {"models": ["solve satisfy;"], "solvers": ["cp-sat"], "per_attempt_timeout_ms": 0},
            "per_attempt_timeout_ms",
        ),
    ],
)
def test_portfolio_rejects_invalid_plan(
    kwargs: dict[str, Any], match: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an invalid plan")

    _patch_solve(monkeypatch, _fail)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match=match):
            _admit(registry, **kwargs)
    finally:
        registry.shutdown()


def test_portfolio_propagates_atomic_admission_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A registry already full (a concurrent load) makes the atomic submit_many
    # reject the whole batch; the portfolio surfaces it and admits nothing.
    release = threading.Event()
    started = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=2, max_queued_jobs=0)  # capacity 2
    try:
        registry.submit(model="solve satisfy;")
        registry.submit(model="solve satisfy;")  # both running → capacity full
        assert started.wait(timeout=3)
        with pytest.raises(JobRejectedError):
            _admit(registry, models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"])
        # Atomic: nothing from the portfolio was admitted and in_flight is unchanged.
        assert len(registry.list()) == 2
        assert registry._in_flight == 2
    finally:
        release.set()
        registry.shutdown()


def _decisive_status(
    *, index: int, finished_at_ms: int, status: str = "optimal", solver: str = "cp-sat"
) -> SolveJobStatus:
    """A terminal, decisive attempt snapshot that finished at a given wall-clock ms."""
    return SolveJobStatus(
        job_id=f"job-{index}",
        state="succeeded",
        solver=solver,
        timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
        submitted_at_ms=0,
        started_at_ms=0,
        finished_at_ms=finished_at_ms,
        elapsed_ms=finished_at_ms,
        result=_solve_result(status, solver=solver),
    )


def test_first_decisive_index_picks_earliest_finisher_not_lowest_index() -> None:
    # Attempt 1 reached its decisive verdict first (t=100ms); attempt 0 became
    # decisive later (t=200ms). The documented "first-decisive-result" policy makes
    # the earliest finisher win, so winner selection must not just take the lowest
    # plan-order index that happens to be decisive in the snapshot.
    statuses = [
        _decisive_status(index=0, finished_at_ms=200, status="satisfied"),
        _decisive_status(index=1, finished_at_ms=100, status="optimal"),
    ]
    assert _first_decisive_index(statuses) == 1


def test_first_decisive_index_breaks_same_ms_tie_by_index() -> None:
    # Two attempts finished in the same millisecond; the attempt index is the
    # deterministic tie-breaker, so the lower index wins.
    statuses = [
        _decisive_status(index=0, finished_at_ms=100, status="optimal"),
        _decisive_status(index=1, finished_at_ms=100, status="optimal"),
    ]
    assert _first_decisive_index(statuses) == 0
