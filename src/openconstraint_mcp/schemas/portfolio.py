from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .diagnostics import Diagnostic
from .job_state import JobState
from .minizinc import CheckerStatus, SolveResult, SolveStatus

# One portfolio attempt's lifecycle as the portfolio observed it. `submitted`/
# `running` are non-terminal (a race settles on its first decisive attempt without
# waiting for the losers, so a loser can still be running); `succeeded`/`timeout`/
# `failed`/`cancelled` mirror the registry's terminal `JobState`; `rejected` marks
# an attempt that was never admitted (the atomic `submit_many` makes the happy path
# admit all or none, so `rejected` is a defensive vocabulary entry, not produced on a
# returned result).
PortfolioAttemptState = Literal[
    "submitted",
    "running",
    "succeeded",
    "timeout",
    "failed",
    "cancelled",
    "rejected",
]
# A portfolio's overall outcome. `winner` ⇔ an attempt was selected (its winning
# `SolveResult` is attached and `winner_index` is set); the winning result's own
# `status` says whether the win was decisive (a proof/solution) or a best-available
# fallback (timeout/unknown/error). `no_winner` ⇔ no attempt produced a usable
# result (all failed/cancelled). This ⇔ is enforced on `PortfolioSolveResult`.
PortfolioStatus = Literal["winner", "no_winner"]


class PortfolioAttempt(BaseModel):
    """One model/solver/seed attempt in a portfolio race, as of when the race settled.

    Records which formulation it ran (`model_index`, a 0-based handle into the
    caller's `models` list), its `solver`/`seed` (the exact requested or generated
    seed value, or ``None`` when the portfolio ran unseeded), the portfolio-level
    `state` and raw registry `job_state` (``None`` if it was never admitted) at
    settlement, and — when a ``SolveResult`` was produced — the `result_status` and
    `objective`. A loser not yet stopped at settlement stays `running`/`submitted`;
    its final outcome is not recorded (see ``PortfolioSolveResult``). `message`
    carries failure/cancel detail; `job_id` is the registry handle (``None`` when
    not admitted). The winning formulation is `models[attempts[winner_index].model_index]`.
    `checker_status` is the attempt's own checker verdict (``None`` when no checker
    was supplied to the race, or the attempt never produced a result) — purely
    observational: it does not affect winner selection, so a checker-violated
    attempt can still win the race.
    """

    index: int
    model_index: int
    solver: str
    seed: int | None = None
    timeout_ms: int
    state: PortfolioAttemptState
    job_id: str | None = None
    job_state: JobState | None = None
    result_status: SolveStatus | None = None
    objective: int | float | None = None
    elapsed_ms: int | None = None
    message: str | None = None
    checker_status: CheckerStatus | None = None
    diagnostic: Diagnostic | None = None


class PortfolioSolveControls(BaseModel):
    """The shared solve controls every attempt in a portfolio race ran with.

    Provenance, recorded at admission time like the sha256 hashes on
    ``PortfolioSolveResult``: these four controls are applied uniformly to every
    attempt's ``SolveRequest``, so they live once on the race result rather than
    on each ``PortfolioAttempt`` row. A save that attaches the race as
    ``portfolio_result`` must replay with the same values — unlike
    ``timeout_ms``, which is a budget rather than search configuration (and is
    already recorded per attempt), these change what the solver searches, so a
    mismatch means the save is not replaying the winning attempt's run.
    """

    free_search: bool
    parallel: int | None
    all_solutions: bool
    num_solutions: int | None


class PortfolioSolveResult(BaseModel):
    """Outcome of a solver-portfolio race over the managed runtime.

    ``status="winner"`` carries the winning attempt's `winner_index` and full
    `winner` ``SolveResult``; ``status="no_winner"`` leaves both ``None`` because
    no attempt produced a usable result. The invariant ``winner present ⇔
    winner_index present ⇔ status == "winner"`` is enforced, so a client can branch
    on `status` and trust the winner fields. `attempts` records every attempt's
    state as of when the race settled: the winner, the losers that had finished,
    and any loser not yet stopped (``running``/``submitted``). Remaining attempts
    are cancelled after settlement; their cleanup does not delay the winner.
    This snapshot is never refreshed, even on later portfolio polls or when saved
    to ``experiment-log.json``: ``running`` is the state at settlement, not evidence
    that a loser is still executing. Final loser outcomes are not tracked here.
    `selection_policy` documents how the winner was chosen (e.g.
    ``"first-decisive-result"``).

    ``models_sha256``/``data_sha256``/``checker_sha256`` are provenance: sha256 hex
    digests of the exact ``models``/``data``/``checker`` text the race was admitted
    with, computed once at admission time (before any attempt ran) rather than at
    save time, since a later save-time input is untrusted client round-trip data —
    the binding must reflect what actually ran. ``models_sha256`` is one digest per
    formulation, index-aligned with the caller's ``models`` list (so
    ``models_sha256[attempt.model_index]`` names the exact text an attempt ran);
    ``data_sha256``/``checker_sha256`` are ``None`` iff the race ran with no
    ``data``/``checker`` supplied (an empty-string input, if ever accepted, still
    hashes to ``sha256("")`` — never ``None``). They let a later save-with-result
    flow verify the race ran against the exact same text it is being asked to save,
    and let a persisted experiment log stay self-describing after it leaves the
    original request. This task only computes and records these fields — no gating
    on them happens here: a checker-hash mismatch between race and save is not a
    rejection, the fresh save-time checker decides, and the recorded hash only lets
    a log say which checker gated the race.

    ``solve_controls`` records the shared search configuration the race ran under
    (see ``PortfolioSolveControls``) — captured at admission time for the same
    round-trip-trust reason as the hashes above.
    """

    status: PortfolioStatus
    winner_index: int | None = None
    winner: SolveResult | None = None
    attempts: list[PortfolioAttempt] = Field(default_factory=list)
    elapsed_ms: int
    selection_policy: str
    models_sha256: list[str]
    data_sha256: str | None
    checker_sha256: str | None
    solve_controls: PortfolioSolveControls
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _winner_presence_matches_status(self) -> PortfolioSolveResult:
        has_winner = self.winner is not None
        index_present = self.winner_index is not None
        status_winner = self.status == "winner"
        if not (has_winner == index_present == status_winner):
            raise ValueError(
                "PortfolioSolveResult requires winner, winner_index, and "
                "status=='winner' to agree (all present together or all absent)"
            )
        return self

    @model_validator(mode="after")
    def _attempt_model_indices_in_range(self) -> PortfolioSolveResult:
        for attempt in self.attempts:
            if not (0 <= attempt.model_index < len(self.models_sha256)):
                raise ValueError(
                    f"attempt index={attempt.index} has model_index="
                    f"{attempt.model_index}, out of range for {len(self.models_sha256)} "
                    "models_sha256 entries"
                )
        return self


# A background portfolio job's lifecycle. Unlike a single solve job there is no
# `queued`/`timeout`: the race's attempts are admitted to the solve
# registry the moment the portfolio is submitted (so a full-queue rejection surfaces
# synchronously, not as a job), winner-selection is a pure function of the attempts'
# statuses (a per-attempt failure is captured in the attempts table, and a race with
# no decisive winner is still a SUCCESSFUL orchestration carrying a `no_winner`
# PortfolioSolveResult; `failed` means an internal completion, notification, or
# aggregate-building error), and
# only `succeeded` is result-bearing.
PortfolioJobState = Literal["running", "succeeded", "failed", "cancelled"]


class PortfolioJobStatus(BaseModel):
    """A background portfolio race's status snapshot (the async face of a portfolio).

    The portfolio analogue of ``SolveJobStatus``: ``submit_portfolio_job`` returns
    one with ``state="running"`` immediately so the race never blocks past a
    synchronous MCP client timeout, and ``get_portfolio_job`` polls it to a terminal
    state. ``result`` (the full ``PortfolioSolveResult``, winner and the
    attempt table at settlement alike) is present IFF ``state == "succeeded"`` — this is
    enforced, so a client branches on ``state`` and trusts ``result``'s presence.
    A ``no_winner`` race is ``succeeded`` (the orchestration completed); ``failed``
    means an internal completion/notification error or failure to build the race
    result (``message`` says why);
    ``cancelled`` means the client stopped the race. Mid-race statistics are not provided: a
    ``running`` job reports only ``state``, ``elapsed_ms``, and the requested
    ``per_attempt_timeout_ms`` so a client can pace polling against the per-attempt
    budget instead of guessing an interval.
    """

    job_id: str
    state: PortfolioJobState
    per_attempt_timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: PortfolioSolveResult | None = None
    message: str | None = None
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> PortfolioJobStatus:
        has_result = self.result is not None
        expects_result = self.state == "succeeded"
        if has_result and not expects_result:
            raise ValueError(
                f"PortfolioJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"PortfolioJobStatus state={self.state!r} requires a result "
                "(state 'succeeded' ⇔ result is present)"
            )
        return self
