"""In-process registry for background (async) MiniZinc solve jobs.

The single deliberate, bounded exception to the repo's "no global mutable state"
rule (AGENTS.md): one ``JobRegistry`` instance is created per server in
``create_mcp_server`` and owned by that server's lifecycle — never a module-level
singleton. It lets a client submit a solve as a background job, poll its status,
fetch the final ``SolveResult``, and cancel a running job, so hard solves no longer
hit synchronous MCP client timeouts.

Bounding (D1.3 / D1.5): at most ``max_running_jobs`` solves run concurrently (a
fixed ``ThreadPoolExecutor`` pool); further submissions sit ``queued`` until a
worker frees up, up to ``max_queued_jobs``; a submit beyond that is rejected with
``JobRejectedError`` rather than growing unbounded. Retained terminal jobs are
capped at ``max_retained_terminal`` (oldest evicted), and ``shutdown`` terminates
any still-running child process tree.

Layering: this is a server-layer module — it imports the ``minizinc`` solve
machinery and ``schemas``; it never imports ``server``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from subprocess import Popen
from uuid import uuid4

# jobs reuses core's solve helpers (validation, arg-building, process teardown)
# rather than re-implementing them.
from ..minizinc.core import (
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    prepare_solve_args,
    run_prepared_solve,
    validate_model_and_timeout,
)

# RESULT_BEARING_STATES/TERMINAL_STATES are imported, not re-declared: they are
# the load-bearing D1.9 invariant ("result present iff state in this set") and
# schemas.job_state owns it, so _finalize and the SolveJobStatus validator can
# never drift apart.
from ..schemas.diagnostics import Diagnostic, wrapper_job_diagnostic
from ..schemas.job_state import RESULT_BEARING_STATES, TERMINAL_STATES, JobState
from ..schemas.minizinc import (
    DEFAULT_SOLVE_CONTROLS,
    SolveControls,
    SolveJobStatus,
    SolveResult,
    job_state_for_result,
)
from ..shared.job_errors import JobRejectedError, UnknownJobError, exception_summary, now_ms
from ..shared.proc import terminate_process_tree as _terminate_process_tree

_logger: logging.Logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SolveRequest:
    """The immutable, prepared solve parameters for one job.

    ``extra_args`` is the solve argv the submitter already built from ``controls``
    (``prepare_solve_args`` for a single job; ``build_solve_extra_args`` per attempt
    after a portfolio's plan-level capability check). The worker runs it verbatim
    and never rebuilds or re-resolves.
    """

    model: str
    solver: str
    data: str | None
    checker: str | None
    timeout_ms: int
    controls: SolveControls
    extra_args: tuple[str, ...]


@dataclass
class _JobRecord:
    """Mutable per-job state, guarded by the registry lock."""

    job_id: str
    request: SolveRequest
    submitted_at_ms: int
    state: JobState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: SolveResult | None = None
    message: str | None = None
    handle: Popen[str] | None = None
    future: Future[None] | None = None
    cancel_requested: bool = False
    # Set by submit_many: called once, outside the lock, after the record finalizes.
    on_terminal: Callable[[int, SolveJobStatus], None] | None = None
    on_terminal_error: Callable[[int, str], None] | None = None
    batch_index: int = 0


class JobRegistry:
    """A bounded, single-owned registry of background solve jobs.

    All record mutation happens under ``_lock``; the critical sections are kept
    trivial. A worker writes only its own record. The bounded pool caps concurrent
    running solves (and thus live MiniZinc subprocesses); a single ``in_flight``
    counter (running + queued) enforces the admission bound of ``max_running +
    max_queued``. Admitted jobs stay queued until a worker starts them, including
    while a finishing worker is still notifying its terminal listener.
    """

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
        self._records: dict[str, _JobRecord] = {}
        self._terminal_order: list[str] = []
        # Handles of evicted terminal records whose leader was never reaped
        # (returncode None). Eviction drops the record — the only reference to
        # that live child — so the handle is stashed here for shutdown to sweep.
        self._unreaped_orphans: list[Popen[str]] = []
        self._in_flight = 0
        # Set by shutdown(); admission rejects once closed.
        self._closed: bool = False
        self._executor = ThreadPoolExecutor(
            max_workers=max_running_jobs, thread_name_prefix="solve-job"
        )

    def submit(
        self,
        *,
        model: str,
        solver: str = DEFAULT_SOLVER,
        data: str | None = None,
        checker: str | None = None,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        controls: SolveControls = DEFAULT_SOLVE_CONTROLS,
    ) -> str:
        """Admit a solve as a background job; return its server-generated ``job_id``.

        Validates the model/timeout and solver/search controls up front with the
        exact ``solve_model`` rules (so a bad ``num_solutions``/``parallel`` fails
        fast as a ``ValueError`` before any job exists), then applies D1.3 admission
        under the lock. Returns immediately (state ``queued`` or ``running``)
        without awaiting the solve; raises ``JobRejectedError`` when the bounded
        queue is full or shutdown has begun — no worker or subprocess is created then.
        """
        validate_model_and_timeout(model, timeout_ms)
        # Validates the controls and rejects an unsupported -a/-f/-p/-r control at
        # admission (one --solvers-json at most, only when a gated control is set);
        # the worker runs these prepared args and never re-resolves (D1/D2).
        request = SolveRequest(
            model=model,
            solver=solver,
            data=data,
            checker=checker,
            timeout_ms=timeout_ms,
            controls=controls,
            extra_args=prepare_solve_args(solver, controls),
        )
        with self._lock:
            self._require_open()
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message())
            return self._admit_locked(request)

    def submit_many(
        self,
        requests: Sequence[SolveRequest],
        *,
        on_terminal: Callable[[int, SolveJobStatus], None] | None = None,
        on_terminal_error: Callable[[int, str], None] | None = None,
    ) -> list[str]:
        """Admit a batch of solves atomically — all or none (D8) — in request order.

        Validates every request's model/timeout up front with the exact
        ``solve_model`` rules, then under a SINGLE lock acquisition either admits
        the whole batch (so a concurrent ``submit`` cannot take a slot
        mid-sequence) or, when the batch would exceed the bounded running+queued
        capacity, admits NONE and raises ``JobRejectedError`` — never a partial
        batch. Requests arrive prepared: control validation and capability
        (`-a/-f/-p/-r`) enforcement are the caller's job — a portfolio checks the
        whole plan once and builds each attempt's ``extra_args`` before calling
        this, so this primitive runs no ``--solvers-json`` itself. Returns the
        ``job_id`` list in request order.

        ``on_terminal``, when given, is called once on normal completion with
        ``(position in requests, terminal status)`` after that job finalizes — on the
        finishing worker's thread, or synchronously on the thread whose ``cancel``/
        ``cancel_if_queued``/``shutdown`` finalized a job that never started. It never runs
        while the registry lock is held, so it may call back into the registry; an event can
        arrive before this method returns. A listener exception is logged and does not
        propagate, so it cannot abort ``cancel``/``shutdown`` or vanish on a worker.
        ``on_terminal_error`` receives the index and error summary if completion or
        the terminal listener fails. Both callbacks run outside the registry lock;
        the error callback lets an owning portfolio fail instead of waiting forever.
        """
        for request in requests:
            validate_model_and_timeout(request.model, request.timeout_ms)
        with self._lock:
            self._require_open()
            if self._in_flight + len(requests) > self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message(batch=len(requests)))
            job_ids: list[str] = []
            try:
                for index, request in enumerate(requests):
                    job_ids.append(
                        self._admit_locked(
                            request,
                            on_terminal=on_terminal,
                            on_terminal_error=on_terminal_error,
                            batch_index=index,
                        )
                    )
            except BaseException:
                # A worker thread that cannot start fails the batch partway. None of it has
                # run yet (each worker waits for this lock), so un-admit the jobs already
                # admitted; a worker that then finds no record returns without solving.
                for job_id in job_ids:
                    del self._records[job_id]
                    self._in_flight -= 1
                raise
            return job_ids

    def get(self, job_id: str) -> SolveJobStatus:
        with self._lock:
            return self._to_status(self._require_record(job_id))

    def list(self) -> list[SolveJobStatus]:
        with self._lock:
            return [self._to_status(record) for record in self._records.values()]

    def cancel(self, job_id: str) -> SolveJobStatus:
        """Cancel a job: drop it if still queued, else terminate its process tree.

        A no-op on an already-terminal job. Cancellation before the worker starts
        is handled by ``Future.cancel``; for a running job the live handle's process
        tree is terminated and the worker records the ``cancelled`` state. If the
        handle is not yet recorded (a cancel that races process startup), the
        ``on_start`` hook terminates as soon as it captures the handle.
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
            status: SolveJobStatus | None = self._complete(
                record, "cancelled", None, "Cancelled before start"
            )
            if status is not None:
                return status
            with self._lock:
                return self._to_status(record)
        if handle is not None:
            _terminate_process_tree(handle)
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
            record: _JobRecord = self._require_record(job_id)
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
            # snapshot below; with the flag set, its own on_start terminates the
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
            _terminate_process_tree(handle)
        self._executor.shutdown(wait=True, cancel_futures=True)

    # --- internals (assume the caller holds the lock unless noted) -------------

    def _require_open(self) -> None:
        # Caller holds the lock. The single admission gate for shutdown.
        if self._closed:
            raise JobRejectedError("Job registry is shutting down; no new jobs are accepted.")

    def _queue_full_message(self, *, batch: int | None = None) -> str:
        capacity = f"{self._max_running} running + {self._max_queued} queued"
        if batch is None:
            return f"Job queue is full ({capacity}). Retry once a running job finishes."
        return (
            f"Batch of {batch} job(s) exceeds the bounded capacity ({capacity}) given "
            f"{self._in_flight} already in flight. Retry once running jobs finish."
        )

    def _admit_locked(
        self,
        request: SolveRequest,
        *,
        on_terminal: Callable[[int, SolveJobStatus], None] | None = None,
        on_terminal_error: Callable[[int, str], None] | None = None,
        batch_index: int = 0,
    ) -> str:
        # Caller holds the lock AND has already checked capacity. Launches the worker
        # future, then records the job and bumps in_flight — the single admission
        # primitive shared by submit (one) and submit_many (a batch under one lock).
        job_id = uuid4().hex
        now = now_ms()
        record = _JobRecord(
            job_id=job_id,
            request=request,
            submitted_at_ms=now,
            state="queued",
            on_terminal=on_terminal,
            on_terminal_error=on_terminal_error,
            batch_index=batch_index,
        )
        # Submit first: the worker waits for the caller's lock, so a submit that raises
        # after queueing the item (a worker thread that cannot start) leaves no record
        # for that item to run and no in-flight slot to leak.
        record.future = self._executor.submit(self._run_job, job_id)
        self._records[job_id] = record
        self._in_flight += 1
        return job_id

    def _require_record(self, job_id: str) -> _JobRecord:
        record = self._records.get(job_id)
        if record is None:
            raise UnknownJobError(f"unknown job_id: {job_id}")
        return record

    @staticmethod
    def _to_status(record: _JobRecord) -> SolveJobStatus:
        # `result` is stored only on result-bearing terminal states, so passing it
        # straight through already satisfies the SolveJobStatus invariant.
        # `elapsed_ms` is frozen at finalize for terminal jobs; for a started-but-
        # running job it is derived from `started_at_ms` on each read so it advances
        # (a `running` job reports `state` + `elapsed_ms` — README / SolveJobStatus).
        if record.state in TERMINAL_STATES:
            elapsed_ms = record.elapsed_ms
        elif record.started_at_ms is not None:
            elapsed_ms = max(now_ms() - record.started_at_ms, 0)
        else:
            elapsed_ms = None
        return SolveJobStatus(
            job_id=record.job_id,
            state=record.state,
            solver=record.request.solver,
            timeout_ms=record.request.timeout_ms,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=elapsed_ms,
            result=record.result,
            message=record.message,
            diagnostic=JobRegistry._job_diagnostic(record),
        )

    @staticmethod
    def _job_diagnostic(record: _JobRecord) -> Diagnostic | None:
        # Non-result-bearing terminal states (failed/cancelled) get a wrapper
        # diagnostic; result-bearing states (succeeded/timeout) derive theirs
        # from the embedded result, so a result-carried timeout_with_incumbent
        # wins over a generic wrapper timeout (D3 invariant).
        wrapper = wrapper_job_diagnostic(
            record.state,
            message=record.message or f"job {record.state}",
            details={"job_id": record.job_id, "state": record.state},
        )
        if wrapper is not None:
            return wrapper
        return record.result.diagnostic if record.result is not None else None

    def _finalize(
        self,
        record: _JobRecord,
        state: JobState,
        result: SolveResult | None,
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
        record.result = result if state in RESULT_BEARING_STATES else None
        record.message = message
        self._in_flight -= 1
        self._terminal_order.append(record.job_id)
        self._evict_terminal_overflow()
        return True

    def _complete(
        self,
        record: _JobRecord,
        state: JobState,
        result: SolveResult | None,
        message: str | None,
    ) -> SolveJobStatus | None:
        # Shared by workers, queued cancellation, and shutdown. In particular,
        # eviction or status construction can fail AFTER _finalize changed state.
        # Report that failure independently of constructing a SolveJobStatus.
        error: str | None = None
        status: SolveJobStatus | None = None
        with self._lock:
            try:
                # A requested cancel wins however the killed solve ended, even by raising;
                # a job already finalizing as cancelled keeps its own message.
                if record.cancel_requested and state != "cancelled":
                    state, result, message = "cancelled", None, "Cancelled by client"
                if not self._finalize(record, state, result, message):
                    return None
                status = self._to_status(record)
            except Exception as exc:  # noqa: BLE001 - completion boundary
                error = exception_summary(exc)
                _logger.exception("completion failed for job %s", record.job_id)
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

    def _notify_terminal(self, record: _JobRecord, status: SolveJobStatus) -> None:
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
            _logger.exception("terminal listener failed for job %s", record.job_id)
            self._notify_terminal_error(record, exception_summary(exc))

    @staticmethod
    def _notify_terminal_error(record: _JobRecord, message: str) -> None:
        # Caller must NOT hold the registry lock: the owner may cancel other jobs.
        if record.on_terminal_error is None:
            return
        try:
            record.on_terminal_error(record.batch_index, message)
        except Exception:
            _logger.exception("terminal error listener failed for job %s", record.job_id)

    def _evict_terminal_overflow(self) -> None:
        # Caller holds the lock. FIFO eviction of the oldest terminal jobs beyond
        # the retention cap, so a long-lived server cannot grow unbounded (D1.5).
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
            _terminate_process_tree(proc)

    def _run_job(self, job_id: str) -> None:
        # The worker callable: mark running, solve, then record the terminal state.
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return  # un-admitted by a failed batch, or evicted, before it could start
            request = record.request
            record.state = "running"
            if record.started_at_ms is None:
                record.started_at_ms = now_ms()
        try:
            result = run_prepared_solve(
                request.model,
                solver=request.solver,
                data=request.data,
                checker=request.checker,
                timeout_ms=request.timeout_ms,
                extra_args=request.extra_args,
                on_start=lambda proc: self._on_start(job_id, proc),
            )
        except Exception as exc:  # noqa: BLE001 - worker boundary: never leak; record as failed
            self._complete(record, "failed", None, exception_summary(exc))
            return
        self._complete(record, job_state_for_result(result), result, None)
