"""Background (async) registry for solver-portfolio races — collect-on-poll.

The async face of a solver portfolio: a portfolio race can run for minutes, far
past a synchronous MCP client timeout, so this registry admits the race's attempts
synchronously (failing fast on a bad plan or a full queue, exactly like
``submit_solve_job``) and hands the client a ``portfolio_job_id`` to poll. The
attempts ARE ordinary jobs in the shared ``JobRegistry``; the only thing this
registry adds is winner-selection, which it runs *lazily on each poll* — there is
no background worker thread or second executor. ``submit`` records the admitted
attempt ids and returns; ``get`` reads the attempts' current statuses and, once one
is decisive, cancels the losers and caches the aggregate ``PortfolioSolveResult``.

Trade-off versus an eager background runner: a loser is cancelled when the client
next polls rather than the instant a winner appears (bounded by each attempt's own
``per_attempt_timeout_ms``). For a local, polling client that is negligible, and it
removes a whole parallel worker pool — winner-selection is a pure function of the
attempts' statuses, so it needs no thread of its own.

One ``PortfolioJobRegistry`` is created per server in ``create_mcp_server``. It owns
no threads or processes (the attempts live in the ``JobRegistry``, torn down by that
registry's shutdown), so it needs no shutdown of its own.

Layering: a server-layer module that imports ``portfolio`` (admission + the
selection pass), ``registry`` (the attempt registry it drives), and ``schemas``; it
never imports ``server``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from ..minizinc.core import DEFAULT_SOLVE_TIMEOUT_MS
from ..schemas.diagnostics import wrapper_job_diagnostic
from ..schemas.portfolio import (
    PortfolioJobState,
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from ..shared.job_errors import JobRejectedError, now_ms

# portfolio_registry reuses portfolio's synchronous admission (_admit_portfolio) and
# its non-blocking selection pass (_select_portfolio_outcome). These are
# package-internal helpers, not a public API.
# noinspection PyProtectedMember
from .portfolio import _admit_portfolio, _select_portfolio_outcome
from .registry import JobRegistry


@dataclass
class _PortfolioRecord:
    """Mutable per-portfolio-job state, guarded by the registry lock.

    Holds only metadata: the admitted ``attempt_job_ids`` (so ``cancel`` can stop
    them and ``get`` can read them), the ``plan`` and monotonic ``start`` needed to
    build the aggregate, the ``models_sha256``/``data_sha256``/``checker_sha256``/
    ``solve_controls`` provenance ``_admit_portfolio`` captured while the original
    request was still in scope, and — once terminal — the cached ``result``/
    ``message``.
    """

    job_id: str
    submitted_at_ms: int
    started_at_ms: int
    per_attempt_timeout_ms: int
    start: float
    attempt_job_ids: list[str]
    plan: list[tuple[int, str, int | None]]
    models_sha256: list[str]
    data_sha256: str | None
    checker_sha256: str | None
    solve_controls: PortfolioSolveControls
    state: PortfolioJobState = "running"
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: PortfolioSolveResult | None = None
    message: str | None = None
    attempt_pins_released: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


type _EvictedPortfolioRecords = list[_PortfolioRecord]


def _to_status(record: _PortfolioRecord) -> PortfolioJobStatus:
    # `result` lives only on a succeeded record, so it already satisfies the
    # PortfolioJobStatus invariant. `elapsed_ms` is frozen at finalize for a
    # terminal job; a `running` job derives it from `started_at_ms` so it advances.
    if record.state != "running":
        elapsed_ms = record.elapsed_ms
    else:
        elapsed_ms = max(now_ms() - record.started_at_ms, 0)
    # `cancelled` gets a wrapper diagnostic; `succeeded` derives from the race
    # result (None for a decisive winner, no_winner/timeout otherwise); `running`
    # has none. A portfolio has no `failed`/`timeout` state.
    diagnostic = wrapper_job_diagnostic(
        record.state,
        message=record.message or f"portfolio {record.state}",
        details={"job_id": record.job_id, "state": record.state},
    )
    if diagnostic is None and record.result is not None:
        diagnostic = record.result.diagnostic
    return PortfolioJobStatus(
        job_id=record.job_id,
        state=record.state,
        per_attempt_timeout_ms=record.per_attempt_timeout_ms,
        submitted_at_ms=record.submitted_at_ms,
        started_at_ms=record.started_at_ms,
        finished_at_ms=record.finished_at_ms,
        elapsed_ms=elapsed_ms,
        result=record.result,
        message=record.message,
        diagnostic=diagnostic,
    )


class PortfolioJobRegistry:
    """A bounded, single-owned registry of background portfolio races (collect-on-poll).

    ``submit`` admits a plan's attempts synchronously (raising ``ValueError`` /
    ``JobRejectedError`` before any job exists) and records them; ``get`` drives one
    selection pass and caches the result once decided; ``cancel`` stops a running
    race's attempts; ``list`` reads status. ``running`` is the only non-terminal
    state, and only ``succeeded`` carries the aggregate result.
    """

    def __init__(
        self,
        registry: JobRegistry,
        *,
        max_running: int = 64,
        max_retained_terminal: int = 64,
    ) -> None:
        if max_running < 1:
            raise ValueError("max_running must be >= 1")
        if max_retained_terminal < 1:
            raise ValueError("max_retained_terminal must be >= 1")
        self._registry = registry
        self._max_running = max_running
        self._max_retained = max_retained_terminal
        self._lock = threading.Lock()
        self._records: dict[str, _PortfolioRecord] = {}
        self._terminal_order: list[str] = []

    def submit(
        self,
        *,
        models: Sequence[str],
        solvers: Sequence[str],
        data: str | None = None,
        checker: str | None = None,
        seed_count: int = 1,
        seeds: list[int] | None = None,
        per_attempt_timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        solve_controls: PortfolioSolveControls | None = None,
    ) -> str:
        """Admit a portfolio as a background race; return its ``portfolio_job_id``.

        Validation, capability enforcement, and the over-capacity ``JobRejectedError``
        all happen synchronously here (the attempts are admitted before this returns),
        so a bad plan or full queue fails fast as it does for the synchronous tool —
        no job is created in those cases. Returns immediately with the job ``running``;
        the winner is selected lazily by ``get``.

        Also bounded: a portfolio job leaves its attempt records *pinned* (so the race
        can still report a loser's fate) until the job is polled or cancelled to a
        terminal state, and a ``running`` portfolio is never retention-evicted. So a
        client that submits and never polls would accumulate pinned attempt records
        without limit. Rejecting beyond ``max_running`` concurrent non-terminal
        portfolios caps that — symmetric with how ``JobRegistry`` bounds in-flight
        solves. This is a coarse leak guard, not a hard invariant: the pre-admission
        count check can transiently overshoot by a few under concurrent ``submit``,
        which is harmless (the hard bound on the real resource — attempt slots — is
        still ``submit_many``'s atomic capacity check).
        """
        with self._lock:
            # Every terminal record is in `_terminal_order`, so the difference is the
            # live (non-terminal) portfolio count — no separate counter to keep.
            if len(self._records) - len(self._terminal_order) >= self._max_running:
                raise JobRejectedError(
                    f"Too many running portfolio jobs (max {self._max_running}). Poll "
                    "or cancel a running portfolio before submitting another."
                )
        # Built per call: PortfolioSolveControls is mutable and is recorded by
        # reference, so a shared default instance would leak across portfolios.
        controls: PortfolioSolveControls = (
            solve_controls
            if solve_controls is not None
            else PortfolioSolveControls(
                free_search=False, parallel=None, all_solutions=False, num_solutions=None
            )
        )
        admission = _admit_portfolio(
            self._registry,
            models=models,
            solvers=solvers,
            data=data,
            checker=checker,
            seed_count=seed_count,
            seeds=seeds,
            per_attempt_timeout_ms=per_attempt_timeout_ms,
            solve_controls=controls,
            pin_attempts=True,
        )
        try:
            job_id = uuid4().hex
            now = now_ms()
            record = _PortfolioRecord(
                job_id=job_id,
                submitted_at_ms=now,
                started_at_ms=now,
                per_attempt_timeout_ms=per_attempt_timeout_ms,
                start=admission.start,
                attempt_job_ids=list(admission.job_ids),
                plan=list(admission.plan),
                models_sha256=list(admission.models_sha256),
                data_sha256=admission.data_sha256,
                checker_sha256=admission.checker_sha256,
                solve_controls=admission.solve_controls,
            )
            with self._lock:
                self._records[job_id] = record
            return job_id
        except Exception:
            self._registry.release_pins(admission.job_ids)
            raise

    def get(self, job_id: str) -> PortfolioJobStatus:
        """Poll a portfolio job, driving one winner-selection pass if still racing.

        Returns the cached status once terminal. Otherwise reads the attempts'
        statuses once: if a winner is decided (or all attempts finished without one),
        cancels any still-running losers, settles, caches the aggregate, and reports
        ``succeeded``; while undecided, reports ``running``. The per-record lock
        serializes concurrent polls so only one runs the selection.
        """
        with self._lock:
            record = self._require_record(job_id)
        # Hold the per-record lock across the selection so two concurrent polls do
        # not both cancel/settle; the registry lock is free meanwhile.
        with record.lock:
            if record.state != "running":
                return _to_status(record)
            outcome = _select_portfolio_outcome(
                self._registry,
                record.attempt_job_ids,
                record.plan,
                record.start,
                record.models_sha256,
                record.data_sha256,
                record.checker_sha256,
                record.solve_controls,
            )
            if outcome is None:
                return _to_status(record)
            self._finalize(record, "succeeded", outcome, None)
            return _to_status(record)

    def cancel(self, job_id: str) -> PortfolioJobStatus:
        """Stop a running portfolio race and its attempts; a no-op once terminal.

        Cancels every attempt (idempotent in the solve registry) and finalizes the
        job ``cancelled`` with no aggregate result. Cancelling an already-terminal
        job returns its status unchanged.
        """
        with self._lock:
            record = self._require_record(job_id)
        with record.lock:
            if record.state != "running":
                return _to_status(record)
            for attempt_id in record.attempt_job_ids:
                self._registry.cancel(attempt_id)
            self._finalize(record, "cancelled", None, "Cancelled by client")
            return _to_status(record)

    def list(self) -> list[PortfolioJobStatus]:
        # Snapshot the record set under the registry lock, then read each record
        # under ITS OWN lock — _finalize mutates state before result, so a lockless
        # read could catch the transient state='succeeded' with result=None and trip
        # the PortfolioJobStatus validator. Per-record locking is taken WITHOUT the
        # registry lock held, so it never inverts _finalize's record.lock -> _lock
        # order (no deadlock).
        with self._lock:
            records = list(self._records.values())
        statuses: list[PortfolioJobStatus] = []
        for record in records:
            with record.lock:
                statuses.append(_to_status(record))
        return statuses

    # --- internals -------------------------------------------------------------

    def _require_record(self, job_id: str) -> _PortfolioRecord:
        # Caller holds the registry lock.
        record = self._records.get(job_id)
        if record is None:
            raise ValueError(f"unknown portfolio job_id: {job_id}")
        return record

    def _finalize(
        self,
        record: _PortfolioRecord,
        state: PortfolioJobState,
        result: PortfolioSolveResult | None,
        message: str | None,
    ) -> None:
        # Caller holds record.lock. Records the terminal state and registers the job
        # for retention eviction under the registry lock.
        now = now_ms()
        record.state = state
        record.finished_at_ms = now
        record.elapsed_ms = max(now - record.started_at_ms, 0)
        record.result = result if state == "succeeded" else None
        record.message = message
        self._release_attempt_pins(record)
        with self._lock:
            self._terminal_order.append(record.job_id)
            evicted = self._evict_terminal_overflow()
        for evicted_record in evicted:
            self._release_attempt_pins(evicted_record)

    def _release_attempt_pins(self, record: _PortfolioRecord) -> None:
        if record.attempt_pins_released:
            return
        record.attempt_pins_released = True
        self._registry.release_pins(record.attempt_job_ids)

    def _evict_terminal_overflow(self) -> _EvictedPortfolioRecords:
        # Caller holds the registry lock. FIFO eviction of terminal jobs beyond the
        # retention cap, so a long-lived server cannot grow unbounded.
        evicted: _EvictedPortfolioRecords = []
        while len(self._terminal_order) > self._max_retained:
            oldest = self._terminal_order.pop(0)
            record = self._records.pop(oldest, None)
            if record is not None:
                evicted.append(record)
        return evicted
