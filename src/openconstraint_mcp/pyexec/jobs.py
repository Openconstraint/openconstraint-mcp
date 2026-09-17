"""In-process registry for background (async) CP-SAT Python jobs.

The CP-SAT Python execution path's background registry. One
``CpsatJobRegistry`` instance is created per server and captured by the tool
closures; it is never a module-level singleton. The bounded admission, cancel,
FIFO retention and shutdown lifecycle lives in ``shared.job_registry``; this
module adds the request type, the two submission flavors, the status snapshot,
and the worker's solver + optional-checker phases.

Layering: imports ``pyexec.core`` (executor), ``pyexec.checker`` (optional
checker adapter), ``pyexec.eligibility`` (shared diagnostic-incumbent gate),
``schemas`` (output models), ``proc`` (tree-kill), ``job_errors`` (shared
rejection error + job-registry primitives), ``job_registry`` (the shared
lifecycle). Never imports ``minizinc``, ``runtime``, ``server``, or ``jobs``.
"""

from __future__ import annotations

import logging
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
from ..schemas.job_state import RESULT_BEARING_STATES, JobState
from ..shared.job_errors import exception_summary, now_ms
from ..shared.job_registry import BackgroundJobRegistry, JobRecord
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


class _CpsatJobRecord(JobRecord[_CpsatJobRequest, CpsatPythonResult, CpsatPythonJobStatus]):
    """Adds the checker phase's outcome, of which at most one is ever set."""

    checker_report: CpsatCheckerReport | None = None
    checker_skipped_reason: str | None = None

    def set_checker_outcome(
        self, report: CpsatCheckerReport | None, skipped_reason: str | None
    ) -> None:
        """Publish the checker phase's outcome — the only writer of these fields.

        Called under the lock, only while finalizing to a result-bearing state, so a
        cancelled or failed job never carries one (the status model's invariant).
        """
        self.checker_report = report
        self.checker_skipped_reason = skipped_reason


class CpsatJobRegistry(
    BackgroundJobRegistry[CpsatPythonResult, CpsatPythonJobStatus, _CpsatJobRecord]
):
    """A bounded, single-owned registry of background CP-SAT Python jobs.

    Unlike the MiniZinc registry, a job admitted while a running slot is free
    reports ``running`` at once instead of staying ``queued`` until a worker
    starts it. Supports two submission flavors:
    - ``submit_source`` — inline Python source (same as ``run_cpsat_python``).
    - ``submit_file`` — local script path (same as ``run_cpsat_python_file``).

    ``get`` / ``list`` / ``cancel`` / ``shutdown`` are kind-agnostic. The
    result-presence invariant ``result present ⇔ state ∈ {succeeded, timeout}``
    is enforced by ``CpsatPythonJobStatus``'s model validator (D3). Cancel
    post-run overrides a completed run's result with ``cancelled`` (D4), while a
    wrapper exception still reports ``failed``.
    """

    _queue_label = "CP-SAT job"
    _thread_name_prefix = "cpsat-job"
    _logger = logging.getLogger(__name__)

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
        queue is full or shutdown has begun.
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
            self._admission_gate_locked()
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
        full or shutdown has begun.

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
            self._admission_gate_locked()
            return self._admit_locked(request)

    # --- internals (assume the caller holds the lock unless noted) -------------

    def _terminate(self, handle: Popen[str]) -> None:
        _terminate_process_tree(handle)

    def _admit_locked(self, request: _CpsatJobRequest) -> str:
        # Caller holds the lock and has already checked capacity. A job with a free
        # running slot is published as `running`, with its clock already started.
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
        # Publish before submit, deliberately the reverse of the MiniZinc registry (which
        # submits first so a raising submit leaks no slot); `_publish_locked` carries no
        # shared ordering guarantee.
        self._publish_locked(record)
        record.future = self._executor.submit(self._run_job, job_id)
        return job_id

    def _to_status(self, record: _CpsatJobRecord) -> CpsatPythonJobStatus:
        return CpsatPythonJobStatus(
            job_id=record.job_id,
            state=record.state,
            script_timeout_ms=record.request.script_timeout_ms,
            submitted_at_ms=record.submitted_at_ms,
            started_at_ms=record.started_at_ms,
            finished_at_ms=record.finished_at_ms,
            elapsed_ms=self._elapsed_ms(record),
            result=record.result,
            message=record.message,
            checker=record.checker_report,
            checker_skipped_reason=record.checker_skipped_reason,
            checker_timeout_ms=record.request.effective_checker_timeout_ms,
            diagnostic=self._job_diagnostic(record),
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
    ) -> bool:
        # A cancel observed during (or after) the checker phase wins over any
        # checker report AND discards the completed solver result — cancelled never
        # carries a result (deliberately asymmetric with the checker-fault rule,
        # which preserves the solver result). A wrapper exception carries no result,
        # and still reports `failed` even under a requested cancel.
        # Keyed off the state, not off `result is not None`: `_complete` is a shared
        # base-class entry point (worker, cancel, shutdown), so only the base's own
        # `result present ⇔ state ∈ RESULT_BEARING_STATES` invariant is guaranteed here.
        if record.cancel_requested and state in RESULT_BEARING_STATES:
            state, result, message = "cancelled", None, "Cancelled by client"
        return super()._finalize(record, state, result, message)

    def _run_job(self, job_id: str) -> None:
        record = self._begin_job(job_id)
        if record is None:
            return  # evicted before the worker could start it
        request = record.request
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
            # Inside the boundary too: an exception escaping the checker phase (one
            # its own handler does not catch) fails the job rather than leaving it
            # unfinalized forever.
            report, skipped_reason = self._run_checker_phase(job_id, record, result)
            # Inside the boundary as well: an exception from the state mapping would
            # otherwise leave the record `running` forever and leak its in-flight slot.
            state: JobState = cpsat_job_state_for_result(result)
        except Exception as exc:  # noqa: BLE001 - worker boundary: never leak; record as failed
            self._complete(record, "failed", None, exception_summary(exc))
            return
        # The outcome stays local until completion publishes it under the lock that
        # writes the terminal state and result, so no poll sees it on a running job.
        self._complete(
            record,
            state,
            result,
            None,
            lambda rec: rec.set_checker_outcome(report, skipped_reason),
        )

    def _run_checker_phase(
        self, job_id: str, record: _CpsatJobRecord, result: CpsatPythonResult
    ) -> tuple[CpsatCheckerReport | None, str | None]:
        """Run the optional diagnostic checker against a completed solver result.

        Returns ``(checker_report, checker_skipped_reason)`` — at most one is
        set. Both are ``None`` when no checker was supplied or a cancel was
        already requested (finalization turns that into ``cancelled``). A
        checker infrastructure exception becomes a ``status="error"`` report:
        it must never discard the completed solver result by failing the job,
        which is why ``run_checker_file`` — it re-validates ``checker_path`` and
        raises ``ValueError`` if the file vanished after admission — has to stay
        inside the ``except`` below.
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
