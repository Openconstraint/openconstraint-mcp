"""Background (async) registry for solver-portfolio races — event-driven selection.

The async face of a solver portfolio: a portfolio race can run for minutes, far
past a synchronous MCP client timeout, so this registry admits the race's attempts
synchronously (failing fast on a bad plan or a full queue, exactly like
``submit_solve_job``) and hands the client a ``portfolio_job_id`` to poll. The
attempts ARE ordinary jobs in the shared ``JobRegistry``; the only thing this
registry adds is winner-selection, driven by that registry's terminal listener.
Each time an attempt finishes, ``_on_attempt_terminal`` caches its status; the first
decisive attempt cancels the still-pending losers at once, and when every attempt is
terminal the aggregate ``PortfolioSolveResult`` is built and the job finalizes
``succeeded`` (or ``failed``, if the aggregate cannot be built).
``get``/``list`` only read, so a never-polled race still finishes.

The listener runs on the finishing attempt's worker thread (or synchronously inside
the ``JobRegistry.cancel``/``shutdown`` that finalized a queued attempt) — there is
no worker thread or executor of its own. Lock order is ``record.lock``, then this
registry's ``_lock``. While holding ``record.lock`` the portfolio never calls a
``JobRegistry`` method that can run a listener on the calling thread (``cancel``,
``shutdown``); ``submit_many`` is safe there because it never runs a listener itself.

One ``PortfolioJobRegistry`` is created per server in ``create_mcp_server``. It owns
no threads or processes (the attempts live in the ``JobRegistry``, torn down by that
registry's shutdown), so it needs no shutdown of its own.

Layering: a server-layer module that imports ``portfolio`` (admission + result
building), ``registry`` (the attempt registry it drives), and ``schemas``; it never
imports ``server``.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import uuid4

from ..minizinc.core import DEFAULT_SOLVE_TIMEOUT_MS
from ..schemas.diagnostics import wrapper_job_diagnostic
from ..schemas.minizinc import SolveJobStatus
from ..schemas.portfolio import (
    PortfolioJobState,
    PortfolioJobStatus,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from ..shared.job_errors import exception_summary, now_ms

# portfolio_registry reuses portfolio's synchronous admission (_admit_portfolio) and
# its result builder and decisiveness rules. These are package-internal helpers, not
# a public API.
# noinspection PyProtectedMember
from .portfolio import (
    _admit_portfolio,
    _build_portfolio_result,
    _first_decisive_index,
    _is_decisive,
)
from .registry import JobRegistry


@dataclass
class _PortfolioRecord:
    """Mutable per-portfolio-job state, guarded by ``lock``.

    ``submit`` creates the record before admission and, while holding ``lock``, fills
    the admission fields: the admitted ``attempt_job_ids``, the ``plan`` and monotonic
    ``start`` needed to build the aggregate, the ``models_sha256``/``data_sha256``/
    ``checker_sha256``/``solve_controls`` provenance ``_admit_portfolio`` captured, and
    one ``statuses`` slot per plan index. An attempt event that arrives before
    admission returns waits on ``lock`` until they are set. ``statuses`` caches each
    attempt's terminal snapshot, ``decided`` marks that the losers were already
    cancelled, and — once terminal — ``result``/``message`` are cached.
    """

    job_id: str
    submitted_at_ms: int
    started_at_ms: int
    per_attempt_timeout_ms: int
    solve_controls: PortfolioSolveControls
    start: float = 0.0
    attempt_job_ids: list[str] = field(default_factory=list)
    plan: list[tuple[int, str, int | None]] = field(default_factory=list)
    models_sha256: list[str] = field(default_factory=list)
    data_sha256: str | None = None
    checker_sha256: str | None = None
    statuses: list[SolveJobStatus | None] = field(default_factory=list)
    decided: bool = False
    state: PortfolioJobState = "running"
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: PortfolioSolveResult | None = None
    message: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


def _pending_attempt_ids(record: _PortfolioRecord) -> list[str]:
    # Caller holds record.lock. Attempts that have not reported a terminal status.
    return [
        job_id
        for job_id, cached in zip(record.attempt_job_ids, record.statuses, strict=True)
        if cached is None
    ]


def _to_status(record: _PortfolioRecord) -> PortfolioJobStatus:
    # `result` lives only on a succeeded record, so it already satisfies the
    # PortfolioJobStatus invariant. `elapsed_ms` is frozen at finalize for a
    # terminal job; a `running` job derives it from `started_at_ms` so it advances.
    if record.state != "running":
        elapsed_ms = record.elapsed_ms
    else:
        elapsed_ms = max(now_ms() - record.started_at_ms, 0)
    # `cancelled`/`failed` get a wrapper diagnostic; `succeeded` derives from the race
    # result (None for a decisive winner, no_winner/timeout otherwise); `running`
    # has none. A portfolio has no `timeout` state.
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
    """A bounded, single-owned registry of background portfolio races (event-driven).

    ``submit`` admits a plan's attempts synchronously (raising ``ValueError`` /
    ``JobRejectedError`` before any job exists) and records them; attempt terminal
    events settle the race; ``get`` and ``list`` read status; ``cancel`` stops a
    running race's attempts. ``running`` is the only non-terminal state, and only
    ``succeeded`` carries the aggregate result.
    """

    def __init__(
        self,
        registry: JobRegistry,
        *,
        max_retained_terminal: int = 64,
    ) -> None:
        if max_retained_terminal < 1:
            raise ValueError("max_retained_terminal must be >= 1")
        self._registry = registry
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
        the race then settles on its attempts' terminal events. Running portfolios need
        no bound of their own: a portfolio stays ``running`` only while one of its
        attempts has not reported terminal, and the last report always finalizes it
        (``succeeded``, or ``failed`` when the aggregate cannot be built), so live
        portfolios are bounded by the solve registry's capacity.
        """
        # Built per call: PortfolioSolveControls is mutable and is recorded by
        # reference, so a shared default instance would leak across portfolios.
        controls: PortfolioSolveControls = (
            solve_controls
            if solve_controls is not None
            else PortfolioSolveControls(
                free_search=False, parallel=None, all_solutions=False, num_solutions=None
            )
        )
        now: int = now_ms()
        record: _PortfolioRecord = _PortfolioRecord(
            job_id=uuid4().hex,
            submitted_at_ms=now,
            started_at_ms=now,
            per_attempt_timeout_ms=per_attempt_timeout_ms,
            solve_controls=controls,
        )
        # Hold record.lock across admission: an attempt can finish before submit_many
        # returns, and its event must wait until the record is complete. A rejected
        # plan admits nothing and fires no listener, so the record is simply dropped.
        with record.lock:
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
                on_attempt_terminal=lambda index, status: self._on_attempt_terminal(
                    record, index, status
                ),
            )
            record.start = admission.start
            record.attempt_job_ids = list(admission.job_ids)
            record.plan = list(admission.plan)
            record.models_sha256 = list(admission.models_sha256)
            record.data_sha256 = admission.data_sha256
            record.checker_sha256 = admission.checker_sha256
            record.solve_controls = admission.solve_controls
            record.statuses = [None] * len(admission.plan)
            with self._lock:
                self._records[record.job_id] = record
        return record.job_id

    def get(self, job_id: str) -> PortfolioJobStatus:
        """Read a portfolio job's status; never selects a winner or cancels anything."""
        with self._lock:
            record = self._require_record(job_id)
        with record.lock:
            return _to_status(record)

    def cancel(self, job_id: str) -> PortfolioJobStatus:
        """Stop a running portfolio race and its attempts; a no-op once terminal.

        Finalizes the job ``cancelled`` (no aggregate result) under ``record.lock``,
        then — outside it, since cancelling a queued attempt runs its listener on this
        thread — cancels every attempt that has not reported terminal. Their late
        events are ignored. Cancelling an already-terminal job returns its status
        unchanged.
        """
        with self._lock:
            record = self._require_record(job_id)
        with record.lock:
            if record.state != "running":
                return _to_status(record)
            self._finalize(record, "cancelled", None, "Cancelled by client")
            status: PortfolioJobStatus = _to_status(record)
            pending: list[str] = _pending_attempt_ids(record)
        self._cancel_attempts(pending)
        return status

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

    def _on_attempt_terminal(
        self, record: _PortfolioRecord, index: int, status: SolveJobStatus
    ) -> None:
        # The JobRegistry terminal listener for one attempt (never called under that
        # registry's lock). Losers are cancelled after record.lock is released,
        # because cancelling a queued loser re-enters this listener synchronously.
        losers: list[str] = []
        with record.lock:
            if record.state != "running":
                return
            record.statuses[index] = status
            if not record.decided and _is_decisive(status):
                record.decided = True
                losers = _pending_attempt_ids(record)
            if all(cached is not None for cached in record.statuses):
                self._settle(record)
        self._cancel_attempts(losers)

    def _settle(self, record: _PortfolioRecord) -> None:
        # Caller holds record.lock and every attempt status is cached. The winner is
        # computed over the final snapshot, so decisive events that arrived out of
        # finish order still honor the earliest-finish rule.
        statuses: list[SolveJobStatus] = [
            cached for cached in record.statuses if cached is not None
        ]
        try:
            result: PortfolioSolveResult = _build_portfolio_result(
                record.plan,
                statuses,
                _first_decisive_index(statuses),
                record.start,
                record.models_sha256,
                record.data_sha256,
                record.checker_sha256,
                record.solve_controls,
            )
        except Exception as exc:  # noqa: BLE001 - an internal bug must still end the race
            self._finalize(
                record,
                "failed",
                None,
                f"Could not build the portfolio result: {exception_summary(exc)}",
            )
            return
        self._finalize(record, "succeeded", result, None)

    def _cancel_attempts(self, job_ids: Sequence[str]) -> None:
        # Caller must NOT hold record.lock (see _on_attempt_terminal).
        for job_id in job_ids:
            try:
                self._registry.cancel(job_id)
            except ValueError:
                # Unknown job_id: the solve registry evicts only terminal records, so
                # this attempt already finished and there is nothing to stop.
                continue

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
        with self._lock:
            self._terminal_order.append(record.job_id)
            self._evict_terminal_overflow()

    def _evict_terminal_overflow(self) -> None:
        # Caller holds the registry lock. FIFO eviction of terminal jobs beyond the
        # retention cap, so a long-lived server cannot grow unbounded.
        while len(self._terminal_order) > self._max_retained:
            oldest = self._terminal_order.pop(0)
            self._records.pop(oldest, None)
