from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from typing import Any

import pytest

from openconstraint_mcp.jobs.registry import JobRegistry, SolveRequest, _JobRecord
from openconstraint_mcp.minizinc.core import build_solve_extra_args, prepare_solve_args
from openconstraint_mcp.schemas.minizinc import (
    SolveControls,
    SolveJobStatus,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
)
from openconstraint_mcp.shared.job_errors import JobRejectedError, UnknownJobError
from tests.minizinc.helpers import STREAM_SATISFY, child_result


def _request(model: str = "solve satisfy;", **overrides: Any) -> SolveRequest:
    """Build a prepared request the way a portfolio does: controls, then their argv."""
    solver: str = overrides.pop("solver", "cp-sat")
    controls = SolveControls(
        **{
            name: overrides.pop(name)
            for name in list(overrides)
            if name in SolveControls.model_fields
        }
    )
    fields: dict[str, Any] = {
        "model": model,
        "solver": solver,
        "data": None,
        "checker": None,
        "timeout_ms": 30000,
        "controls": controls,
        "extra_args": build_solve_extra_args(solver, controls),
    }
    fields.update(overrides)
    return SolveRequest(**fields)


def _patch_list_solvers(monkeypatch: pytest.MonkeyPatch, caps: SolverCapabilities) -> list[int]:
    """Point the admission resolver's ``list_solvers`` at one ``cp-sat`` entry.

    Returns a single-element counter of resolver invocations so a test can assert
    a gated-control job resolves capabilities exactly once (at admission, not the
    worker — the worker's ``run_prepared_solve`` is mocked away here anyway).
    """
    calls = [0]

    def _fake_list_solvers() -> SolverList:
        calls[0] += 1
        return SolverList(solvers=[SolverInfo(id="cp-sat", name="cp-sat", capabilities=caps)])

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.list_solvers", _fake_list_solvers)
    return calls


def _solve_result(status: str = "satisfied") -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x = 1;\n",
        stderr="",
        elapsed_ms=3,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=None,
    )


class _FakeProc:
    """An opaque process-handle stand-in passed through on_start/terminate.

    It has no ``pid`` and backs no real process, so the real (group-aware)
    ``_terminate_process_tree`` must never see it — the autouse
    ``_never_terminate_for_real`` fixture below guarantees that.

    Defaults to looking unreaped (``returncode = None``); set ``returncode = 0``
    to model a child the registry has already reaped.
    """

    returncode: Any = None

    def poll(self) -> Any:
        return self.returncode


@pytest.fixture(autouse=True)
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every proc in this file is a ``_FakeProc``; the real group-aware
    terminate would probe ``os.getpgid``/``os.killpg`` on it. Tests that
    assert termination re-patch a recorder over this via ``_patch_terminate``.
    """
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _wait_until_terminal(registry: JobRegistry, job_id: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


def _patch_solve(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.jobs.registry.run_prepared_solve", fake)


def _patch_terminate(monkeypatch: pytest.MonkeyPatch, recorder: list[Any]) -> None:
    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        recorder.append(proc)

    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)


def test_submit_returns_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="var 1..5: x;\nsolve satisfy;")
        assert isinstance(job_id, str)
        assert job_id
    finally:
        registry.shutdown()


def test_status_reports_requested_timeout_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    # The status echoes the caller's solve time-limit so a polling client can pace
    # against it (remaining = timeout_ms - elapsed_ms) instead of guessing.
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;", timeout_ms=45000)
        assert registry.get(job_id).timeout_ms == 45000
    finally:
        registry.shutdown()


def test_fast_solve_reaches_succeeded_with_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result("optimal"))
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
    finally:
        registry.shutdown()


def test_solve_status_error_reaches_succeeded_not_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    # D1.9: a structured solver `error` verdict is a SUCCEEDED job with the result
    # attached — `failed` is reserved for the absence of a result.
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result("error"))
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "error"
    finally:
        registry.shutdown()


def test_timed_out_result_reaches_timeout_state(monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out = SolveResult(
        status="timeout",
        solver="cp-sat",
        return_code=None,
        timed_out=True,
        stdout="",
        stderr="",
        elapsed_ms=9,
    )
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: timed_out)
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "timeout"
        assert registry.get(job_id).result is not None
    finally:
        registry.shutdown()


def test_runner_exception_reaches_failed_with_none_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise RuntimeError("managed binary blew up")

    _patch_solve(monkeypatch, _boom)
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert _wait_until_terminal(registry, job_id) == "failed"
        status = registry.get(job_id)
        assert status.result is None
        assert status.message is not None
        assert "blew up" in status.message
    finally:
        registry.shutdown()


def test_cancel_running_job_reaches_cancelled_and_terminates_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = threading.Event()
    release = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        on_start(proc)
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the "process" dying unblocks the solve

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)
        registry.cancel(job_id)
        assert _wait_until_terminal(registry, job_id) == "cancelled"
        assert terminated == handles
        assert registry.get(job_id).result is None
    finally:
        release.set()
        registry.shutdown()


def test_cancelled_running_job_whose_solve_then_raises_reaches_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Killing the child can make the solve itself raise (say, while reading the dead
    # process's output). The client asked for the stop, so the job must not read failed.
    started: threading.Event = threading.Event()
    killed: threading.Event = threading.Event()

    def _solve_raising_once_killed(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        killed.wait(timeout=5)
        raise RuntimeError("lost the killed child's output")

    _patch_solve(monkeypatch, _solve_raising_once_killed)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: killed.set(),
    )
    registry: JobRegistry = JobRegistry()
    try:
        job_id: str = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)

        registry.cancel(job_id)

        assert _wait_until_terminal(registry, job_id) == "cancelled"
    finally:
        killed.set()
        registry.shutdown()


def test_cancel_if_queued_drops_a_job_no_worker_has_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(model="solve satisfy;")  # occupies the only worker
        assert started.wait(timeout=3)
        queued_id: str = registry.submit(model="solve satisfy;")

        dropped: bool = registry.cancel_if_queued(queued_id)

        assert (dropped, registry.get(queued_id).state) == (True, "cancelled")
    finally:
        release.set()
        registry.shutdown()


def test_cancel_if_queued_leaves_a_started_job_running(monkeypatch: pytest.MonkeyPatch) -> None:
    # The caller defers a job a worker already took to a real cancel; this one must
    # neither block on a process teardown nor mark the job.
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    terminated: list[Any] = []
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))
    _patch_terminate(monkeypatch, terminated)
    registry: JobRegistry = JobRegistry()
    try:
        job_id: str = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)

        dropped: bool = registry.cancel_if_queued(job_id)

        assert (
            dropped,
            registry.get(job_id).state,
            registry._records[job_id].cancel_requested,
            terminated,
        ) == (False, "running", False, [])
    finally:
        release.set()
        registry.shutdown()


def test_submit_resolves_capabilities_once_at_admission(monkeypatch: pytest.MonkeyPatch) -> None:
    # A gated-control job resolves the capability map exactly once, at admission;
    # the worker trusts that and never re-resolves (D1/D2).
    resolve_calls = _patch_list_solvers(monkeypatch, SolverCapabilities(supports_free_search=True))
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;", controls=SolveControls(free_search=True))
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        assert resolve_calls[0] == 1
    finally:
        registry.shutdown()


def test_worker_runs_the_admission_prepared_extra_args(monkeypatch: pytest.MonkeyPatch) -> None:
    # The worker never rebuilds the argv: it runs exactly what admission prepared.
    _patch_list_solvers(
        monkeypatch, SolverCapabilities(supports_free_search=True, supports_parallel=True)
    )
    captured: list[tuple[str, ...]] = []

    def _fake_solve(
        model: str, *, extra_args: tuple[str, ...], on_start: Any, **kw: Any
    ) -> SolveResult:
        captured.append(tuple(extra_args))
        return _solve_result()

    _patch_solve(monkeypatch, _fake_solve)
    controls = SolveControls(free_search=True, parallel=2)
    registry = JobRegistry()
    try:
        job_id = registry.submit(model="solve satisfy;", controls=controls)
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        assert captured == [prepare_solve_args("cp-sat", controls)]
    finally:
        registry.shutdown()


def test_gated_control_job_resolves_capabilities_only_at_admission(
    fake_minizinc_binary: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With the real worker solve (only the child executor is faked), a gated-control
    # job runs --solvers-json exactly once: at admission, never in the worker.
    resolve_calls = _patch_list_solvers(monkeypatch, SolverCapabilities(supports_free_search=True))
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *args, **kwargs: child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    registry = JobRegistry()
    try:
        job_id = registry.submit(
            model="var 1..5: x;\nsolve satisfy;", controls=SolveControls(free_search=True)
        )
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        assert resolve_calls[0] == 1
    finally:
        registry.shutdown()


def test_submit_rejects_unsupported_control_before_creating_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unsupported control is rejected at admission before any job record exists
    # and before a worker/solve is created.
    _patch_list_solvers(monkeypatch, SolverCapabilities())

    def _fail_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("worker solve must not run for a rejected control")

    _patch_solve(monkeypatch, _fail_solve)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="free_search"):
            registry.submit(model="solve satisfy;", controls=SolveControls(free_search=True))
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_many_admits_whole_batch_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry(max_running_jobs=4)
    try:
        job_ids = registry.submit_many(
            [_request(solver="cp-sat"), _request(solver="org.gecode.gecode")]
        )
        assert len(job_ids) == 2
        assert len(set(job_ids)) == 2
        for job_id in job_ids:
            assert _wait_until_terminal(registry, job_id) == "succeeded"
    finally:
        registry.shutdown()


def test_submit_many_rejects_whole_batch_when_over_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Atomic admission (D8): a batch that would exceed running+queued capacity
    # admits NONE — no record is created and in_flight is unchanged.
    release = threading.Event()
    started = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=1)  # capacity 2
    try:
        registry.submit(model="solve satisfy;")  # occupies the running slot
        assert started.wait(timeout=3)
        with pytest.raises(JobRejectedError):
            registry.submit_many([_request(), _request()])  # 1 + 2 > 2 → reject all
        # Nothing from the rejected batch was admitted: only the one running job.
        assert len(registry.list()) == 1
    finally:
        release.set()
        registry.shutdown()


def test_submit_many_admits_nothing_when_a_worker_thread_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ThreadPoolExecutor.submit raises when it cannot start a worker thread, after the
    # batch's earlier jobs were already handed to the pool. The batch must still admit
    # none: no job may run or notify, and no admission slot may leak.
    solved: list[str] = []
    submits: list[Future[Any]] = []

    def _recording_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        solved.append(model)
        return _solve_result()

    _patch_solve(monkeypatch, _recording_solve)
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    real_submit: Callable[..., Future[Any]] = registry._executor.submit

    def _thread_start_fails_on_second_job(fn: Callable[..., Any], /, *args: Any) -> Future[Any]:
        # Like the real submit, the work item is queued before the thread start fails.
        future: Future[Any] = real_submit(fn, *args)
        submits.append(future)
        if len(submits) == 2:
            raise RuntimeError("can't start new thread")
        return future

    monkeypatch.setattr(registry._executor, "submit", _thread_start_fails_on_second_job)
    try:
        with pytest.raises(RuntimeError, match="can't start new thread"):
            registry.submit_many([_request("first"), _request("second")], on_terminal=listener)
    finally:
        registry.shutdown()  # joins the worker both jobs were handed to

    assert (registry.list(), registry._in_flight, solved, events) == ([], 0, [], [])


def test_submit_many_validates_every_request_before_admitting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Requests arrive with their argv prepared, but submit_many still rejects a bad
    # model/timeout anywhere in the batch before any job is created.
    def _fail_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no worker should run when a batch request is invalid")

    _patch_solve(monkeypatch, _fail_solve)
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="model must not be empty"):
            registry.submit_many([_request(), _request(model="")])
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_get_unknown_job_id_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = JobRegistry()
    try:
        with pytest.raises(ValueError, match="unknown"):
            registry.get("does-not-exist")
    finally:
        registry.shutdown()


def test_submit_beyond_running_capacity_enqueues(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    started = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=2)
    try:
        running_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)
        queued_id = registry.submit(model="solve satisfy;")
        # The second submit cannot run (the only worker is busy) → it waits queued.
        assert registry.get(queued_id).state == "queued"
        assert registry.get(running_id).state == "running"
    finally:
        release.set()
        registry.shutdown()


@pytest.mark.parametrize("batch", [False, True])
def test_job_waiting_for_terminal_listener_starts_timing_only_on_worker(
    monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    listener_entered: threading.Event = threading.Event()
    release_listener: threading.Event = threading.Event()
    started: threading.Event = threading.Event()
    release_solve: threading.Event = threading.Event()
    clock_ms: list[int] = [1000]

    def _solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        if model == "second":
            started.set()
            release_solve.wait(timeout=5)
        return _solve_result()

    def _listener(index: int, status: SolveJobStatus) -> None:
        listener_entered.set()
        release_listener.wait(timeout=5)

    _patch_solve(monkeypatch, _solve)
    monkeypatch.setattr("openconstraint_mcp.shared.job_registry.now_ms", lambda: clock_ms[0])
    monkeypatch.setattr("openconstraint_mcp.jobs.registry.now_ms", lambda: clock_ms[0])
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=0)
    try:
        registry.submit_many([_request()], on_terminal=_listener)
        assert listener_entered.wait(timeout=3)
        job_id: str
        if batch:
            (job_id,) = registry.submit_many([_request("second")])
        else:
            job_id = registry.submit(model="second")
        clock_ms[0] = 1750
        queued: SolveJobStatus = registry.get(job_id)
        assert not started.is_set()
        assert (queued.state, queued.started_at_ms, queued.elapsed_ms) == ("queued", None, None)

        release_listener.set()
        assert started.wait(timeout=3)
        clock_ms[0] = 1800
        running: SolveJobStatus = registry.get(job_id)
        assert (running.state, running.started_at_ms, running.elapsed_ms) == ("running", 1750, 50)
    finally:
        release_listener.set()
        release_solve.set()
        registry.shutdown()


def test_submit_beyond_queue_capacity_rejects_without_starting_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    started = threading.Event()
    solve_calls = 0
    lock = threading.Lock()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        nonlocal solve_calls
        with lock:
            solve_calls += 1
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=1)
    try:
        registry.submit(model="solve satisfy;")  # running
        assert started.wait(timeout=3)
        registry.submit(model="solve satisfy;")  # queued (fills the 1-slot queue)
        with pytest.raises(JobRejectedError):
            registry.submit(model="solve satisfy;")  # over capacity → rejected
        # The rejected submit must not have started a worker/solve.
        with lock:
            assert solve_calls == 1
    finally:
        release.set()
        registry.shutdown()


def test_retention_cap_evicts_oldest_terminal_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    # max_running_jobs=1 forces sequential completion, so "oldest terminal" is
    # deterministic (submission order == completion order).
    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=2)
    try:
        first = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, first)
        second = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, second)
        third = registry.submit(model="solve satisfy;")
        _wait_until_terminal(registry, third)

        with pytest.raises(ValueError, match="unknown"):
            registry.get(first)
        assert registry.get(second).state == "succeeded"
        assert registry.get(third).state == "succeeded"
        assert len(registry.list()) == 2
    finally:
        registry.shutdown()


def test_list_returns_one_status_per_retained_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry = JobRegistry(max_running_jobs=2)
    try:
        ids = {registry.submit(model="solve satisfy;") for _ in range(3)}
        for job_id in ids:
            _wait_until_terminal(registry, job_id)
        listed = {status.job_id for status in registry.list()}
        assert listed == ids
    finally:
        registry.shutdown()


def test_running_job_reports_advancing_elapsed_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    # Contract (README / SolveJobStatus docstring): while a job is `running` only
    # `state` and `elapsed_ms` advance. elapsed_ms is frozen at finalize, so a
    # live read must derive it from started_at_ms rather than the stored field.
    started = threading.Event()
    release = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    _patch_solve(monkeypatch, _blocking_solve)
    registry = JobRegistry(max_running_jobs=1)
    try:
        job_id = registry.submit(model="solve satisfy;")
        assert started.wait(timeout=3)

        first = registry.get(job_id)
        assert first.state == "running"
        assert first.elapsed_ms is not None

        time.sleep(0.03)
        second = registry.get(job_id)
        assert second.state == "running"
        assert second.elapsed_ms is not None
        assert second.elapsed_ms > first.elapsed_ms  # advances between reads
    finally:
        release.set()
        registry.shutdown()


def test_shutdown_terminates_a_running_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        on_start(proc)
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # let the blocked worker unwind so shutdown can join it

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    registry = JobRegistry()
    registry.submit(model="solve satisfy;")
    assert started.wait(timeout=3)
    registry.shutdown()

    assert terminated == handles


def test_shutdown_finalizes_a_queued_job_as_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    # A queued (never-started) job must not be left in `queued` after shutdown:
    # its future is cancellable, so shutdown finalizes it as `cancelled`.
    started = threading.Event()
    release = threading.Event()

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()  # unblock the running worker so shutdown can join the pool

    _patch_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    registry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    registry.submit(model="solve satisfy;")  # occupies the only worker
    assert started.wait(timeout=3)
    queued_id = registry.submit(model="solve satisfy;")  # cannot start → queued
    assert registry.get(queued_id).state == "queued"

    registry.shutdown()

    status = registry.get(queued_id)
    assert status.state == "cancelled"
    assert status.result is None


def test_shutdown_terminates_a_child_launched_after_its_handle_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Launch-window race: a worker is `running` (its future can no longer be
    # cancelled) but has not yet recorded its process handle when shutdown takes
    # its handle snapshot. shutdown must still stop that child — it marks the
    # record cancel_requested, so the worker's own on_start terminates the process
    # at launch instead of letting shutdown block on the full solve timeout.
    worker_running = threading.Event()
    handles: list[Any] = []
    terminated: list[Any] = []
    registry = JobRegistry(max_running_jobs=1)

    def _racing_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        proc = _FakeProc()
        handles.append(proc)
        worker_running.set()  # state == running; handle NOT yet recorded
        # Order the "launch" strictly after shutdown's handle snapshot: wait until
        # shutdown has marked this job cancel_requested. Bounded, so the pre-fix
        # bug surfaces as an assertion failure below rather than hanging here.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with registry._lock:
                record = next(iter(registry._records.values()), None)
                marked = record is not None and record.cancel_requested
            if marked:
                break
            time.sleep(0.001)
        on_start(proc)  # records handle; terminates iff cancel_requested is set
        return _solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)

    _patch_solve(monkeypatch, _racing_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    job_id = registry.submit(model="solve satisfy;")
    assert worker_running.wait(timeout=3)

    shutdown_done = threading.Event()

    def _run_shutdown() -> None:
        registry.shutdown()
        shutdown_done.set()

    threading.Thread(target=_run_shutdown, name="shutdown").start()

    assert shutdown_done.wait(timeout=5), "shutdown hung waiting on the launching child"
    assert set(terminated) == set(handles)  # the late-launched child was terminated
    assert registry.get(job_id).state == "cancelled"


def test_submit_after_shutdown_is_rejected() -> None:
    registry: JobRegistry = JobRegistry()
    registry.shutdown()

    with pytest.raises(JobRejectedError, match="shutting down"):
        registry.submit(model="solve satisfy;")
    assert registry.list() == []


def test_submit_many_after_shutdown_is_rejected_without_listener_events() -> None:
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    registry.shutdown()

    with pytest.raises(JobRejectedError, match="shutting down"):
        registry.submit_many([_request(), _request()], on_terminal=listener)
    assert registry.list() == []
    assert events == []


def test_submit_during_shutdown_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Admission window race: a submit that runs after shutdown has marked the live
    # jobs (and taken its record snapshot) but before the pool is torn down must be
    # rejected. Otherwise its record is missing from the snapshot, is never
    # finalized, and stays `queued` (or starts unflagged and stalls teardown).
    worker_running: threading.Event = threading.Event()
    inner_done: threading.Event = threading.Event()
    inner: list[str | JobRejectedError] = []
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)

    def _submitting_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        if inner:
            return _solve_result()
        worker_running.set()
        deadline: float = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with registry._lock:
                marked: bool = all(r.cancel_requested for r in registry._records.values())
            if marked:
                break
            time.sleep(0.001)
        try:
            inner.append(registry.submit(model="solve satisfy;"))
        except JobRejectedError as exc:
            inner.append(exc)
        finally:
            inner_done.set()
        return _solve_result()

    def _terminate_after_inner_submit(proc: Any, **kwargs: Any) -> None:
        # Pin the inner submit inside the window: shutdown terminates handles before
        # tearing down the pool, so it cannot reach executor.shutdown until then.
        inner_done.wait(timeout=3)

    _patch_solve(monkeypatch, _submitting_solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree", _terminate_after_inner_submit
    )

    registry.submit(model="solve satisfy;")
    assert worker_running.wait(timeout=3)
    shutdown_done: threading.Event = threading.Event()

    def _run_shutdown() -> None:
        registry.shutdown()
        shutdown_done.set()

    threading.Thread(target=_run_shutdown, name="shutdown").start()

    assert shutdown_done.wait(timeout=5), "shutdown hung"
    assert len(inner) == 1 and isinstance(inner[0], JobRejectedError)
    assert len(registry.list()) == 1


# --- terminal listener (submit_many on_terminal) ---------------------------


_Events = list[tuple[int, SolveJobStatus]]


def _recording_listener() -> tuple[_Events, Callable[[int, SolveJobStatus], None]]:
    events: _Events = []

    def _listener(index: int, status: SolveJobStatus) -> None:
        events.append((index, status))

    return events, _listener


def _wait_for_events(events: _Events, count: int, timeout: float = 3.0) -> None:
    deadline: float = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(events) >= count:
            return
        time.sleep(0.005)
    raise AssertionError(f"expected {count} listener event(s) within {timeout}s, got {len(events)}")


def _summaries(events: _Events) -> list[tuple[int, str, str]]:
    return [(index, status.job_id, status.state) for index, status in events]


def _blocking_solve_until(release: threading.Event, started: threading.Event) -> Any:
    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_FakeProc())
        started.set()
        release.wait(timeout=5)
        return _solve_result()

    return _blocking_solve


def test_on_terminal_fires_once_for_succeeded_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    try:
        (job_id,) = registry.submit_many([_request()], on_terminal=listener)
        _wait_for_events(events, 1)
    finally:
        registry.shutdown()

    assert _summaries(events) == [(0, job_id, "succeeded")]


def test_on_terminal_fires_once_for_timeout_job(monkeypatch: pytest.MonkeyPatch) -> None:
    timed_out: SolveResult = SolveResult(
        status="timeout",
        solver="cp-sat",
        return_code=None,
        timed_out=True,
        stdout="",
        stderr="",
        elapsed_ms=9,
    )
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: timed_out)
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    try:
        (job_id,) = registry.submit_many([_request()], on_terminal=listener)
        _wait_for_events(events, 1)
    finally:
        registry.shutdown()

    assert _summaries(events) == [(0, job_id, "timeout")]


def test_on_terminal_fires_once_for_failed_job(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise RuntimeError("managed binary blew up")

    _patch_solve(monkeypatch, _boom)
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    try:
        (job_id,) = registry.submit_many([_request()], on_terminal=listener)
        _wait_for_events(events, 1)
    finally:
        registry.shutdown()

    assert _summaries(events) == [(0, job_id, "failed")]


def test_on_terminal_fires_once_for_job_cancelled_before_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(model="solve satisfy;")  # occupies the only worker
        assert started.wait(timeout=3)
        (queued_id,) = registry.submit_many([_request()], on_terminal=listener)
        registry.cancel(queued_id)
    finally:
        release.set()
        registry.shutdown()

    assert _summaries(events) == [(0, queued_id, "cancelled")]


def test_on_terminal_fires_once_for_job_cancelled_while_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()  # the "process" dying unblocks the solve

    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry()
    try:
        (job_id,) = registry.submit_many([_request()], on_terminal=listener)
        assert started.wait(timeout=3)
        registry.cancel(job_id)
        _wait_for_events(events, 1)
    finally:
        release.set()
        registry.shutdown()

    assert _summaries(events) == [(0, job_id, "cancelled")]


def test_on_terminal_fires_once_for_queued_job_at_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()  # unblock the running worker so shutdown can join the pool

    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(model="solve satisfy;")  # occupies the only worker
        assert started.wait(timeout=3)
        (queued_id,) = registry.submit_many([_request()], on_terminal=listener)
    finally:
        registry.shutdown()

    assert _summaries(events) == [(0, queued_id, "cancelled")]


def test_on_terminal_reports_batch_index_for_instant_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    # Instant fakes can finish before submit_many returns its ids, so the index must
    # come from the registry rather than a caller-side job_id lookup.
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry(max_running_jobs=4)
    try:
        job_ids: list[str] = registry.submit_many(
            [_request(), _request(), _request()], on_terminal=listener
        )
        _wait_for_events(events, 3)
    finally:
        registry.shutdown()

    assert sorted((index, status.job_id) for index, status in events) == list(enumerate(job_ids))


class _BarrierFuture(Future[None]):
    """Wraps a queued job's future so two concurrent cancels both pass the
    terminal-state check before either one finalizes — the race the exactly-once
    notify guards against (``Future.cancel`` is True for an already-cancelled future).
    """

    def __init__(self, inner: Future[None], barrier: threading.Barrier) -> None:
        super().__init__()
        self._inner = inner
        self._barrier = barrier

    def cancel(self) -> bool:
        self._barrier.wait()
        return self._inner.cancel()


def test_on_terminal_fires_once_when_a_queued_job_is_cancelled_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))
    events, listener = _recording_listener()
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(model="solve satisfy;")  # occupies the only worker
        assert started.wait(timeout=3)
        (queued_id,) = registry.submit_many([_request()], on_terminal=listener)
        with registry._lock:
            record: _JobRecord = registry._records[queued_id]
            inner: Future[None] | None = record.future
            assert inner is not None
            record.future = _BarrierFuture(inner, threading.Barrier(2, timeout=3))
        cancellers: list[threading.Thread] = [
            threading.Thread(target=registry.cancel, args=(queued_id,)) for _ in range(2)
        ]
        for canceller in cancellers:
            canceller.start()
        for canceller in cancellers:
            canceller.join(timeout=5)
        record.future = inner
    finally:
        release.set()
        registry.shutdown()

    assert _summaries(events) == [(0, queued_id, "cancelled")]


def test_on_terminal_listener_may_call_get_and_cancel_without_deadlock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    done: threading.Event = threading.Event()
    registry: JobRegistry = JobRegistry()

    def _reentrant_listener(index: int, status: SolveJobStatus) -> None:
        registry.get(status.job_id)
        registry.cancel(status.job_id)
        done.set()

    registry.submit_many([_request()], on_terminal=_reentrant_listener)
    completed: bool = done.wait(timeout=3)
    if completed:  # a deadlocked worker would hang the pool join below
        registry.shutdown()
    assert completed, "listener deadlocked calling back into the registry"


# --- terminal listener failures ------------------------------------------------


@pytest.mark.parametrize("completion_path", ["worker", "failed_worker", "cancel", "shutdown"])
def test_snapshot_error_notifies_owner_once_outside_lock(
    monkeypatch: pytest.MonkeyPatch, completion_path: str
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    registry: JobRegistry = JobRegistry(max_running_jobs=1)
    errors: list[tuple[int, str]] = []
    original_status: Any = registry._to_status

    def _snapshot(record: Any) -> SolveJobStatus:
        if record.request.model == "target":
            raise RuntimeError("snapshot exploded")
        return original_status(record)

    def _on_error(index: int, message: str) -> None:
        # Re-entry would deadlock if completion invoked this under registry._lock.
        with registry._lock:
            errors.append((index, message))

    def _solve(model: str, *, on_start: Any, **kwargs: Any) -> SolveResult:
        if model == "blocker":
            on_start(_FakeProc())
            started.set()
            release.wait(timeout=5)
        elif completion_path == "failed_worker":
            raise RuntimeError("solver exploded")
        return _solve_result()

    _patch_solve(monkeypatch, _solve)
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree", lambda proc: release.set()
    )
    monkeypatch.setattr(registry, "_to_status", _snapshot)
    try:
        if completion_path in {"cancel", "shutdown"}:
            registry.submit(model="blocker")
            assert started.wait(timeout=3)
        job_ids: list[str] = registry.submit_many(
            [_request("target"), _request("other")], on_terminal_error=_on_error
        )
        if completion_path == "cancel":
            with pytest.raises(RuntimeError, match="snapshot exploded"):
                registry.cancel(job_ids[0])
        elif completion_path == "shutdown":
            registry.shutdown()
        else:
            future: Future[None] | None = registry._records[job_ids[0]].future
            assert future is not None
            future.result(timeout=3)
    finally:
        release.set()
        registry.shutdown()
    assert errors == [(0, "RuntimeError: snapshot exploded")]


def _raising_listener(index: int, status: SolveJobStatus) -> None:
    raise RuntimeError("listener exploded")


def test_shutdown_finalizes_every_queued_job_when_the_listener_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        release.set()  # unblock the running worker so shutdown can join the pool

    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    registry.submit(model="solve satisfy;")  # occupies the only worker
    assert started.wait(timeout=3)
    queued_ids: list[str] = registry.submit_many(
        [_request(), _request()], on_terminal=_raising_listener
    )
    try:
        registry.shutdown()
    finally:
        release.set()

    assert [registry.get(job_id).state for job_id in queued_ids] == ["cancelled", "cancelled"]


def test_cancel_returns_the_cancelled_status_when_the_listener_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: threading.Event = threading.Event()
    release: threading.Event = threading.Event()
    _patch_solve(monkeypatch, _blocking_solve_until(release, started))
    registry: JobRegistry = JobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(model="solve satisfy;")  # occupies the only worker
        assert started.wait(timeout=3)
        (queued_id,) = registry.submit_many([_request()], on_terminal=_raising_listener)
        status: SolveJobStatus = registry.cancel(queued_id)
    finally:
        release.set()
        registry.shutdown()

    assert status.state == "cancelled"


def test_listener_exception_on_the_worker_thread_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="openconstraint_mcp.jobs.registry")
    _patch_solve(monkeypatch, lambda model, *, on_start, **kw: _solve_result())
    registry: JobRegistry = JobRegistry()
    try:
        (job_id,) = registry.submit_many([_request()], on_terminal=_raising_listener)
        _wait_until_terminal(registry, job_id)
    finally:
        registry.shutdown()  # joins the worker, which notifies after finalizing

    logged: list[str] = [
        str(record.exc_info[1])
        for record in caplog.records
        if record.name == "openconstraint_mcp.jobs.registry" and record.exc_info
    ]
    assert logged == ["listener exploded"]


# --- differences this backend keeps from the shared lifecycle ------------------


def test_admission_leaves_a_job_queued_even_with_a_free_running_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MiniZinc admission: a job is `queued` and untimed until a worker takes it,
    however free the pool is (the CP-SAT registry reports `running` at once).

    The pool is stubbed out so no worker can run, leaving admission as the only
    thing that could have set the state.
    """
    registry: JobRegistry = JobRegistry()
    monkeypatch.setattr(registry._executor, "submit", lambda fn, *args: Future())
    try:
        status: SolveJobStatus = registry.get(registry.submit(model="solve satisfy;"))

        assert (status.state, status.started_at_ms) == ("queued", None)
    finally:
        registry.shutdown()


def test_get_unknown_job_id_raises_unknown_job_error() -> None:
    # Its own ValueError subtype: a portfolio skipping evicted attempts catches it
    # without also swallowing pydantic's ValidationError.
    registry: JobRegistry = JobRegistry()
    try:
        with pytest.raises(UnknownJobError):
            registry.get("does-not-exist")
    finally:
        registry.shutdown()
