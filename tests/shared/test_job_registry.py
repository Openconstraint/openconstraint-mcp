"""Unit tests for the shared background-job lifecycle.

Everything here runs against ``_FakeRegistry``, a minimal concrete backend whose
worker is a caller-supplied callable driven by ``threading.Event``s — no solver,
no child process. The MiniZinc and CP-SAT registries own only the differences
(their request/status types, admission timing, and worker outcome policy), which
their own suites cover.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

import pytest
from pydantic import BaseModel

from openconstraint_mcp.schemas.job_state import JobState
from openconstraint_mcp.shared.job_errors import JobRejectedError, exception_summary, now_ms
from openconstraint_mcp.shared.job_registry import BackgroundJobRegistry, JobRecord

_LOGGER_NAME = "tests.shared.job_registry"


class _FakeStatus(BaseModel):
    """The fake backend's status snapshot — the shape, not the field set, matters."""

    job_id: str
    state: JobState
    result: str | None = None
    message: str | None = None
    elapsed_ms: int | None = None


_Runner = Callable[[Any, str], str]
_FakeRecord = JobRecord[_Runner, str, _FakeStatus]


class _FakeRegistry(BackgroundJobRegistry[str, _FakeStatus, _FakeRecord]):
    """A backend whose "solve" is the callable the submitter passed in."""

    _queue_label = "Fake job"
    _thread_name_prefix = "fake-job"
    _logger = logging.getLogger(_LOGGER_NAME)

    def submit(
        self,
        runner: _Runner,
        *,
        on_terminal: Callable[[int, _FakeStatus], None] | None = None,
        on_terminal_error: Callable[[int, str], None] | None = None,
    ) -> str:
        with self._lock:
            self._admission_gate_locked()
            job_id: str = uuid4().hex
            record = _FakeRecord(
                job_id=job_id,
                request=runner,
                submitted_at_ms=now_ms(),
                state="queued",
                on_terminal=on_terminal,
                on_terminal_error=on_terminal_error,
            )
            record.future = self._executor.submit(self._run_job, job_id)
            self._publish_locked(record)
            return job_id

    def _to_status(self, record: _FakeRecord) -> _FakeStatus:
        return _FakeStatus(
            job_id=record.job_id,
            state=record.state,
            result=record.result,
            message=record.message,
            elapsed_ms=self._elapsed_ms(record),
        )

    def _run_job(self, job_id: str) -> None:
        record = self._begin_job(job_id)
        if record is None:
            return
        try:
            result: str = record.request(self, job_id)
        except Exception as exc:  # noqa: BLE001 - worker boundary
            self._complete(record, "failed", None, exception_summary(exc))
            return
        if record.cancel_requested:
            self._complete(record, "cancelled", None, "Cancelled by client")
            return
        self._complete(record, "succeeded", result, None)


class _FakeProc:
    """A handle stand-in: no pid, no real process, so termination must be patched.

    Defaults to looking unreaped (``returncode = None``); set ``returncode = 0``
    to model a child the registry has already reaped.
    """

    returncode: Any = None

    def poll(self) -> Any:
        return self.returncode


@pytest.fixture(autouse=True)
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.shared.job_registry.terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _patch_terminate(monkeypatch: pytest.MonkeyPatch, recorder: list[Any]) -> None:
    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        recorder.append(proc)

    monkeypatch.setattr(
        "openconstraint_mcp.shared.job_registry.terminate_process_tree", _fake_terminate
    )


def _instant(registry: _FakeRegistry, job_id: str) -> str:
    return "done"


def _blocking(release: threading.Event, started: threading.Event, proc: Any = None) -> _Runner:
    def _runner(registry: _FakeRegistry, job_id: str) -> str:
        registry._on_start(job_id, proc if proc is not None else _FakeProc())
        started.set()
        release.wait(timeout=5)
        return "done"

    return _runner


def _launching(handle: Any) -> _Runner:
    def _runner(registry: _FakeRegistry, job_id: str) -> str:
        registry._on_start(job_id, handle)
        return "done"

    return _runner


def _wait_until_terminal(registry: _FakeRegistry, job_id: str, timeout: float = 3.0) -> JobState:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


# --- admission ---------------------------------------------------------------


def test_submitted_job_runs_and_reaches_succeeded_with_its_result() -> None:
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_instant)
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        assert registry.get(job_id).result == "done"
    finally:
        registry.shutdown()


def test_submit_beyond_the_running_capacity_queues() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=2)
    try:
        registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        assert registry.get(registry.submit(_instant)).state == "queued"
    finally:
        release.set()
        registry.shutdown()


def test_submit_beyond_the_bound_is_rejected_with_the_registry_label() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=0)
    try:
        registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        with pytest.raises(JobRejectedError) as excinfo:
            registry.submit(_instant)
        assert str(excinfo.value) == (
            "Fake job queue is full (1 running + 0 queued). Retry once a running job finishes."
        )
    finally:
        release.set()
        registry.shutdown()


def test_submit_after_shutdown_is_rejected_with_the_registry_label() -> None:
    registry = _FakeRegistry()
    registry.shutdown()

    with pytest.raises(JobRejectedError) as excinfo:
        registry.submit(_instant)
    assert str(excinfo.value) == ("Fake job registry is shutting down; no new jobs are accepted.")


def test_get_of_an_unknown_job_id_raises_value_error() -> None:
    registry = _FakeRegistry()
    try:
        with pytest.raises(ValueError, match="unknown job_id: nope"):
            registry.get("nope")
    finally:
        registry.shutdown()


def test_list_returns_one_status_per_retained_job() -> None:
    registry = _FakeRegistry(max_running_jobs=2)
    try:
        ids = {registry.submit(_instant) for _ in range(3)}
        for job_id in ids:
            _wait_until_terminal(registry, job_id)
        assert {status.job_id for status in registry.list()} == ids
    finally:
        registry.shutdown()


# --- cancel ------------------------------------------------------------------


def test_cancel_of_a_queued_job_finalizes_it_cancelled() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        queued_id = registry.submit(_instant)

        assert registry.cancel(queued_id).state == "cancelled"
    finally:
        release.set()
        registry.shutdown()


def test_cancel_of_a_running_job_terminates_its_handle(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    handle = _FakeProc()
    terminated: list[Any] = []

    def _terminate_and_release(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the "process" dying unblocks the run

    monkeypatch.setattr(
        "openconstraint_mcp.shared.job_registry.terminate_process_tree", _terminate_and_release
    )
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_blocking(release, started, handle))
        assert started.wait(timeout=3)

        registry.cancel(job_id)

        assert terminated == [handle]
    finally:
        release.set()
        registry.shutdown()


def test_cancel_of_a_terminal_job_is_a_no_op(monkeypatch: pytest.MonkeyPatch) -> None:
    terminated: list[Any] = []
    _patch_terminate(monkeypatch, terminated)
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_instant)
        assert _wait_until_terminal(registry, job_id) == "succeeded"

        assert registry.cancel(job_id).state == "succeeded"
        assert terminated == []
    finally:
        registry.shutdown()


def test_cancel_if_queued_drops_a_job_no_worker_has_started() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        queued_id = registry.submit(_instant)

        assert (registry.cancel_if_queued(queued_id), registry.get(queued_id).state) == (
            True,
            "cancelled",
        )
    finally:
        release.set()
        registry.shutdown()


def test_cancel_if_queued_leaves_a_started_job_running() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)

        assert (registry.cancel_if_queued(job_id), registry.get(job_id).state) == (False, "running")
    finally:
        release.set()
        registry.shutdown()


def test_cancel_arriving_before_the_handle_exists_terminates_it_at_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The launch window: the worker is running but has not recorded its handle yet,
    # so cancel finds none. _on_start must terminate the instant it captures one.
    running, cancelled = threading.Event(), threading.Event()
    handle = _FakeProc()
    terminated: list[Any] = []
    _patch_terminate(monkeypatch, terminated)

    def _late_launch(registry: _FakeRegistry, job_id: str) -> str:
        running.set()
        assert cancelled.wait(timeout=3)
        registry._on_start(job_id, handle)
        return "done"

    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_late_launch)
        assert running.wait(timeout=3)
        registry.cancel(job_id)
        cancelled.set()
        _wait_until_terminal(registry, job_id)

        assert terminated == [handle]
    finally:
        cancelled.set()
        registry.shutdown()


# --- worker outcomes ---------------------------------------------------------


def test_worker_exception_reaches_failed_with_the_exception_summary() -> None:
    def _boom(registry: _FakeRegistry, job_id: str) -> str:
        raise RuntimeError("worker exploded")

    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_boom)
        assert _wait_until_terminal(registry, job_id) == "failed"
        status = registry.get(job_id)
        assert (status.result, status.message) == (None, "RuntimeError: worker exploded")
    finally:
        registry.shutdown()


def test_cancel_observed_after_the_run_finalizes_cancelled_without_a_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started, release = threading.Event(), threading.Event()
    _patch_terminate(monkeypatch, [])
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        registry.cancel(job_id)
        release.set()

        assert _wait_until_terminal(registry, job_id) == "cancelled"
        assert registry.get(job_id).result is None
    finally:
        release.set()
        registry.shutdown()


def test_a_running_job_reports_an_advancing_elapsed_ms() -> None:
    started, release = threading.Event(), threading.Event()
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        first = registry.get(job_id).elapsed_ms
        time.sleep(0.03)
        second = registry.get(job_id).elapsed_ms

        assert first is not None and second is not None and second > first
    finally:
        release.set()
        registry.shutdown()


# --- retention and eviction --------------------------------------------------


def test_retention_cap_evicts_the_oldest_terminal_job() -> None:
    # max_running_jobs=1 forces sequential completion, so "oldest terminal" is
    # deterministic (submission order == completion order).
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=2)
    try:
        first = registry.submit(_instant)
        _wait_until_terminal(registry, first)
        second = registry.submit(_instant)
        _wait_until_terminal(registry, second)
        third = registry.submit(_instant)
        _wait_until_terminal(registry, third)

        with pytest.raises(ValueError, match="unknown"):
            registry.get(first)
        assert {status.job_id for status in registry.list()} == {second, third}
    finally:
        registry.shutdown()


def test_shutdown_terminates_an_evicted_unreaped_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # Evicting a terminal record that still owns an unreaped leader must not drop
    # the only reference to that child: the handle is kept for the shutdown sweep.
    unreaped = _FakeProc()
    unreaped.returncode = None  # type: ignore[attr-defined]
    reaped = _FakeProc()
    reaped.returncode = 0  # type: ignore[attr-defined]
    terminated: list[Any] = []
    _patch_terminate(monkeypatch, terminated)
    # max_running=1 forces sequential completion so "oldest terminal" is the first
    # job; cap=1 evicts it the moment the second finalizes.
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=1)
    try:
        first = registry.submit(_launching(unreaped))
        assert _wait_until_terminal(registry, first) == "succeeded"
        second = registry.submit(_launching(reaped))
        assert _wait_until_terminal(registry, second) == "succeeded"
        with pytest.raises(ValueError, match="unknown"):
            registry.get(first)  # evicted
    finally:
        registry.shutdown()

    # first's unreaped handle was kept and swept; second's reaped handle was not.
    assert terminated == [unreaped]


def test_shutdown_retries_a_terminal_unreaped_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # A job can finalize (terminal) while its leader survived termination and was
    # never reaped (returncode None). shutdown must still terminate that handle,
    # not skip it just because the record is terminal.
    handle = _FakeProc()
    handle.returncode = None  # type: ignore[attr-defined]
    terminated: list[Any] = []
    _patch_terminate(monkeypatch, terminated)
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_launching(handle))
        assert _wait_until_terminal(registry, job_id) == "succeeded"
    finally:
        registry.shutdown()

    assert terminated == [handle]


def test_eviction_reaps_a_leader_that_exited_before_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A leader can exit on its own between finalize and eviction (eviction may run
    # long after the job that owns it finished, once the retention cap is next
    # exceeded). Eviction must poll() to reap it there rather than trusting the
    # stale post-finalize returncode and stashing an already-dead handle.
    class _LateExitProc(_FakeProc):
        def __init__(self) -> None:
            self.returncode = None

        def poll(self) -> Any:
            self.returncode = 0  # exits the moment eviction checks it
            return self.returncode

    late_exit = _LateExitProc()
    reaped = _FakeProc()
    reaped.returncode = 0  # type: ignore[attr-defined]
    terminated: list[Any] = []
    _patch_terminate(monkeypatch, terminated)
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=1)
    try:
        first = registry.submit(_launching(late_exit))
        assert _wait_until_terminal(registry, first) == "succeeded"
        second = registry.submit(_launching(reaped))
        assert _wait_until_terminal(registry, second) == "succeeded"
    finally:
        registry.shutdown()

    # Reaped at eviction time, so it was never stashed for the shutdown sweep.
    assert terminated == []


# --- shutdown ----------------------------------------------------------------


def test_shutdown_finalizes_a_queued_job_as_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    monkeypatch.setattr(
        "openconstraint_mcp.shared.job_registry.terminate_process_tree",
        lambda proc, **kwargs: release.set(),
    )
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=4)
    registry.submit(_blocking(release, started))
    assert started.wait(timeout=3)
    queued_id = registry.submit(_instant)

    registry.shutdown()

    assert registry.get(queued_id).state == "cancelled"


def test_shutdown_terminates_a_running_child(monkeypatch: pytest.MonkeyPatch) -> None:
    started, release = threading.Event(), threading.Event()
    handle = _FakeProc()
    terminated: list[Any] = []

    def _terminate_and_release(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # let the blocked worker unwind so shutdown can join it

    monkeypatch.setattr(
        "openconstraint_mcp.shared.job_registry.terminate_process_tree", _terminate_and_release
    )
    registry = _FakeRegistry()
    registry.submit(_blocking(release, started, handle))
    assert started.wait(timeout=3)

    registry.shutdown()

    assert terminated == [handle]


# --- terminal listeners ------------------------------------------------------


_Events = list[tuple[int, _FakeStatus]]


def _recording_listener() -> tuple[_Events, Callable[[int, _FakeStatus], None]]:
    events: _Events = []

    def _listener(index: int, status: _FakeStatus) -> None:
        events.append((index, status))

    return events, _listener


def _wait_for_events(events: _Events, count: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(events) >= count:
            return
        time.sleep(0.005)
    raise AssertionError(f"expected {count} listener event(s) within {timeout}s, got {len(events)}")


def test_terminal_listener_fires_once_for_a_succeeded_job() -> None:
    events, listener = _recording_listener()
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(_instant, on_terminal=listener)
        _wait_for_events(events, 1)
    finally:
        registry.shutdown()

    assert [(index, status.job_id, status.state) for index, status in events] == [
        (0, job_id, "succeeded")
    ]


def test_terminal_listener_fires_once_for_a_job_cancelled_while_queued() -> None:
    started, release = threading.Event(), threading.Event()
    events, listener = _recording_listener()
    registry = _FakeRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        registry.submit(_blocking(release, started))
        assert started.wait(timeout=3)
        queued_id = registry.submit(_instant, on_terminal=listener)
        registry.cancel(queued_id)
    finally:
        release.set()
        registry.shutdown()

    assert [(index, status.job_id, status.state) for index, status in events] == [
        (0, queued_id, "cancelled")
    ]


def test_terminal_listener_never_runs_while_the_registry_lock_is_held() -> None:
    # Re-entry would deadlock if completion invoked the listener under the lock.
    done = threading.Event()
    registry = _FakeRegistry()

    def _reentrant(index: int, status: _FakeStatus) -> None:
        registry.get(status.job_id)
        registry.cancel(status.job_id)
        done.set()

    registry.submit(_instant, on_terminal=_reentrant)
    completed = done.wait(timeout=3)
    if completed:  # a deadlocked worker would hang the pool join below
        registry.shutdown()
    assert completed, "listener deadlocked calling back into the registry"


def test_status_construction_failure_notifies_the_error_listener(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    errors: list[tuple[int, str]] = []
    registry = _FakeRegistry()

    def _broken(record: _FakeRecord) -> _FakeStatus:
        raise RuntimeError("snapshot exploded")

    def _on_error(index: int, message: str) -> None:
        # Re-entry would deadlock if completion invoked this under registry._lock.
        with registry._lock:
            errors.append((index, message))

    monkeypatch.setattr(registry, "_to_status", _broken)
    try:
        job_id = registry.submit(_instant, on_terminal_error=_on_error)
        future = registry._records[job_id].future
        assert future is not None
        future.result(timeout=3)
    finally:
        registry.shutdown()

    assert errors == [(0, "RuntimeError: snapshot exploded")]


def test_raising_terminal_listener_notifies_the_error_listener() -> None:
    errors: list[tuple[int, str]] = []

    def _raising(index: int, status: _FakeStatus) -> None:
        raise RuntimeError("listener exploded")

    registry = _FakeRegistry()
    try:
        job_id = registry.submit(
            _instant, on_terminal=_raising, on_terminal_error=lambda i, m: errors.append((i, m))
        )
        _wait_until_terminal(registry, job_id)
    finally:
        registry.shutdown()  # joins the worker, which notifies after finalizing

    assert errors == [(0, "RuntimeError: listener exploded")]


def test_raising_error_listener_is_logged_rather_than_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger=_LOGGER_NAME)

    def _raising_error_listener(index: int, message: str) -> None:
        raise RuntimeError("error listener exploded")

    def _raising(index: int, status: _FakeStatus) -> None:
        raise RuntimeError("listener exploded")

    registry = _FakeRegistry()
    try:
        job_id = registry.submit(
            _instant, on_terminal=_raising, on_terminal_error=_raising_error_listener
        )
        _wait_until_terminal(registry, job_id)
    finally:
        registry.shutdown()

    assert "error listener exploded" in [
        str(record.exc_info[1])
        for record in caplog.records
        if record.name == _LOGGER_NAME and record.exc_info
    ]


def test_completion_releases_both_callback_references() -> None:
    # Both callbacks can capture an owning portfolio; a retained terminal record
    # must not keep them alive.
    registry = _FakeRegistry()
    try:
        job_id = registry.submit(
            _instant, on_terminal=lambda i, s: None, on_terminal_error=lambda i, m: None
        )
        _wait_until_terminal(registry, job_id)
    finally:
        registry.shutdown()

    record = registry._records[job_id]
    assert (record.on_terminal, record.on_terminal_error) == (None, None)
