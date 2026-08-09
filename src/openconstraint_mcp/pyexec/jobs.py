"""In-process registry for background (async) CP-SAT Python jobs.

Parallel to ``jobs.py`` (MiniZinc job registry) but for the CP-SAT Python
execution path. One ``CpsatJobRegistry`` instance is created per server and
captured by the tool closures; it is never a module-level singleton.

Layering: imports ``pyexec.core`` (executor), ``pyexec.checker`` (optional
checker adapter), ``pyexec.eligibility`` (shared diagnostic-incumbent gate),
``schemas`` (output models), ``proc`` (tree-kill), ``job_errors`` (shared
rejection error + job-registry primitives). Never imports ``minizinc``,
``runtime``, ``server``, or ``jobs``.
"""

from __future__ import annotations

import threading
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen
from uuid import uuid4

from ..schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    cpsat_job_state_for_result,
)
from ..schemas.diagnostics import Diagnostic, wrapper_job_diagnostic
from ..schemas.job_state import RESULT_BEARING_STATES, TERMINAL_STATES, JobState
from ..shared.job_errors import JobRejectedError, exception_summary, now_ms
from ..shared.proc import terminate_process_tree as _terminate_process_tree
from .checker import checker_infrastructure_report, run_checker, run_checker_file
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    effective_checker_timeout_ms,
    run_cpsat_python,
    run_cpsat_python_file,
    seed_config_env,
    validate_checker_args,
)
from .diagnostics import checked_result_diagnostic
from .eligibility import diagnostic_incumbent_eligibility
from .script_path import validate_script_args, validate_script_path


@dataclass(frozen=True)
class _CpsatJobRequest:
    """Immutable per-job parameters; kind discriminates source vs. file path.

    ``problem``/``checker``/``checker_path``/``checker_timeout_ms`` are the
    optional diagnostic checker inputs (same contract as the save/experiment
    tools); all four are ``None`` for an unchecked job. ``checker`` (inline
    source) and ``checker_path`` (an on-disk checker, file jobs only) are
    mutually exclusive; ``checker_path`` is resolved at admission so a later
    ``cd`` or symlink swap cannot change what runs.

    ``args`` (file jobs only) is the child's ``sys.argv[1:]``. It is a ``tuple``
    rather than a ``list`` because ``frozen=True`` blocks rebinding but not
    mutation of a contained list, and a queued job's argv must not stay
    live-linked to the caller's list; ``submit_file`` snapshots at admission.
    """

    source: str | None
    script_path: Path | None
    script_timeout_ms: int
    problem: str | None = None
    checker: str | None = None
    checker_path: Path | None = None
    checker_timeout_ms: int | None = None
    args: tuple[str, ...] | None = None

    @property
    def is_file(self) -> bool:
        return self.script_path is not None

    @property
    def has_checker(self) -> bool:
        """Whether a checker of either form was supplied — the one "is this job checked?" rule.

        Both the status echo (``effective_checker_timeout_ms``) and the worker's
        checker phase branch on this, and they MUST agree: a request the phase
        treats as checked but the timeout property calls unchecked returns
        ``None`` into an ``assert``, and the job never finalizes. One property
        so a third checker form cannot update one site and miss the other.
        """
        return self.checker is not None or self.checker_path is not None

    @property
    def effective_checker_timeout_ms(self) -> int | None:
        """The checker child's timeout: explicit value, else the solver's; ``None`` unchecked."""
        if not self.has_checker:
            return None
        return effective_checker_timeout_ms(
            checker_timeout_ms=self.checker_timeout_ms,
            default_script_timeout_ms=self.script_timeout_ms,
        )


@dataclass
class _CpsatJobRecord:
    """Mutable per-job state, guarded by the registry lock."""

    job_id: str
    request: _CpsatJobRequest
    submitted_at_ms: int
    state: JobState
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: CpsatPythonResult | None = None
    message: str | None = None
    checker_report: CpsatCheckerReport | None = None
    checker_skipped_reason: str | None = None
    handle: Popen[str] | None = None
    future: Future[None] | None = None
    cancel_requested: bool = False


class CpsatJobRegistry:
    """A bounded, single-owned registry of background CP-SAT Python jobs.

    Mirrors ``JobRegistry`` (MiniZinc) in structure and contract. Supports two
    submission flavors:
    - ``submit_source`` — inline Python source (same as ``run_cpsat_python``).
    - ``submit_file`` — local script path (same as ``run_cpsat_python_file``).

    ``get`` / ``list`` / ``cancel`` / ``shutdown`` are kind-agnostic. The
    result-presence invariant ``result present ⇔ state ∈ {succeeded, timeout}``
    is enforced by ``CpsatPythonJobStatus``'s model validator (D3). Cancel
    post-run checks ``cancel_requested`` and overrides the executor's ``error``
    result with ``cancelled`` (D4).
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
        self._records: dict[str, _CpsatJobRecord] = {}
        self._terminal_order: list[str] = []
        # Handles of evicted terminal records whose leader was never reaped
        # (returncode None). Eviction drops the record — the only reference to
        # that live child — so the handle is stashed here for shutdown to sweep.
        self._unreaped_orphans: list[Popen[str]] = []
        self._in_flight = 0
        self._executor = ThreadPoolExecutor(
            max_workers=max_running_jobs, thread_name_prefix="cpsat-job"
        )

    def submit_source(
        self,
        source: str,
        *,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        problem: str | None = None,
        checker: str | None = None,
        checker_timeout_ms: int | None = None,
    ) -> str:
        """Admit an inline CP-SAT source as a background job; return ``job_id``.

        Validates ``script_timeout_ms`` (positive gate) and the optional checker args
        up front, then admits under the lock. Returns immediately; raises
        ``ValueError`` on bad args or ``JobRejectedError`` when the bounded
        queue is full.
        """
        if script_timeout_ms <= 0:
            raise ValueError("script_timeout_ms must be positive")
        validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
        request = _CpsatJobRequest(
            source=source,
            script_path=None,
            script_timeout_ms=script_timeout_ms,
            problem=problem,
            checker=checker,
            checker_timeout_ms=checker_timeout_ms,
        )
        with self._lock:
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message())
            return self._admit_locked(request)

    def submit_file(
        self,
        script_path: Path,
        *,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        args: list[str] | None = None,
        problem: str | None = None,
        checker: str | None = None,
        checker_path: Path | None = None,
        checker_timeout_ms: int | None = None,
    ) -> str:
        """Admit a CP-SAT script file as a background job; return ``job_id``.

        Validates ``script_timeout_ms``, the optional checker args, the path
        (exists / regular file / non-empty / UTF-8), AND ``args`` (no embedded
        NUL, bounded total encoding) before admission so a bad argument raises
        ``ValueError`` synchronously and no job record is created — either would
        otherwise surface only when the queued child was spawned, long after this
        call returned a ``job_id``. Raises ``JobRejectedError`` when the queue is
        full.

        ``args`` becomes the child's ``sys.argv[1:]``; it is snapshotted here at
        admission, so mutating the caller's list while the job sits queued
        cannot change what runs.

        ``checker_path`` names an on-disk checker run IN PLACE (``cwd`` is its
        own parent directory), so a checker that reads a relative sibling data
        file resolves — unlike an inline ``checker``, which runs from a temp
        directory. It is validated and resolved here for the same reason
        ``script_path`` is; a file deleted between admission and the checker
        phase surfaces as a ``status="error"`` checker report, not a rejection.
        """
        if script_timeout_ms <= 0:
            raise ValueError("script_timeout_ms must be positive")
        validate_checker_args(
            checker=checker, checker_timeout_ms=checker_timeout_ms, checker_path=checker_path
        )
        resolved = validate_script_path(script_path)
        resolved_checker = (
            validate_script_path(checker_path, parameter="checker_path")
            if checker_path is not None
            else None
        )
        validate_script_args(args)
        request = _CpsatJobRequest(
            source=None,
            script_path=resolved,
            script_timeout_ms=script_timeout_ms,
            problem=problem,
            checker=checker,
            checker_path=resolved_checker,
            checker_timeout_ms=checker_timeout_ms,
            args=tuple(args) if args is not None else None,
        )
        with self._lock:
            if self._in_flight >= self._max_running + self._max_queued:
                raise JobRejectedError(self._queue_full_message())
            return self._admit_locked(request)

    def get(self, job_id: str) -> CpsatPythonJobStatus:
        with self._lock:
            return self._to_status(self._require_record(job_id))

    def list(self) -> list[CpsatPythonJobStatus]:
        with self._lock:
            return [self._to_status(record) for record in self._records.values()]

    def cancel(self, job_id: str) -> CpsatPythonJobStatus:
        """Cancel a job: drop it if still queued, else terminate its process tree.

        A no-op on an already-terminal job. Mirrors ``JobRegistry.cancel``.
        """
        with self._lock:
            record = self._require_record(job_id)
            if record.state in TERMINAL_STATES:
                return self._to_status(record)
            record.cancel_requested = True
            future = record.future
            handle = record.handle
        if future is not None and future.cancel():
            with self._lock:
                self._finalize(record, "cancelled", None, "Cancelled before start")
                return self._to_status(record)
        if handle is not None:
            _terminate_process_tree(handle)
        with self._lock:
            return self._to_status(record)

    def shutdown(self) -> None:
        """Terminate running children and tear down the worker pool (lifespan exit)."""
        with self._lock:
            for record in self._records.values():
                if record.state not in TERMINAL_STATES:
                    record.cancel_requested = True
            records = list(self._records.values())
        for record in records:
            future = record.future
            if future is not None and future.cancel():
                with self._lock:
                    self._finalize(record, "cancelled", None, "Cancelled at shutdown")
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

    def _queue_full_message(self) -> str:
        return (
            f"CP-SAT job queue is full "
            f"({self._max_running} running + {self._max_queued} queued). "
            "Retry once a running job finishes."
        )

    def _admit_locked(self, request: _CpsatJobRequest) -> str:
        job_id = uuid4().hex
        now = now_ms()
        runs_now = self._in_flight < self._max_running
        record = _CpsatJobRecord(
            job_id=job_id,
            request=request,
            submitted_at_ms=now,
            state="running" if runs_now else "queued",
            started_at_ms=now if runs_now else None,
        )
        self._records[job_id] = record
        self._in_flight += 1
        record.future = self._executor.submit(self._run_job, job_id)
        return job_id

    def _require_record(self, job_id: str) -> _CpsatJobRecord:
        record = self._records.get(job_id)
        if record is None:
            raise ValueError(f"unknown job_id: {job_id}")
        return record

    @staticmethod
    def _to_status(record: _CpsatJobRecord) -> CpsatPythonJobStatus:
        if record.state in TERMINAL_STATES:
            elapsed_ms = record.elapsed_ms
        elif record.started_at_ms is not None:
            elapsed_ms = max(now_ms() - record.started_at_ms, 0)
        else:
            elapsed_ms = None
        return CpsatPythonJobStatus(
            job_id=record.job_id,
            state=record.state,
            script_timeout_ms=record.request.script_timeout_ms,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=elapsed_ms,
            result=record.result,
            message=record.message,
            checker=record.checker_report,
            checker_skipped_reason=record.checker_skipped_reason,
            checker_timeout_ms=record.request.effective_checker_timeout_ms,
            diagnostic=CpsatJobRegistry._job_diagnostic(record),
        )

    @staticmethod
    def _job_diagnostic(record: _CpsatJobRecord) -> Diagnostic | None:
        # failed/cancelled -> wrapper diagnostic. Result-bearing states derive
        # from the embedded result; a job-level checker verdict that failed then
        # overrides to checker_failed unless the result timed out. A set
        # checker_skipped_reason (checker
        # supplied but result not checker-eligible) adds no diagnostic on its
        # own — the result-derived diagnostic already reflects the ineligibility.
        wrapper = wrapper_job_diagnostic(
            record.state,
            message=record.message or f"job {record.state}",
            details={"job_id": record.job_id, "state": record.state},
        )
        if wrapper is not None:
            return wrapper
        return checked_result_diagnostic(record.result, record.checker_report)

    def _finalize(
        self,
        record: _CpsatJobRecord,
        state: JobState,
        result: CpsatPythonResult | None,
        message: str | None,
        *,
        checker_report: CpsatCheckerReport | None = None,
        checker_skipped_reason: str | None = None,
    ) -> None:
        if record.state in TERMINAL_STATES:
            return
        now = now_ms()
        record.state = state
        record.finished_at_ms = now
        if record.started_at_ms is not None:
            record.elapsed_ms = max(now - record.started_at_ms, 0)
        result_bearing = state in RESULT_BEARING_STATES
        record.result = result if result_bearing else None
        # Checker outcomes ride only on result-bearing states — a cancelled or
        # failed job discards them, matching the status model's invariant.
        record.checker_report = checker_report if result_bearing else None
        record.checker_skipped_reason = checker_skipped_reason if result_bearing else None
        record.message = message
        self._in_flight -= 1
        self._terminal_order.append(record.job_id)
        self._evict_terminal_overflow()

    def _evict_terminal_overflow(self) -> None:
        while len(self._terminal_order) > self._max_retained_terminal:
            oldest = self._terminal_order.pop(0)
            evicted = self._records.pop(oldest, None)
            handle = evicted.handle if evicted is not None else None
            # poll() (not the stale .returncode) so a leader that exited on its own
            # since finalize gets reaped here instead of stashed as a false orphan.
            if handle is not None and handle.poll() is None:
                # A still-live, unreaped leader whose only reference was this record;
                # keep the handle so shutdown can still terminate it.
                self._unreaped_orphans.append(handle)

    def _on_start(self, job_id: str, proc: Popen[str]) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                cancel_now = True
            else:
                record.handle = proc
                cancel_now = record.cancel_requested
        if cancel_now:
            _terminate_process_tree(proc)

    def _run_job(self, job_id: str) -> None:
        with self._lock:
            record = self._records.get(job_id)
            if record is None:
                return
            request = record.request
            record.state = "running"
            if record.started_at_ms is None:
                record.started_at_ms = now_ms()
        try:
            if request.is_file:
                assert request.script_path is not None
                result = run_cpsat_python_file(
                    request.script_path,
                    script_timeout_ms=request.script_timeout_ms,
                    args=list(request.args) if request.args is not None else None,
                    on_start=lambda proc: self._on_start(job_id, proc),
                    env=seed_config_env(seed=None, config_path=None),
                    spawn_failure_as_result=False,
                )
            else:
                assert request.source is not None
                result = run_cpsat_python(
                    request.source,
                    script_timeout_ms=request.script_timeout_ms,
                    on_start=lambda proc: self._on_start(job_id, proc),
                    env=seed_config_env(seed=None, config_path=None),
                    spawn_failure_as_result=False,
                )
        except Exception as exc:  # noqa: BLE001 - worker boundary: never leak; record as failed
            with self._lock:
                self._finalize(record, "failed", None, exception_summary(exc))
            return
        checker_report, checker_skipped_reason = self._run_checker_phase(job_id, record, result)
        with self._lock:
            if record.cancel_requested:
                # A cancel observed during (or after) the checker phase wins over
                # any checker report AND discards the completed solver result —
                # cancelled never carries a result (deliberately asymmetric with
                # the checker-fault rule, which preserves the solver result).
                self._finalize(record, "cancelled", None, "Cancelled by client")
            else:
                self._finalize(
                    record,
                    cpsat_job_state_for_result(result),
                    result,
                    None,
                    checker_report=checker_report,
                    checker_skipped_reason=checker_skipped_reason,
                )

    def _run_checker_phase(
        self, job_id: str, record: _CpsatJobRecord, result: CpsatPythonResult
    ) -> tuple[CpsatCheckerReport | None, str | None]:
        """Run the optional diagnostic checker against a completed solver result.

        Returns ``(checker_report, checker_skipped_reason)`` — at most one is
        set. Both are ``None`` when no checker was supplied or a cancel was
        already requested (the caller's final cancel check finalizes it). A
        checker infrastructure exception becomes a ``status="error"`` report:
        it must never discard the completed solver result by failing the job.
        This method runs OUTSIDE the worker's own try/except, so the ``except``
        below is the only handler between a checker fault and a job that never
        finalizes — ``run_checker_file`` re-validates ``checker_path`` and
        raises ``ValueError`` if the file vanished after admission, which is
        why its call has to stay inside it.
        """
        if not record.request.has_checker:
            return None, None
        checker = record.request.checker
        checker_path = record.request.checker_path
        with self._lock:
            if record.cancel_requested:
                return None, None
        eligible, reject_reason = diagnostic_incumbent_eligibility(result)
        if not eligible:
            return None, reject_reason
        timeout = record.request.effective_checker_timeout_ms
        assert timeout is not None  # a checker of either form ⇒ effective timeout is set
        try:
            # `on_start` on BOTH branches: it is what points `record.handle` at
            # the checker child so `cancel()` terminates the process actually
            # running, not the finished solver one.
            if checker_path is not None:
                report = run_checker_file(
                    checker_path,
                    result,
                    problem=record.request.problem,
                    timeout_ms=timeout,
                    tracker=None,
                    on_start=lambda proc: self._on_start(job_id, proc),
                )
            else:
                assert checker is not None
                report = run_checker(
                    checker,
                    result,
                    problem=record.request.problem,
                    timeout_ms=timeout,
                    tracker=None,
                    on_start=lambda proc: self._on_start(job_id, proc),
                )
        except Exception as exc:  # noqa: BLE001 - checker fault must not void the solver result
            report = checker_infrastructure_report(exc)
        return report, None
