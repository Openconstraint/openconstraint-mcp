"""Solver-portfolio admission and winner-selection over the server-owned ``JobRegistry``.

A portfolio expands a set of model formulations, solvers, and optional seeds into
independent solve attempts, admits them atomically through the *existing* registry
(no new pool, scheduler, or subprocess runner), and selects a winner from the
attempts' statuses. The background portfolio path (``portfolio_registry``) drives this:
``_admit_portfolio`` admits the plan synchronously (fail-fast on a bad plan or a
full queue) and routes each attempt's terminal event to the registry's listener,
which calls ``_build_portfolio_result`` once every attempt is terminal — the winning
``SolveResult`` plus metadata explaining what happened to every attempt.

Local-first invariants are inherited from the layers below: every attempt runs on
the managed MiniZinc runtime via the registry's cancellable solve, capabilities are
resolved once for the whole plan through the runtime's own ``--solvers-json``, and
nothing leaves the machine. This module orchestrates; it never spawns its own
processes.

Layering: a server-side module that imports ``jobs.registry``, ``minizinc.core``
helpers, and ``schemas``; it never imports ``server``.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from typing import NamedTuple

from pydantic import JsonValue

from ..minizinc.core import (
    build_solve_extra_args,
    resolve_capability_map,
    validate_model_and_timeout,
    validate_solver_capabilities,
)
from ..schemas.diagnostics import Diagnostic
from ..schemas.job_state import JobState
from ..schemas.minizinc import SolveControls, SolveJobStatus, SolveResult
from ..schemas.portfolio import (
    PortfolioAttempt,
    PortfolioAttemptState,
    PortfolioSolveControls,
    PortfolioSolveResult,
    PortfolioStatus,
)
from ..shared.save_target import text_sha256

# portfolio consumes the registry (provider) plus core's capability
# resolver/validator, model/timeout validator, and solve-argv builder; these
# package-internal helpers keep plan-time enforcement and per-attempt argv
# identical to the single solve.
from .registry import JobRegistry, SolveRequest

# The solve verdicts that end the race immediately (a proof or a satisfaction
# solution). The first attempt to reach one of these wins; the rest are cancelled.
_DECISIVE_STATUSES: frozenset[str] = frozenset(
    {"optimal", "satisfied", "unsatisfiable", "unbounded", "unsat_or_unbounded"}
)
# How a winner is chosen, surfaced verbatim on the result so a client can record it.
_SELECTION_POLICY: str = "first-decisive-result"

_JOB_TO_ATTEMPT_STATE: dict[JobState, PortfolioAttemptState] = {
    "queued": "submitted",
    "running": "running",
    "succeeded": "succeeded",
    "timeout": "timeout",
    "failed": "failed",
    "cancelled": "cancelled",
}


class _PortfolioAdmission(NamedTuple):
    """``_admit_portfolio``'s return: the admitted plan plus its provenance.

    ``models_sha256``/``data_sha256``/``checker_sha256``/``solve_controls`` are
    captured here — while the caller's original request values are still in
    scope — because by the time ``_build_portfolio_result`` runs (when the last
    attempt finishes, via the background ``PortfolioJobRegistry``) those originals
    are out of scope. They
    must be threaded through unchanged to the eventual ``PortfolioSolveResult``.
    """

    start: float
    job_ids: list[str]
    plan: list[tuple[int, str, int | None]]
    models_sha256: list[str]
    data_sha256: str | None
    checker_sha256: str | None
    solve_controls: PortfolioSolveControls


def _admit_portfolio(
    registry: JobRegistry,
    *,
    models: Sequence[str],
    solvers: Sequence[str],
    data: str | None,
    checker: str | None,
    seed_count: int,
    per_attempt_timeout_ms: int,
    solve_controls: PortfolioSolveControls,
    on_attempt_terminal: Callable[[int, SolveJobStatus], None],
    seeds: list[int] | None = None,
) -> _PortfolioAdmission:
    """Validate the plan and admit its attempts atomically; return a ``_PortfolioAdmission``.

    The synchronous, fail-fast half of a portfolio: every ``ValueError`` (empty
    ``models``/``solvers``, bad control), capability rejection, and the
    ``JobRejectedError`` for an over-capacity batch is raised HERE, before any
    attempt runs — so ``PortfolioJobRegistry.submit`` fails fast on a bad plan or a
    full queue instead of recording a background job that instantly fails. On return
    the attempts are already admitted to ``registry`` (running or queued), and each
    attempt's terminal status reaches ``on_attempt_terminal`` with its plan index —
    possibly before this function returns. The returned
    ``models_sha256``/``data_sha256``/``checker_sha256`` are provenance hashes of the
    exact ``models``/``data``/``checker`` text this call admitted (see
    ``PortfolioSolveResult``).
    """
    start = time.monotonic()
    if not models:
        raise ValueError("models must not be empty")
    if not solvers:
        raise ValueError("solvers must not be empty")
    if per_attempt_timeout_ms <= 0:
        raise ValueError("per_attempt_timeout_ms must be positive")

    plan_seeds, seed_used = _resolve_plan_seeds(seed_count=seed_count, seeds=seeds)
    # Model index varies fastest so the first attempts span distinct formulations
    # before any one is repeated: with the cap gone, a plan wider than the running
    # limit should still race the formulations first, not stack extra seeds/solvers
    # onto model 0 while the other models wait in the queue.
    plan: list[tuple[int, str, int | None]] = [
        (m_idx, solver, seed)
        for solver in solvers
        for seed in plan_seeds
        for m_idx in range(len(models))
    ]

    _validate_plan_capabilities(solvers=solvers, seed_used=seed_used, controls=solve_controls)

    requests: list[SolveRequest] = []
    for m_idx, solver, seed in plan:
        # Per attempt, model/timeout is checked before the controls are built, so a
        # plan with several problems reports the same first error as before.
        validate_model_and_timeout(models[m_idx], per_attempt_timeout_ms)
        attempt_controls: SolveControls = SolveControls(
            **solve_controls.model_dump(), random_seed=seed
        )
        requests.append(
            SolveRequest(
                model=models[m_idx],
                solver=solver,
                data=data,
                checker=checker,
                timeout_ms=per_attempt_timeout_ms,
                controls=attempt_controls,
                extra_args=build_solve_extra_args(solver, attempt_controls),
            )
        )
    job_ids = registry.submit_many(requests, on_terminal=on_attempt_terminal)
    models_sha256 = [text_sha256(model) for model in models]
    data_sha256 = text_sha256(data) if data is not None else None
    checker_sha256 = text_sha256(checker) if checker is not None else None
    return _PortfolioAdmission(
        start=start,
        job_ids=job_ids,
        plan=plan,
        models_sha256=models_sha256,
        data_sha256=data_sha256,
        checker_sha256=checker_sha256,
        solve_controls=solve_controls,
    )


def _resolve_plan_seeds(
    *, seed_count: int, seeds: list[int] | None
) -> tuple[list[int | None], bool]:
    """Return the attempt seed list and whether the plan uses MiniZinc ``-r``."""
    if seeds is None:
        if seed_count < 1:
            raise ValueError("seed_count must be >= 1")
        if seed_count == 1:
            return [None], False
        return list(range(1, seed_count + 1)), True

    if seed_count != 1:
        raise ValueError("seeds cannot be combined with seed_count != 1")
    if not seeds:
        raise ValueError("seeds must not be empty")
    if len(set(seeds)) != len(seeds):
        raise ValueError("seeds must not contain duplicates")
    return list(seeds), True


def _build_portfolio_result(
    plan: Sequence[tuple[int, str, int | None]],
    statuses: Sequence[SolveJobStatus],
    winner_index: int | None,
    start: float,
    models_sha256: list[str],
    data_sha256: str | None,
    checker_sha256: str | None,
    solve_controls: PortfolioSolveControls,
) -> PortfolioSolveResult:
    """Build the winner-led ``PortfolioSolveResult`` from a terminal attempt snapshot.

    Called by the portfolio registry once every attempt is terminal. With no
    decisive ``winner_index``, falls back to the best available terminal attempt (or
    ``no_winner`` when none produced a usable result); the model enforces
    ``winner present ⇔ status=="winner"``. ``models_sha256``/``data_sha256``/
    ``checker_sha256``/``solve_controls`` are recorded on the result verbatim —
    provenance of what the race actually ran, not a race-time decision (see
    ``PortfolioSolveResult``).
    """
    if winner_index is None:
        winner_index = _best_available_index(statuses)
    attempts = [_to_attempt(index, plan[index], statuses[index]) for index in range(len(statuses))]
    status_value: PortfolioStatus
    if winner_index is None:
        status_value = "no_winner"
        winner_result: SolveResult | None = None
    else:
        status_value = "winner"
        winner_result = statuses[winner_index].result

    return PortfolioSolveResult(
        status=status_value,
        winner_index=winner_index,
        winner=winner_result,
        attempts=attempts,
        elapsed_ms=max(int((time.monotonic() - start) * 1000), 0),
        selection_policy=_SELECTION_POLICY,
        models_sha256=models_sha256,
        data_sha256=data_sha256,
        checker_sha256=checker_sha256,
        solve_controls=solve_controls,
        diagnostic=_portfolio_result_diagnostic(status_value, winner_result, attempts),
    )


def _portfolio_result_diagnostic(
    status: PortfolioStatus,
    winner: SolveResult | None,
    attempts: Sequence[PortfolioAttempt],
) -> Diagnostic | None:
    """Diagnose a race result: ``no_winner``, else the winner's own diagnostic.

    A ``no_winner`` race (no attempt produced a usable result) is ``no_winner``
    with the attempt states seen; a winner surfaces the winning ``SolveResult``'s
    own diagnostic (None for a decisive win, ``timeout_with_incumbent`` for a
    best-available timeout fallback), so the race never invents an outcome the
    winning attempt did not have.
    """
    if status == "no_winner":
        states: list[JsonValue] = [s for s in sorted({attempt.state for attempt in attempts})]
        return Diagnostic(
            category="no_winner",
            message="no attempt produced a usable result",
            details={"attempts": len(attempts), "states": states},
        )
    return winner.diagnostic if winner is not None else None


def _validate_plan_capabilities(
    *,
    solvers: Sequence[str],
    seed_used: bool,
    controls: PortfolioSolveControls,
) -> None:
    """Reject the plan if any solver omits a requested control (one resolve, D4).

    Lazy like the single-solve gate: no ``--solvers-json`` when no gated control is
    requested. Seeds drive ``random_seed`` per attempt, so ``seed_count > 1`` or an
    explicit ``seeds`` list means every solver must support ``-r``. An unresolved
    solver string (a short alias) passes through (D4 case c) — MiniZinc resolves it
    at solve time.
    """
    if not (
        controls.free_search or controls.all_solutions or controls.parallel is not None or seed_used
    ):
        return
    capability_map = resolve_capability_map()
    plan_controls: SolveControls = SolveControls(
        **controls.model_dump(), random_seed=1 if seed_used else None
    )
    for solver in solvers:
        capabilities = capability_map.get(solver)
        if capabilities is None:
            continue
        validate_solver_capabilities(solver, capabilities, plan_controls)


def _is_decisive(status: SolveJobStatus) -> bool:
    """Whether an attempt snapshot carries a verdict that ends the race."""
    return status.result is not None and status.result.status in _DECISIVE_STATUSES


def _first_decisive_index(statuses: Sequence[SolveJobStatus]) -> int | None:
    """Index of the attempt that reached a decisive verdict *first*, by finish order.

    Follows the documented ``first-decisive-result`` policy: among the attempts that
    are decisive in this snapshot, the smallest ``finished_at_ms`` wins, with the
    plan-order index breaking a same-millisecond tie. Two decisive attempts' terminal
    events can arrive out of finish order, and the final snapshot can hold several
    decisive attempts, so taking the lowest index would misreport a later finisher as
    the winner. ``finished_at_ms`` is stamped on every result-bearing terminal
    attempt, so it is non-None for any decisive candidate (the sentinel is defensive
    only, never the deciding value).
    """
    decisive = [
        (status.finished_at_ms, index)
        for index, status in enumerate(statuses)
        if _is_decisive(status)
    ]
    if not decisive:
        return None
    return min(decisive, key=lambda item: (item[0] if item[0] is not None else 2**63, item[1]))[1]


def _best_available_rank(result: SolveResult) -> int:
    """Rank a non-decisive but result-bearing attempt; lower is better (D6).

    Order: a timeout/error that still carried a solution, then ``unknown``, then a
    timeout with no solution, then a bare error.
    """
    has_solution = result.solution is not None or bool(result.solutions)
    if result.status in ("timeout", "error") and has_solution:
        return 0
    if result.status == "unknown":
        return 1
    if result.status == "timeout":
        return 2
    return 3


def _best_available_index(statuses: Sequence[SolveJobStatus]) -> int | None:
    """Pick the best result-bearing attempt by rank then index, or ``None``.

    ``None`` when no attempt produced a usable ``SolveResult`` (all failed or were
    cancelled before producing one).
    """
    ranked = [
        (_best_available_rank(status.result), index)
        for index, status in enumerate(statuses)
        if status.result is not None
    ]
    if not ranked:
        return None
    return min(ranked)[1]


def _to_attempt(
    index: int, plan_entry: tuple[int, str, int | None], status: SolveJobStatus
) -> PortfolioAttempt:
    model_index, solver, seed = plan_entry
    result = status.result
    return PortfolioAttempt(
        index=index,
        model_index=model_index,
        solver=solver,
        seed=seed,
        timeout_ms=status.timeout_ms,
        state=_JOB_TO_ATTEMPT_STATE[status.state],
        job_id=status.job_id,
        job_state=status.state,
        result_status=result.status if result is not None else None,
        objective=result.objective if result is not None else None,
        elapsed_ms=status.elapsed_ms,
        message=status.message,
        checker_status=(
            result.checker.status if result is not None and result.checker is not None else None
        ),
        # The underlying solve job already computed this attempt's diagnostic
        # (wrapper for failed/cancelled, result-derived — checker included — for
        # succeeded/timeout), so reuse it rather than re-deriving.
        diagnostic=status.diagnostic,
    )
