"""In-process registry for background (async) MiniZinc solve jobs.

The single deliberate, bounded exception to the repo's "no global mutable state"
rule (AGENTS.md): one ``JobRegistry`` instance is created per server in
``create_mcp_server`` and owned by that server's lifecycle — never a module-level
singleton. It lets a client submit a solve as a background job, poll its status,
fetch the final ``SolveResult``, and cancel a running job, so hard solves no longer
hit synchronous MCP client timeouts.

Bounded admission, cancel, FIFO retention and shutdown all live in
``shared.job_registry``; this module adds the MiniZinc request type, the two
submission entry points (one job, or an atomic batch for a portfolio), and the
worker that runs a prepared solve.

Layering: this is a server-layer module — it imports the ``minizinc`` solve
machinery and ``schemas``; it never imports ``server``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
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
from ..schemas.diagnostics import Diagnostic, wrapper_job_diagnostic
from ..schemas.job_state import JobState
from ..schemas.minizinc import (
    DEFAULT_SOLVE_CONTROLS,
    SolveControls,
    SolveJobStatus,
    SolveResult,
    job_state_for_result,
)
from ..shared.job_errors import JobRejectedError, UnknownJobError, exception_summary, now_ms
from ..shared.job_registry import BackgroundJobRegistry, JobRecord
from ..shared.proc import terminate_process_tree as _terminate_process_tree


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


_JobRecord = JobRecord[SolveRequest, SolveResult, SolveJobStatus]


class JobRegistry(BackgroundJobRegistry[SolveResult, SolveJobStatus, _JobRecord]):
    """A bounded, single-owned registry of background solve jobs.

    Admitted jobs stay ``queued`` until a worker starts them — including while a
    finishing worker is still notifying its terminal listener — so a job's clock
    starts on the worker, never at admission.
    """

    _queue_label = "Job"
    _thread_name_prefix = "solve-job"
    _logger = logging.getLogger(__name__)

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
        under the lock. Returns immediately (state ``queued``) without awaiting the
        solve; raises ``JobRejectedError`` when the bounded queue is full or shutdown
        has begun — no worker or subprocess is created then.
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
            self._admission_gate_locked()
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
                raise JobRejectedError(self._batch_full_message(len(requests)))
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
                    self._unpublish_locked(job_id)
                raise
            return job_ids

    # --- internals (assume the caller holds the lock unless noted) -------------

    def _terminate(self, handle: Popen[str]) -> None:
        _terminate_process_tree(handle)

    def _unknown_job_error(self, job_id: str) -> Exception:
        # Its own type, so a portfolio skipping evicted attempts does not also
        # swallow pydantic's ValidationError.
        return UnknownJobError(f"unknown job_id: {job_id}")

    def _batch_full_message(self, batch: int) -> str:
        return (
            f"Batch of {batch} job(s) exceeds the bounded capacity ({self._capacity_label()}) "
            f"given {self._in_flight} already in flight. Retry once running jobs finish."
        )

    def _admit_locked(
        self,
        request: SolveRequest,
        *,
        on_terminal: Callable[[int, SolveJobStatus], None] | None = None,
        on_terminal_error: Callable[[int, str], None] | None = None,
        batch_index: int = 0,
    ) -> str:
        # Caller holds the lock AND has already checked capacity. The single admission
        # primitive shared by submit (one) and submit_many (a batch under one lock).
        job_id = uuid4().hex
        record = _JobRecord(
            job_id=job_id,
            request=request,
            submitted_at_ms=now_ms(),
            state="queued",
            on_terminal=on_terminal,
            on_terminal_error=on_terminal_error,
            batch_index=batch_index,
        )
        # Submit first: the worker waits for the caller's lock, so a submit that raises
        # after queueing the item (a worker thread that cannot start) leaves no record
        # for that item to run and no in-flight slot to leak.
        record.future = self._executor.submit(self._run_job, job_id)
        self._publish_locked(record)
        return job_id

    def _to_status(self, record: _JobRecord) -> SolveJobStatus:
        # `result` is stored only on result-bearing terminal states, so passing it
        # straight through already satisfies the SolveJobStatus invariant.
        return SolveJobStatus(
            job_id=record.job_id,
            state=record.state,
            solver=record.request.solver,
            timeout_ms=record.request.timeout_ms,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=self._elapsed_ms(record),
            result=record.result,
            message=record.message,
            diagnostic=self._job_diagnostic(record),
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
        # A requested cancel wins however the killed solve ended, even by raising;
        # a job already finalizing as cancelled keeps its own message.
        if record.cancel_requested and state != "cancelled":
            state, result, message = "cancelled", None, "Cancelled by client"
        return super()._finalize(record, state, result, message)

    def _run_job(self, job_id: str) -> None:
        # The worker callable: mark running, solve, then record the terminal state.
        record = self._begin_job(job_id)
        if record is None:
            return  # un-admitted by a failed batch, or evicted, before it could start
        request = record.request
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
