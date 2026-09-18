"""The bounded background-job lifecycle, owned once and shared by both registries.

``jobs.registry.JobRegistry`` (MiniZinc solves) and ``pyexec.jobs.CpsatJobRegistry``
(CP-SAT Python) admit work into a fixed ``ThreadPoolExecutor``, poll it, cancel it,
retain it FIFO, and tear it down at shutdown. That lifecycle lives here, so a fix
to it lands once. Each backend keeps only what genuinely differs: its request,
record and status types, its submission validation, when a job starts timing, and
its worker's outcome policy.

Bounding: at most ``max_running_jobs`` jobs run concurrently; further submissions
sit ``queued`` until a worker frees up, up to ``max_queued_jobs``; a submit beyond
that is rejected with ``JobRejectedError`` rather than growing unbounded. Retained
terminal jobs are capped at ``max_retained_terminal`` (oldest evicted).

Layering: a dependency-light ``shared`` leaf — it imports ``shared.proc``,
``shared.job_errors`` and ``schemas.job_state`` only, so the ``pyexec`` subtree can
use it without pulling in ``minizinc`` or ``runtime``.
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from subprocess import Popen
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict

# RESULT_BEARING_STATES/TERMINAL_STATES are imported, not re-declared: they are
# the load-bearing result-presence invariant ("result present iff state in this set") and
# schemas.job_state owns it, so _finalize and the status validators can never
# drift apart.
from ..schemas.job_state import RESULT_BEARING_STATES, TERMINAL_STATES, JobState
from .job_errors import JobRejectedError, exception_summary, now_ms
from .proc import terminate_process_tree


class JobRecord[RequestT, ResultT, StatusT](BaseModel):
    """Mutable per-job state, guarded by the owning registry's lock.

    Parameterized over the backend's request, result and status types so a
    concrete record keeps them instead of widening to ``Any``. ``handle`` and
    ``future`` are runtime objects, hence ``arbitrary_types_allowed``.
    ``on_terminal``/``on_terminal_error`` are the batch listeners: set by a batch
    submit, called once, outside the lock, after the record finalizes.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, strict=True)

    job_id: str
    request: RequestT
    submitted_at_ms: int
    state: JobState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: ResultT | None = None
    message: str | None = None
    handle: Popen[str] | None = None
    future: Future[None] | None = None
    cancel_requested: bool = False
    on_terminal: Callable[[int, StatusT], None] | None = None
    on_terminal_error: Callable[[int, str], None] | None = None
    batch_index: int = 0


class BackgroundJobRegistry[ResultT, StatusT, RecordT: JobRecord[Any, Any, Any]](ABC):
    """A bounded, single-owned registry of background jobs.

    All record mutation happens under ``_lock``; the critical sections are kept
    trivial and a worker writes only its own record. The bounded pool caps
    concurrent running jobs (and thus live child processes); a single
    ``in_flight`` counter (running + queued) enforces the admission bound of
    ``max_running + max_queued``.

    ``_queue_label`` opens the two rejection messages ("<label> queue is full …",
    "<label> registry is shutting down …") so no backend spells them itself, and
    ``_logger`` is the backend's own module logger, so a completion or listener
    failure is reported under the module a reader (or a caplog filter) expects.
    """

    _queue_label: ClassVar[str]
    _thread_name_prefix: ClassVar[str]
    _logger: ClassVar[logging.Logger]

    def __init__(
        self,
        *,
        max_running_jobs: int = 4,
        max_queued_jobs: int = 16,
        max_retained_terminal: int = 64,
    ) -> None:
        if max_running_jobs < 1:
            raise ValueError("max_running_jobs must be >= 1")
        if max_queued_jobs < 0:
            raise ValueError("max_queued_jobs must be >= 0")
        if max_retained_terminal < 1:
            raise ValueError("max_retained_terminal must be >= 1")
        self._max_running = max_running_jobs
        self._max_queued = max_queued_jobs
        self._max_retained_terminal = max_retained_terminal
        self._lock = threading.Lock()
        self._records: dict[str, RecordT] = {}
        self._terminal_order: list[str] = []
        # Handles of evicted terminal records whose leader was never reaped
        # (returncode None). Eviction drops the record — the only reference to
        # that live child — so the handle is stashed here for shutdown to sweep.
        self._unreaped_orphans: list[Popen[str]] = []
        self._in_flight = 0
        # Set by shutdown(); admission rejects once closed.
        self._closed: bool = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_running_jobs, thread_name_prefix=self._thread_name_prefix
        )

    # --- backend seam ----------------------------------------------------------

    @abstractmethod
    def _to_status(self, record: RecordT) -> StatusT:
        """Snapshot a record as the backend's status model. Caller holds the lock."""

    @abstractmethod
    def _run_job(self, job_id: str) -> None:
        """The worker callable: run one job and complete its record."""

    def _terminate(self, handle: Popen[str]) -> None:
        """Kill a job's child process tree.

        Routed through the backend so each one terminates via its own module
        global, which is also the seam its tests patch.
        """
        terminate_process_tree(handle)

    def _unknown_job_error(self, job_id: str) -> Exception:
        """The lookup failure for an id this registry never held, or evicted."""
        return ValueError(f"unknown job_id: {job_id}")

    # --- public surface --------------------------------------------------------

    def get(self, job_id: str) -> StatusT:
        with self._lock:
            return self._to_status(self._require_record(job_id))

    def list(self) -> list[StatusT]:
        with self._lock:
            return [self._to_status(record) for record in self._records.values()]

    def cancel(self, job_id: str) -> StatusT:
        """Cancel a job: drop it if still queued, else terminate its process tree.

        A no-op on an already-terminal job. Cancellation before the worker starts
        is handled by ``Future.cancel``; for a running job the live handle's process
        tree is terminated and the worker finalizes under its own backend outcome
        policy — MiniZinc always reaches ``cancelled``, while CP-SAT reaches it only
        when the terminated run still produced a result and reports ``failed`` when
        that run raises instead. If the handle is not yet recorded (a cancel that
        races process startup), the ``_on_start`` hook terminates as soon as it
        captures the handle.
        """
        with self._lock:
            record = self._require_record(job_id)
            if record.state in TERMINAL_STATES:
                return self._to_status(record)
            record.cancel_requested = True
            future = record.future
            handle = record.handle
        if future is not None and future.cancel():
            # Cancelled before the worker started: it will never run, so finalize here.
            # A second cancel racing this one also sees future.cancel() True, but
            # its _finalize is a no-op, so only the first notifies.
            status: StatusT | None = self._complete(
                record, "cancelled", None, "Cancelled before start"
            )
            if status is not None:
                return status
            with self._lock:
                return self._to_status(record)
        if handle is not None:
            self._terminate(handle)
        with self._lock:
            return self._to_status(record)

    def cancel_if_queued(self, job_id: str) -> bool:
        """Drop a job no worker has started yet; return whether it was dropped.

        Unlike ``cancel``, never terminates a process, so it returns promptly: a job a
        worker has already taken, or one already terminal, is left untouched for a
        real ``cancel``. A dropped job finalizes ``cancelled`` and notifies its terminal
        listener on this thread, exactly like a queued job ``cancel`` drops.
        """
        with self._lock:
            record: RecordT = self._require_record(job_id)
            future: Future[None] | None = record.future
        if future is None or not future.cancel():
            return False
        self._complete(record, "cancelled", None, "Cancelled before start")
        return True

    def shutdown(self) -> None:
        """Terminate running children and tear down the worker pool (lifespan exit).

        Cancels not-yet-started queued jobs and finalizes them as ``cancelled`` so
        no record is left non-terminal; terminates every live child process tree
        (orphan handling) — whose worker then finalizes itself — and joins the pool.
        A hard server kill bypasses this — acceptable for a local, non-persistent v1.
        """
        with self._lock:
            # Close admission in the SAME acquisition as the snapshot below, so every
            # job is either in the snapshot (and marked) or rejected — none can be
            # admitted after the snapshot and stranded by the pool teardown.
            self._closed = True
            # Mark every non-terminal record cancel_requested FIRST. A worker that
            # is running but has not yet recorded its handle (the launch window)
            # would otherwise slip past both the future.cancel() and the handle
            # snapshot below; with the flag set, its own _on_start terminates the
            # child the instant it launches, so wait=True joins promptly instead of
            # blocking on the full solve timeout.
            for record in self._records.values():
                if record.state not in TERMINAL_STATES:
                    record.cancel_requested = True
            records = list(self._records.values())
        # A pending job's future.cancel() succeeds, so its worker will never run to
        # finalize it; do that here. A running job's cancel() returns False — its
        # handle is terminated below and its worker finalizes it (joined by wait).
        for record in records:
            future = record.future
            if future is not None and future.cancel():
                self._complete(record, "cancelled", None, "Cancelled at shutdown")
        with self._lock:
            # A terminal record can still own a leader that termination could not
            # reap. Its None returncode is the teardown-retry signal.
            handles = [
                r.handle
                for r in self._records.values()
                if r.handle is not None
                and (r.state not in TERMINAL_STATES or getattr(r.handle, "returncode", 0) is None)
            ]
            # Evicted-but-unreaped children live only here now; sweep them too.
            handles.extend(self._unreaped_orphans)
            self._unreaped_orphans = []
        for handle in handles:
            self._terminate(handle)
        self._executor.shutdown(wait=True, cancel_futures=True)

    # --- admission (assume the caller holds the lock) --------------------------

    def _require_open(self) -> None:
        # The single admission gate for shutdown.
        if self._closed:
            raise JobRejectedError(
                f"{self._queue_label} registry is shutting down; no new jobs are accepted."
            )

    def _capacity_label(self) -> str:
        return f"{self._max_running} running + {self._max_queued} queued"

    def _queue_full_message(self) -> str:
        return (
            f"{self._queue_label} queue is full ({self._capacity_label()}). "
            "Retry once a running job finishes."
        )

    def _admission_gate_locked(self) -> None:
        """Reject a single-job submit that shutdown closed or the bound bars."""
        self._require_open()
        if self._in_flight >= self._max_running + self._max_queued:
            raise JobRejectedError(self._queue_full_message())

    def _publish_locked(self, record: RecordT) -> None:
        """Make an admitted job visible and take its in-flight slot."""
        self._records[record.job_id] = record
        self._in_flight += 1

    def _unpublish_locked(self, job_id: str) -> None:
        """Un-admit a job whose batch failed before any of it could run."""
        del self._records[job_id]
        self._in_flight -= 1

    def _require_record(self, job_id: str) -> RecordT:
        record = self._records.get(job_id)
        if record is None:
            raise self._unknown_job_error(job_id)
        return record

    # --- lifecycle -------------------------------------------------------------

    def _elapsed_ms(self, record: RecordT) -> int | None:
        # Frozen at finalize for a terminal job; for a started-but-running job it is
        # derived from started_at_ms on each read so it advances (a `running` job
        # reports `state` + `elapsed_ms` — see docs/mcp-tools.md).
        if record.state in TERMINAL_STATES:
            return record.elapsed_ms
        if record.started_at_ms is None:
            return None
        return max(now_ms() - record.started_at_ms, 0)

    def _finalize(
        self,
        record: RecordT,
        state: JobState,
        result: ResultT | None,
        message: str | None,
    ) -> bool:
        # Caller holds the lock. Idempotent against a late cancel: a record already
        # terminal is left untouched. Returns whether this call performed the
        # transition, so the caller notifies the terminal listener exactly once.
        if record.state in TERMINAL_STATES:
            return False
        now = now_ms()
        record.state = state
        record.finished_at_ms = now
        if record.started_at_ms is not None:
            record.elapsed_ms = max(now - record.started_at_ms, 0)
        if state in RESULT_BEARING_STATES:
            record.result = result
        else:
            record.result = None
        record.message = message
        self._in_flight -= 1
        self._terminal_order.append(record.job_id)
        self._evict_terminal_overflow()
        return True

    def _complete(
        self,
        record: RecordT,
        state: JobState,
        result: ResultT | None,
        message: str | None,
        publish_extras: Callable[[RecordT], None] | None = None,
    ) -> StatusT | None:
        # Shared by workers, queued cancellation, and shutdown. In particular,
        # eviction or status construction can fail AFTER _finalize changed state.
        # Report that failure independently of constructing a status.
        # `publish_extras` writes a backend's result-linked extras (CP-SAT's checker
        # outcome) in THIS acquisition, so no reader can see them on a still-running
        # record. It is the ONLY writer of those extras, it runs only after _finalize
        # made the record terminal, and only when that terminal state is
        # result-bearing — so a non-result-bearing finalize has nothing to clear.
        error: str | None = None
        status: StatusT | None = None
        with self._lock:
            try:
                if not self._finalize(record, state, result, message):
                    return None
                if publish_extras is not None and record.state in RESULT_BEARING_STATES:
                    publish_extras(record)
                status = self._to_status(record)
            except Exception as exc:  # noqa: BLE001 - completion boundary
                error = exception_summary(exc)
                self._logger.exception("completion failed for job %s", record.job_id)
        try:
            if error is not None:
                self._notify_terminal_error(record, error)
            elif status is not None:
                self._notify_terminal(record, status)
        finally:
            # Both callbacks can capture a portfolio and its winner output. Keep
            # them through error reporting, then release those references even
            # while this terminal attempt remains retained in the registry.
            with self._lock:
                record.on_terminal = None
                record.on_terminal_error = None
        return status

    def _notify_terminal(self, record: RecordT, status: StatusT) -> None:
        # Caller must NOT hold the lock: a listener may call back into the registry
        # (a portfolio cancels its losers), and threading.Lock is not reentrant.
        if record.on_terminal is None:
            return
        try:
            record.on_terminal(record.batch_index, status)
        except Exception as exc:
            # Logged, not raised: shutdown must still finalize the remaining queued jobs
            # and terminate live children, and on a worker thread the error would
            # otherwise vanish into a Future nobody reads.
            self._logger.exception("terminal listener failed for job %s", record.job_id)
            self._notify_terminal_error(record, exception_summary(exc))

    def _notify_terminal_error(self, record: RecordT, message: str) -> None:
        # Caller must NOT hold the registry lock: the owner may cancel other jobs.
        if record.on_terminal_error is None:
            return
        try:
            record.on_terminal_error(record.batch_index, message)
        except Exception:
            self._logger.exception("terminal error listener failed for job %s", record.job_id)

    def _evict_terminal_overflow(self) -> None:
        # Caller holds the lock. FIFO eviction of the oldest terminal jobs beyond
        # the retention cap, so a long-lived server cannot grow unbounded.
        while len(self._terminal_order) > self._max_retained_terminal:
            oldest: str = self._terminal_order.pop(0)
            evicted = self._records.pop(oldest, None)
            handle = evicted.handle if evicted is not None else None
            # poll() (not the stale .returncode) so a leader that exited on its own
            # since finalize gets reaped here instead of stashed as a false orphan.
            if handle is not None and handle.poll() is None:
                # A still-live, unreaped leader whose only reference was this record;
                # keep the handle so shutdown can still terminate it.
                self._unreaped_orphans.append(handle)

    def _on_start(self, job_id: str, proc: Popen[str]) -> None:
        # Called by the runner the instant the child is launched. Record the handle
        # and, if a cancel already arrived during startup, terminate immediately so
        # the cancel can't slip through the launch window.
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                cancel_now = True  # job evicted mid-flight; don't leave a child
            else:
                record.handle = proc
                cancel_now = record.cancel_requested
        if cancel_now:
            self._terminate(proc)

    def _begin_job(self, job_id: str) -> RecordT | None:
        # A worker's first step: mark the job running and start its clock if
        # admission did not. None means the record was un-admitted by a failed
        # batch, or evicted, before the worker could start it.
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return None
            record.state = "running"
            if record.started_at_ms is None:
                record.started_at_ms = now_ms()
            return record
