"""Unit tests for the background portfolio-job registry (event-driven selection).

A real ``JobRegistry`` drives the attempts (its ``run_prepared_solve`` is
mocked) and a real ``PortfolioJobRegistry`` settles the race from the attempts'
terminal events. These prove the async portfolio path — submit returns at once,
the race settles itself, ``get`` only reads, cancel stops it — without a runtime
and without a background worker pool of its own.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from typing import Any

import pytest

from openconstraint_mcp.jobs.portfolio_registry import PortfolioJobRegistry
from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.schemas.minizinc import SolveResult
from openconstraint_mcp.schemas.portfolio import (
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from openconstraint_mcp.shared.job_errors import JobRejectedError

_TERMINAL = {"succeeded", "failed", "cancelled"}
_SOLVE_TERMINAL = {"succeeded", "failed", "timeout", "cancelled"}


def _solve_result(status: str = "satisfied", *, solver: str = "cp-sat") -> SolveResult:
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
    )


class _FakeProc:
    """Opaque handle stand-in; no ``pid``, so real termination must stay patched."""


@pytest.fixture(autouse=True)
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every proc in this file is a ``_FakeProc``; the real group-aware
    terminate would probe ``os.getpgid``/``os.killpg`` on it.
    """
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _patch_solve(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.jobs.registry.run_prepared_solve", fake)


def _poll(registry: PortfolioJobRegistry, job_id: str, timeout: float = 5.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = registry.get(job_id)
        if status.state in _TERMINAL:
            return status
        time.sleep(0.01)
    raise AssertionError(f"portfolio job {job_id} did not finish within {timeout}s")


def _wait_solve_terminal(registry: JobRegistry, job_id: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in _SOLVE_TERMINAL:
            return state
        time.sleep(0.005)
    raise AssertionError(f"solve job {job_id} did not reach terminal state within {timeout}s")


def test_submit_then_poll_reaches_succeeded_with_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        assert final.result is not None
        assert final.result.status == "winner"
        assert final.result.winner is not None
        assert final.result.winner.status == "optimal"
    finally:
        job_registry.shutdown()


def test_poll_succeeds_after_child_attempt_would_exceed_solve_retention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The first attempt finishes non-decisively and is cached while the second still
    # runs; unrelated solves then evict the first attempt's solve record. The race
    # must still settle from the cached status, not a re-read of the evicted record.
    release = threading.Event()

    class _ExitedProc(_FakeProc):
        # Retention eviction reaps an evicted record's handle via poll().
        def poll(self) -> int:
            return 0

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_ExitedProc())
        if solver == "org.gecode.gecode":
            release.wait(timeout=5)
            return _solve_result("optimal", solver=solver)
        return _solve_result("unknown", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=2, max_queued_jobs=4, max_retained_terminal=1)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        record = portfolios._records[job_id]
        cached_id, _ = record.attempt_job_ids
        deadline = time.monotonic() + 3.0
        while record.statuses[0] is None and time.monotonic() < deadline:
            time.sleep(0.005)
        assert record.statuses[0] is not None

        unrelated_id = job_registry.submit(model="solve satisfy;", solver="cp-sat")
        _wait_solve_terminal(job_registry, unrelated_id)
        with pytest.raises(ValueError, match="unknown job_id"):
            job_registry.get(cached_id)  # evicted while the portfolio is still running
        assert portfolios.get(job_id).state == "running"

        release.set()
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        assert final.result is not None
        assert final.result.winner_index == 1
        assert final.result.attempts[0].result_status == "unknown"
        assert final.result.winner is not None
    finally:
        job_registry.shutdown()


def test_submit_after_solve_registry_shutdown_is_rejected() -> None:
    job_registry = JobRegistry()
    portfolios = PortfolioJobRegistry(job_registry)
    job_registry.shutdown()

    with pytest.raises(JobRejectedError, match="shutting down"):
        portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
    assert portfolios.list() == []


def test_submit_does_not_block_while_attempts_run(monkeypatch: pytest.MonkeyPatch) -> None:
    # The attempts block until released; submit must still return promptly (it only
    # admits them), and a poll while they run reports `running`.
    release = threading.Event()

    def _slow_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        release.wait(timeout=5)
        return _solve_result("satisfied", solver=solver)

    _patch_solve(monkeypatch, _slow_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        start = time.monotonic()
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        assert time.monotonic() - start < 1.0  # did not wait for the 5s-blocked solve
        assert portfolios.get(job_id).state == "running"
    finally:
        release.set()
        job_registry.shutdown()


def test_single_attempt_succeeds_on_capacity_one_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 1-attempt portfolio on a max_running=1 attempt registry completes: there is
    # no orchestration thread competing for the single attempt slot.
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=1, max_queued_jobs=0)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        assert final.result is not None
        assert final.result.winner is not None
    finally:
        job_registry.shutdown()


def test_submit_empty_models_raises_synchronously_without_creating_a_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _never(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run when admission rejects the plan")

    _patch_solve(monkeypatch, _never)

    job_registry = JobRegistry()
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        with pytest.raises(ValueError, match="models must not be empty"):
            portfolios.submit(models=[], solvers=["cp-sat"])
        assert portfolios.list() == []
    finally:
        job_registry.shutdown()


def test_submit_rejects_plan_exceeding_capacity_synchronously(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attempt registry's running+queued bound is the only breadth cap; a plan
    # past it is rejected by submit_many at admission, synchronously, before the
    # portfolio job exists.
    def _never(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run when the batch exceeds capacity")

    _patch_solve(monkeypatch, _never)

    job_registry = JobRegistry(max_running_jobs=1, max_queued_jobs=0)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        with pytest.raises(JobRejectedError):
            portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"])
        assert portfolios.list() == []
    finally:
        job_registry.shutdown()


def test_cancel_running_portfolio_reaches_cancelled_and_stops_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result("satisfied", solver=solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the "process" dying unblocks the worker

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        assert started.wait(timeout=3)
        cancelled = portfolios.cancel(job_id)
        assert cancelled.state == "cancelled"
        assert cancelled.result is None
        assert terminated  # the attempt's process tree was signalled
    finally:
        release.set()
        job_registry.shutdown()


def test_list_returns_one_entry_per_submitted_portfolio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        ids = {portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"]) for _ in range(2)}
        for job_id in ids:
            _poll(portfolios, job_id)
        assert {entry.job_id for entry in portfolios.list()} == ids
    finally:
        job_registry.shutdown()


def test_get_unknown_job_id_raises() -> None:
    job_registry = JobRegistry()
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        with pytest.raises(ValueError, match="unknown"):
            portfolios.get("does-not-exist")
    finally:
        job_registry.shutdown()


def test_list_does_not_observe_partially_finalized_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # _finalize sets state='succeeded' before result, under record.lock. A list()
    # that skips that lock can read the transient state='succeeded' with result=None
    # and trip the PortfolioJobStatus validator. Holding record.lock in exactly that
    # transient state proves list() WAITS for the lock rather than reading through it.
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        record = portfolios._records[job_id]
        consistent = PortfolioSolveResult(
            status="no_winner",
            winner_index=None,
            winner=None,
            attempts=[],
            elapsed_ms=1,
            selection_policy="first-decisive-result",
            models_sha256=[],
            data_sha256=None,
            checker_sha256=None,
            solve_controls=PortfolioSolveControls(
                free_search=False, parallel=None, all_solutions=False, num_solutions=None
            ),
        )
        listed: list[Any] = []
        errors: list[BaseException] = []
        started = threading.Event()

        def _call_list() -> None:
            started.set()
            try:
                listed.append(portfolios.list())
            except BaseException as exc:  # noqa: BLE001 - capture the validator crash
                errors.append(exc)

        with record.lock:
            # The transient inconsistent state that _finalize passes through.
            record.state = "succeeded"
            record.result = None
            lister = threading.Thread(target=_call_list)
            lister.start()
            assert started.wait(timeout=2)
            lister.join(timeout=0.2)
            assert lister.is_alive(), "list() read a record without taking its lock"
            # Complete the finalize before releasing, exactly as _finalize does.
            record.result = consistent
        lister.join(timeout=2)

        assert not errors
        assert len(listed) == 1
        (status,) = listed[0]
        assert status.state == "succeeded"
        assert status.result is consistent
    finally:
        job_registry.shutdown()


def test_poll_with_all_attempts_failing_is_succeeded_no_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A race that produces no usable result is a SUCCESSFUL orchestration carrying a
    # `no_winner` PortfolioSolveResult — not a failed job.
    def _boom(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise RuntimeError("boom")

    _patch_solve(monkeypatch, _boom)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        assert final.result is not None
        assert final.result.status == "no_winner"
    finally:
        job_registry.shutdown()


# --- event-driven settlement ------------------------------------------------


def test_race_settles_and_cancels_losers_without_client_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # cp-sat turns decisive only once the gecode loser is running, so the loser's
    # cancel must terminate a live handle. The race settles on attempt events; the
    # deadline wait below only reads (get() has no side effects). The loser is
    # cancelled after the job settles, so its termination is awaited, not assumed.
    loser_started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "cp-sat":
            loser_started.wait(timeout=5)
            return _solve_result("optimal", solver=solver)
        loser_started.set()
        release.wait(timeout=5)
        return _solve_result("satisfied", solver=solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the loser's worker finishes only once its tree is signalled

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        loser_id: str = portfolios._records[job_id].attempt_job_ids[1]
        assert _wait_solve_terminal(job_registry, loser_id) == "cancelled"
        assert len(terminated) == 1  # the loser's process tree was signalled
    finally:
        release.set()
        job_registry.shutdown()


def test_get_has_no_side_effects_on_a_running_race(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the attempt listener disarmed, a decisive attempt and a still-running
    # loser sit side by side. Polling must neither select a winner nor cancel.
    monkeypatch.setattr(
        PortfolioJobRegistry,
        "_on_attempt_terminal",
        lambda self, record, index, status: None,
    )
    loser_started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "cp-sat":
            loser_started.wait(timeout=5)
            return _solve_result("optimal", solver=solver)
        loser_started.set()
        release.wait(timeout=5)
        return _solve_result("satisfied", solver=solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        winner_id, loser_id = portfolios._records[job_id].attempt_job_ids
        assert _wait_solve_terminal(job_registry, winner_id) == "succeeded"

        states = [portfolios.get(job_id).state, portfolios.get(job_id).state]

        assert states == ["running", "running"]
        assert terminated == []
        assert job_registry.get(loser_id).state == "running"
    finally:
        release.set()
        job_registry.shutdown()


def test_attempt_finishing_during_submit_is_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    # submit_many is wrapped to return only after the attempt's listener has probed
    # record.lock, so the event is delivered while submit is still admitting the record.
    # (A terminal solve state alone is not enough: the listener runs after it.) The
    # probe must find the lock held; otherwise early events could see an empty record.
    listener_entered = threading.Event()
    lock_held_at_event: list[bool] = []
    original_listener = PortfolioJobRegistry._on_attempt_terminal

    def _spy_listener(self: PortfolioJobRegistry, record: Any, index: int, status: Any) -> None:
        acquired: bool = record.lock.acquire(blocking=False)
        if acquired:
            record.lock.release()
        lock_held_at_event.append(not acquired)
        listener_entered.set()
        original_listener(self, record, index, status)

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    monkeypatch.setattr(PortfolioJobRegistry, "_on_attempt_terminal", _spy_listener)
    _patch_solve(monkeypatch, _fake_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    real_submit_many = job_registry.submit_many

    def _submit_then_await_terminal(requests: Any, **kwargs: Any) -> list[str]:
        job_ids: list[str] = real_submit_many(requests, **kwargs)
        assert listener_entered.wait(timeout=3)
        assert lock_held_at_event == [True]
        return job_ids

    monkeypatch.setattr(job_registry, "submit_many", _submit_then_await_terminal)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
        assert final.result is not None
        assert final.result.winner_index == 0
    finally:
        job_registry.shutdown()


def test_late_attempt_completion_after_cancel_stays_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    event_handled = threading.Event()
    original_listener = PortfolioJobRegistry._on_attempt_terminal

    def _spy_listener(self: PortfolioJobRegistry, record: Any, index: int, status: Any) -> None:
        original_listener(self, record, index, status)
        event_handled.set()

    def _blocking_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result("optimal", solver=solver)

    monkeypatch.setattr(PortfolioJobRegistry, "_on_attempt_terminal", _spy_listener)
    _patch_solve(monkeypatch, _blocking_solve)

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        assert started.wait(timeout=3)
        portfolios.cancel(job_id)
        release.set()  # the attempt completes after the portfolio was cancelled
        assert event_handled.wait(timeout=3)

        status = portfolios.get(job_id)
        assert status.state == "cancelled"
        assert status.result is None
    finally:
        release.set()
        job_registry.shutdown()


def test_submit_failing_while_hashing_provenance_admits_no_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provenance hashing must finish before the attempts are admitted: a failure after
    # admission would leave attempts running that no portfolio record owns.
    def _hash_exploded(text: str) -> str:
        raise RuntimeError("hash exploded")

    def _never(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run when admission fails")

    _patch_solve(monkeypatch, _never)
    monkeypatch.setattr("openconstraint_mcp.jobs.portfolio.text_sha256", _hash_exploded)

    job_registry: JobRegistry = JobRegistry()
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        with pytest.raises(RuntimeError, match="hash exploded"):
            portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        assert job_registry.list() == []
    finally:
        job_registry.shutdown()


def _patch_broken_result_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """Attempts solve decisively, but building the race's aggregate raises."""

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    def _broken_build(*args: Any, **kwargs: Any) -> PortfolioSolveResult:
        raise RuntimeError("result build exploded")

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.portfolio_registry._build_portfolio_result", _broken_build
    )


def test_result_build_failure_finalizes_the_portfolio_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every attempt is terminal but the aggregate cannot be built: the job must still
    # reach a terminal state that reports the error, not stay `running` forever.
    _patch_broken_result_build(monkeypatch)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        final: PortfolioJobStatus = _poll(portfolios, job_id)
    finally:
        job_registry.shutdown()

    assert final.state == "failed"
    assert final.diagnostic is not None and final.diagnostic.category == "job_failed"
    assert "RuntimeError: result build exploded" in (final.message or "")


@pytest.mark.parametrize("failure_site", ["snapshot", "eviction", "listener"])
def test_completion_error_finalizes_portfolio_without_polling(
    monkeypatch: pytest.MonkeyPatch, failure_site: str
) -> None:
    _patch_solve(monkeypatch, lambda *args, **kwargs: _solve_result("optimal"))
    job_registry: JobRegistry = JobRegistry(max_running_jobs=1)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)

    def _broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("completion exploded")

    try:
        with monkeypatch.context() as patch:
            if failure_site == "snapshot":
                patch.setattr(job_registry, "_to_status", _broken)
            elif failure_site == "eviction":
                patch.setattr(job_registry, "_evict_terminal_overflow", _broken)
            else:
                patch.setattr(portfolios, "_settlement_snapshot", _broken)
            job_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
            attempt_id: str = portfolios._records[job_id].attempt_job_ids[0]
            future: Future[None] | None = job_registry._records[attempt_id].future
            assert future is not None
            future.result(timeout=3)
        final: PortfolioJobStatus = portfolios.get(job_id)
        assert final.state == "failed"
        assert "completion exploded" in (final.message or "")
    finally:
        job_registry.shutdown()


def test_completion_error_cancels_remaining_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    loser_started: threading.Event = threading.Event()
    releases: dict[str, threading.Event] = {
        "loser": threading.Event(),
        "queued": threading.Event(),
    }
    handles: dict[_FakeProc, threading.Event] = {}

    def _fake_solve(model: str, *, on_start: Any, **kwargs: Any) -> SolveResult:
        if model == "winner":
            assert loser_started.wait(timeout=3)
            return _solve_result("optimal")
        proc: _FakeProc = _FakeProc()
        handles[proc] = releases[model]
        on_start(proc)
        loser_started.set()
        releases[model].wait(timeout=5)
        return _solve_result()

    def _broken_snapshot(*args: Any) -> Any:
        raise RuntimeError("snapshot exploded")

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree", lambda proc: handles[proc].set()
    )
    job_registry: JobRegistry = JobRegistry(max_running_jobs=2)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    monkeypatch.setattr(portfolios, "_settlement_snapshot", _broken_snapshot)
    try:
        job_id: str = portfolios.submit(models=["winner", "loser", "queued"], solvers=["cp-sat"])
        attempt_ids: list[str] = portfolios._records[job_id].attempt_job_ids
        _poll(portfolios, job_id)
        states: list[str] = [_wait_solve_terminal(job_registry, job) for job in attempt_ids[1:]]
        assert states == ["cancelled", "cancelled"]
    finally:
        for release in releases.values():
            release.set()
        job_registry.shutdown()


def test_completion_error_portfolios_are_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda *args, **kwargs: _solve_result("optimal"))

    def _broken_listener(*args: Any) -> None:
        raise RuntimeError("listener exploded")

    job_registry: JobRegistry = JobRegistry(max_running_jobs=1)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry, max_retained_terminal=1)
    monkeypatch.setattr(portfolios, "_on_attempt_terminal", _broken_listener)
    try:
        first_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        _poll(portfolios, first_id)
        second_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        _poll(portfolios, second_id)
        with pytest.raises(ValueError, match="unknown portfolio job_id"):
            portfolios.get(first_id)
    finally:
        job_registry.shutdown()


def test_failed_portfolio_is_evicted_by_the_retention_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A failed race is terminal, so the retention cap bounds it like any finished job.
    _patch_broken_result_build(monkeypatch)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry, max_retained_terminal=1)
    try:
        first_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        _poll(portfolios, first_id)
        second_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        _poll(portfolios, second_id)

        with pytest.raises(ValueError, match="unknown portfolio job_id"):
            portfolios.get(first_id)
    finally:
        job_registry.shutdown()


def test_cancel_tolerates_an_evicted_terminal_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    # The attempt listener is disarmed so the finished attempt's status is never
    # cached (its event still in flight), then unrelated solves evict that attempt's
    # solve record. cancel() must skip the unknown id and still stop the live loser.
    monkeypatch.setattr(
        PortfolioJobRegistry,
        "_on_attempt_terminal",
        lambda self, record, index, status: None,
    )
    loser_started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "org.gecode.gecode":
            loser_started.set()
            release.wait(timeout=5)
        return _solve_result("optimal", solver=solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    job_registry = JobRegistry(max_running_jobs=2, max_queued_jobs=4, max_retained_terminal=1)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        finished_id, _ = portfolios._records[job_id].attempt_job_ids
        assert _wait_solve_terminal(job_registry, finished_id) == "succeeded"
        assert loser_started.wait(timeout=3)
        unrelated_id = job_registry.submit(model="solve satisfy;")
        _wait_solve_terminal(job_registry, unrelated_id)
        with pytest.raises(ValueError, match="unknown job_id"):
            job_registry.get(finished_id)  # evicted by the retention cap

        cancelled = portfolios.cancel(job_id)

        assert cancelled.state == "cancelled"
        assert len(terminated) == 1  # the still-running loser was stopped
    finally:
        release.set()
        job_registry.shutdown()


def _patch_decisive_winner_over_running_loser(
    monkeypatch: pytest.MonkeyPatch, loser_release: threading.Event
) -> None:
    """cp-sat turns decisive once the gecode loser runs; the loser runs until released."""
    loser_started: threading.Event = threading.Event()

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if solver == "cp-sat":
            loser_started.wait(timeout=5)
            return _solve_result("optimal", solver=solver)
        loser_started.set()
        loser_release.wait(timeout=10)
        return _solve_result("satisfied", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)


def test_decisive_winner_is_reported_while_a_loser_is_still_being_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Termination is the autouse no-op, so the loser outlives the decisive result the way
    # a slow-dying process tree would. The winner must be reported without waiting for it.
    loser_release: threading.Event = threading.Event()
    _patch_decisive_winner_over_running_loser(monkeypatch, loser_release)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        final: PortfolioJobStatus = _poll(portfolios, job_id, timeout=2.0)

        assert final.result is not None
        assert final.result.winner_index == 0
    finally:
        loser_release.set()
        job_registry.shutdown()


def test_settled_race_reports_a_loser_still_being_cancelled_as_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The attempts table is a snapshot taken when the race settled: a loser that has not
    # stopped yet must not be reported with a fate it has not reached.
    loser_release: threading.Event = threading.Event()
    _patch_decisive_winner_over_running_loser(monkeypatch, loser_release)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        final: PortfolioJobStatus = _poll(portfolios, job_id, timeout=2.0)

        assert final.result is not None
        assert final.result.attempts[1].state == "running"
    finally:
        loser_release.set()
        job_registry.shutdown()


def test_cancel_after_a_decisive_winner_keeps_the_winning_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Once the loser's teardown has begun the race has a winner; a client cancel that
    # arrives then must not discard it.
    loser_release: threading.Event = threading.Event()
    loser_teardown: threading.Event = threading.Event()
    _patch_decisive_winner_over_running_loser(monkeypatch, loser_release)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: loser_teardown.set(),
    )
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        assert loser_teardown.wait(timeout=3)

        cancelled: PortfolioJobStatus = portfolios.cancel(job_id)

        assert cancelled.state == "succeeded"
    finally:
        loser_release.set()
        job_registry.shutdown()


def test_loser_teardown_does_not_occupy_the_winners_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tearing down a loser's process tree can take seconds, and admission already counts
    # the winner's slot free. On a 2-worker pool with the loser still running, an
    # unrelated job admitted as `running` can only actually run on the winner's worker.
    loser_release: threading.Event = threading.Event()
    teardown_started: threading.Event = threading.Event()
    teardown_may_finish: threading.Event = threading.Event()

    def _slow_terminate(proc: Any, **kwargs: Any) -> None:
        teardown_started.set()
        teardown_may_finish.wait(timeout=10)

    _patch_decisive_winner_over_running_loser(monkeypatch, loser_release)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _slow_terminate)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=2)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"])
        assert teardown_started.wait(timeout=3)

        unrelated_id: str = job_registry.submit(model="solve satisfy;", solver="cp-sat")

        assert _wait_solve_terminal(job_registry, unrelated_id, timeout=2.0) == "succeeded"
    finally:
        teardown_may_finish.set()
        loser_release.set()
        job_registry.shutdown()


def test_decisive_winner_cancels_a_queued_loser_before_it_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # On a 2-worker pool the chuffed attempt queues behind the running cp-sat winner and
    # gecode loser. The winner's freed worker takes the next queued item as soon as it
    # returns, so the queued loser must already be cancelled by then — not left for a
    # cancel that waits behind the running loser's (held-open) teardown.
    loser_started: threading.Event = threading.Event()
    teardown_started: threading.Event = threading.Event()
    teardown_may_finish: threading.Event = threading.Event()
    solved: list[str] = []

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        solved.append(solver)
        on_start(_FakeProc())
        if solver == "cp-sat":
            loser_started.wait(timeout=5)
            return _solve_result("optimal", solver=solver)
        loser_started.set()
        teardown_may_finish.wait(timeout=10)
        return _solve_result("satisfied", solver=solver)

    def _slow_terminate(proc: Any, **kwargs: Any) -> None:
        teardown_started.set()
        teardown_may_finish.wait(timeout=10)

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _slow_terminate)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=2)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"],
            solvers=["cp-sat", "org.gecode.gecode", "org.chuffed.chuffed"],
        )
        queued_id: str = portfolios._records[job_id].attempt_job_ids[2]
        assert teardown_started.wait(timeout=3)

        assert job_registry.get(queued_id).state == "cancelled"
        assert "org.chuffed.chuffed" not in solved
    finally:
        teardown_may_finish.set()
        job_registry.shutdown()


def test_cancel_still_stops_later_attempts_when_one_teardown_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A teardown error on one attempt (an OSError from the platform kill) must not leave
    # the remaining attempts running.
    started: dict[str, threading.Event] = {
        "cp-sat": threading.Event(),
        "org.gecode.gecode": threading.Event(),
    }
    release: threading.Event = threading.Event()
    teardown_calls: list[Any] = []

    def _blocking_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started[solver].set()
        release.wait(timeout=10)
        return _solve_result("satisfied", solver=solver)

    def _first_teardown_raises(proc: Any, **kwargs: Any) -> None:
        teardown_calls.append(proc)
        if len(teardown_calls) == 1:
            raise OSError("teardown failed")

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree", _first_teardown_raises
    )
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        assert all(event.wait(timeout=3) for event in started.values())

        portfolios.cancel(job_id)

        assert len(teardown_calls) == 2
    finally:
        release.set()
        job_registry.shutdown()


def test_finished_portfolio_releases_its_attempt_statuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A cached attempt status carries its full solver output (up to the 1 MiB cap). Once
    # the result is built they are dead weight on every retained record.
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    _patch_solve(monkeypatch, _fake_solve)
    job_registry: JobRegistry = JobRegistry(max_running_jobs=4)
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        _poll(portfolios, job_id)

        assert portfolios._records[job_id].statuses == []
    finally:
        job_registry.shutdown()


def test_decisive_race_waits_for_an_evicted_attempts_own_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # With solve retention 1, the decisive attempt's own finalize evicts an attempt that
    # already finished but whose terminal event has not landed yet. That status cannot be
    # read back, so the race must settle when the event arrives, not fail the job.
    first_event_arrived: threading.Event = threading.Event()
    decisive_event_handled: threading.Event = threading.Event()
    original_listener = PortfolioJobRegistry._on_attempt_terminal

    def _deliver_first_event_last(
        self: PortfolioJobRegistry, record: Any, index: int, status: Any
    ) -> None:
        if index == 0:
            first_event_arrived.set()
            decisive_event_handled.wait(timeout=5)
            original_listener(self, record, index, status)
            return
        original_listener(self, record, index, status)
        decisive_event_handled.set()

    class _ExitedProc(_FakeProc):
        # Retention eviction reaps an evicted record's handle via poll().
        def poll(self) -> int:
            return 0

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_ExitedProc())
        if solver == "cp-sat":
            return _solve_result("unknown", solver=solver)
        first_event_arrived.wait(timeout=5)
        return _solve_result("optimal", solver=solver)

    monkeypatch.setattr(PortfolioJobRegistry, "_on_attempt_terminal", _deliver_first_event_last)
    _patch_solve(monkeypatch, _fake_solve)
    job_registry: JobRegistry = JobRegistry(
        max_running_jobs=2, max_queued_jobs=4, max_retained_terminal=1
    )
    portfolios: PortfolioJobRegistry = PortfolioJobRegistry(job_registry)
    try:
        job_id: str = portfolios.submit(
            models=["solve satisfy;"], solvers=["cp-sat", "org.gecode.gecode"]
        )
        final: PortfolioJobStatus = _poll(portfolios, job_id)

        assert final.result is not None
        assert final.result.winner_index == 1
    finally:
        job_registry.shutdown()
