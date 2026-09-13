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
from typing import Any

import pytest

from openconstraint_mcp.jobs.portfolio_registry import PortfolioJobRegistry
from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.schemas.minizinc import SolveResult
from openconstraint_mcp.schemas.portfolio import PortfolioSolveControls, PortfolioSolveResult
from openconstraint_mcp.shared.job_errors import JobRejectedError

_TERMINAL = {"succeeded", "cancelled"}
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
    # deadline wait below only reads (get() has no side effects).
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
        final = _poll(portfolios, job_id)
        assert final.state == "succeeded"
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


def test_result_build_error_surfaces_from_get(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        return _solve_result("optimal", solver=solver)

    def _broken_build(*args: Any, **kwargs: Any) -> PortfolioSolveResult:
        raise RuntimeError("result build exploded")

    _patch_solve(monkeypatch, _fake_solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.portfolio_registry._build_portfolio_result", _broken_build
    )

    job_registry = JobRegistry(max_running_jobs=4)
    portfolios = PortfolioJobRegistry(job_registry)
    try:
        job_id = portfolios.submit(models=["solve satisfy;"], solvers=["cp-sat"])
        raised: RuntimeError | None = None
        deadline = time.monotonic() + 5.0
        while raised is None and time.monotonic() < deadline:
            try:
                portfolios.get(job_id)
            except RuntimeError as exc:
                raised = exc
            else:
                time.sleep(0.01)
        assert raised is not None
        assert "result build exploded" in str(raised)
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
