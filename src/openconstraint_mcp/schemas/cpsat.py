from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import (
    BaseModel,
    Field,
    JsonValue,
    StrictInt,
    computed_field,
    field_validator,
    model_validator,
)

from .artifacts import SavedModelArtifact
from .diagnostics import Diagnostic
from .job_state import RESULT_BEARING_STATES, JobState

# ---------------------------------------------------------------------------
# CP-SAT Python executor output models (moved from pyexec/core.py per D7 so
# CpsatPythonJobStatus can reference CpsatPythonResult without a
# schemas → pyexec.core edge that would break the dependency-free leaf).
# ---------------------------------------------------------------------------

CpsatStatus = Literal["optimal", "feasible", "infeasible", "unknown", "error", "timeout"]
CpsatMutationName = Literal[
    "objective_perturbed",
    "element_dropped",
    "element_duplicated",
    "numeric_field_perturbed",
]
CPSAT_MUTATION_NAMES: tuple[CpsatMutationName, ...] = (
    "objective_perturbed",
    "element_dropped",
    "element_duplicated",
    "numeric_field_perturbed",
)


class CpsatPythonResult(BaseModel):
    status: CpsatStatus
    solution: dict[str, Any] | None
    objective: float | int | None
    # OR-Tools' solver.best_objective_bound — a diagnostic bound, not a proven
    # objective. Useful even when status="unknown" and no incumbent was found,
    # since a script may still emit it. None for a script that never reports it
    # (backward compatible with scripts predating this field), reports a
    # non-finite/non-numeric value (normalized like `objective`), or is a pure
    # feasibility problem (no objective — OR-Tools returns a meaningless 0.0
    # rather than raising, so a conforming script reports None instead).
    best_objective_bound: float | int | None = None
    stdout: str
    stderr: str
    return_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int
    diagnostic: Diagnostic | None = None


def cpsat_job_state_for_result(result: CpsatPythonResult) -> JobState:
    """Map a produced ``CpsatPythonResult`` to its terminal ``JobState`` (D3).

    ``timeout`` → ``timeout`` (result-bearing; partial recovered).
    All other statuses — including ``error`` — → ``succeeded``: ``status="error"``
    is a normal structured verdict (the child ran and produced output), not a
    job-machinery failure. The job "succeeded at running the code"; the embedded
    ``CpsatPythonResult.status`` tells the client whether the code itself errored.
    ``failed`` is reserved for a worker exception with no result; ``cancelled``
    for user cancel — both result-absent, consistent with ``result present ⇔
    state ∈ {succeeded, timeout}``.
    """
    if result.timed_out or result.status == "timeout":
        return "timeout"
    return "succeeded"


class CpsatCheckerReport(BaseModel):
    """Result of running an optional checker script against the CP-SAT solution.

    ``status`` is the normalized server verdict: ``accepted`` only when the
    checker returned ``accepted`` with an empty ``errors`` list; ``rejected``
    when the checker rejected; ``timeout`` on a wall-clock timeout;
    ``error`` for malformed output, nonzero exit, truncation, or
    ``accepted``+non-empty-errors (self-contradictory output).
    ``stdout``, ``stderr``, and ``details`` are raw checker output and are
    NOT persisted in the manifest (only the scalar summary is saved).

    Defined before ``CpsatPythonJobStatus`` (which embeds it) so the job-status
    model builds without a deferred forward reference.
    """

    status: Literal["accepted", "rejected", "error", "timeout"]
    errors: list[str]
    details: dict[str, Any] | None = None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool
    truncated: bool
    diagnostic: Diagnostic | None = None


class CpsatMutationOutcome(BaseModel):
    """One deterministic solution mutation's outcome in a checker self-test.

    ``name`` is the mutation's fixed identifier (``objective_perturbed``,
    ``element_dropped``, ``element_duplicated``, ``numeric_field_perturbed``).
    Exactly one of ``status`` and ``skipped_reason`` is set: a mutation ran IFF
    it carries a ``status``, so "the checker tolerated this" is always
    distinguishable from "this was never tried". A ``skipped_reason`` covers
    both a mutation that could not be produced and one whose probe faulted
    mid-flight; either way it was never graded, so ``errors`` stays empty and
    ``duration_ms`` stays ``None`` (no checker child ran to time).

    A COMPACT row, deliberately NOT a nested ``CpsatCheckerReport``. Up to four
    mutants run per checked call, each a full checker child whose ``stdout`` +
    ``stderr`` share a 1 MiB cap and whose ``details`` is arbitrary
    checker-authored JSON. Embedding whole reports would let one tool result
    serialize several MiB of raw output into an MCP client's context, and none
    of it answers the question this probe exists to ask. ``status`` plus a
    prefix of ``errors`` is the entire signal: whether the checker refused the
    mutant and what it said. The projected errors list is capped at 8 KiB of
    compact JSON per row, including an explicit truncation marker. The
    baseline's raw output and complete errors remain available on the result's
    top-level ``checker``.

    The dropped ``timed_out``/``truncated`` flags are derivable from what
    remains — a truncated mutant is ``error`` with ``"checker output was
    truncated"``, a timed-out one is ``timeout`` with ``"checker timed out"``.

    A mutant row deliberately carries NO ``Diagnostic``. Its verdict is
    evidence about the CHECKER, not a failure of this run: ``rejected`` is the
    DESIRED outcome here, so the ``checker_failed`` diagnostic every checker
    report normally carries would invert the meaning of a category clients
    branch on everywhere else.
    """

    name: CpsatMutationName
    status: Literal["accepted", "rejected", "error", "timeout"] | None = None
    errors: list[str] = Field(default_factory=list)
    duration_ms: int | None = None
    skipped_reason: str | None = None

    @model_validator(mode="after")
    def _status_and_reason_are_exclusive(self) -> CpsatMutationOutcome:
        """Enforce the verdict/reason split the checker-test counts are tallied from."""
        if (self.status is None) == (self.skipped_reason is None):
            raise ValueError(
                "CpsatMutationOutcome carries exactly one of status and skipped_reason "
                "(a graded mutation has a verdict; one that could not apply has a "
                "reason and is never silently dropped)"
            )
        return self


class CpsatCheckerTestReport(BaseModel):
    """Report a caller-supplied checker's verdicts on generic mutations.

    Produced only when a checked run opted into ``test_checker`` AND the
    baseline verdict on the real solution was ``accepted`` — there is nothing
    to test the checker against otherwise. Each entry in ``mutations`` is the
    checker re-run against one mutated copy of that solution.

    This report carries no baseline of its own. It only ever appears on a
    ``CpsatPythonCheckedResult`` whose top-level ``checker`` IS that baseline —
    the runner passes one object to both — and a validator there already
    enforces that it was ``accepted``. Repeating it here would serialize a
    second full copy of a report that can hold a MiB of checker output, to say
    something the result already says.

    Both counts are DERIVED from ``mutations``, so neither can drift from the
    table it summarizes. A graded mutant lands in exactly one of two buckets,
    reported separately because they license different conclusions:
    ``rejected_count`` (graded and refused — the checker is not vacuous) and
    ``accepted_count`` (graded and swallowed — the vacuous-checker signal). An
    ``error``/``timeout`` mutant reached no verdict and lands in neither count,
    same as a skipped mutation — so ``rejected_count: 0, accepted_count: 0``
    alone cannot distinguish "nothing was corruptible" from "every mutant that
    ran errored out or timed out"; both leave zero evidence about the checker,
    but for different reasons. A client that needs that distinction reads
    ``mutations`` directly, where each row's ``status`` or ``skipped_reason``
    says which.

    A positive ``rejected_count`` shows that the checker rejected a payload, not
    that it grades every constraint. Zero-of-nonzero is still inconclusive
    because these domain-agnostic mutations are not known-invalid and can remain
    feasible. This report never alters the run's own
    ``status``/``objective``/``solution`` or produces a top-level diagnostic.
    """

    mutations: list[CpsatMutationOutcome] = Field(default_factory=list)
    rejected_count: int = 0
    accepted_count: int = 0

    @model_validator(mode="after")
    def _derive_counts_from_the_mutation_table(self) -> CpsatCheckerTestReport:
        """Recompute both tallies from ``mutations`` — the single source of truth.

        Real fields rather than ``computed_field``s because the MCP SDK builds
        tool output schemas in pydantic's *validation* mode, where computed
        fields are invisible — and these counts are the numbers a client reads
        first, so they have to appear in the advertised schema.
        """
        graded = [m.status for m in self.mutations if m.status is not None]
        self.rejected_count = graded.count("rejected")
        self.accepted_count = graded.count("accepted")
        return self


class CpsatPythonCheckedResult(CpsatPythonResult):
    """A synchronous CP-SAT file run plus its checker verdict.

    The return contract of ``run_cpsat_python_file_checked``: every
    ``CpsatPythonResult`` field, plus the three checker fields mirroring
    ``CpsatPythonJobStatus``.

    - ``checker`` is the checker's report when the checker ran;
      ``CpsatCheckerReport.status`` is the verdict.
    - ``checker_skipped_reason`` is set only when the checker did NOT run
      because the run produced no checkable incumbent. Exactly one of
      ``checker``/``checker_skipped_reason`` is set — unlike
      ``CpsatPythonJobStatus``, where neither is set if no checker was supplied.
    - ``checker_timeout_ms`` echoes the effective checker cap (the explicit
      value, else ``script_timeout_ms``); it is always set, since this tool always
      requests a check.

    - ``checker_test`` is set only when the caller opted into ``test_checker``
      AND the checker accepted; it reports whether that checker rejected any
      generic mutation. ``None`` otherwise — including for a non-``accepted``
      baseline, which leaves nothing to test the checker against. Its rows are
      compact verdicts; ``checker`` above is the one full report returned.

    The top-level ``diagnostic`` composes the run and baseline checker: a run
    timeout wins, else a failed checker overrides, else the run's own diagnostic.
    The self-test never contributes one — see ``CpsatCheckerTestReport`` for
    why. An ``optimal`` run the checker rejects surfaces a ``checker_failed``
    diagnostic.
    """

    checker: CpsatCheckerReport | None = None
    checker_skipped_reason: str | None = None
    checker_timeout_ms: int | None = None
    checker_test: CpsatCheckerTestReport | None = None

    @model_validator(mode="after")
    def _checker_outcome_is_exclusive(self) -> CpsatPythonCheckedResult:
        if self.checker is not None and self.checker_skipped_reason is not None:
            raise ValueError(
                "CpsatPythonCheckedResult checker and checker_skipped_reason are mutually "
                "exclusive (a checker either ran or was skipped, never both)"
            )
        if self.checker is None and self.checker_skipped_reason is None:
            raise ValueError(
                "CpsatPythonCheckedResult requires checker or checker_skipped_reason "
                "(this tool always requests a check, so exactly one outcome exists). "
                "CpsatPythonJobStatus permits neither because a job may supply no checker"
            )
        return self

    @model_validator(mode="after")
    def _checker_test_follows_an_accepted_checker(self) -> CpsatPythonCheckedResult:
        """A probe exists only where there was an accepted verdict to test against.

        The invariant the runner's orchestration actually depends on: mutants are
        graded against the same checker that accepted the real solution, so a
        ``checker_test`` attached to a missing, rejected, errored, or timed-out
        checker would report mutation evidence no run ever produced. This is
        also what lets ``CpsatCheckerTestReport`` omit a baseline of its own:
        the gate below establishes that ``checker`` IS the accepted baseline
        those mutants were graded against.
        """
        if self.checker_test is None:
            return self
        if self.checker is None or self.checker.status != "accepted":
            raise ValueError(
                "CpsatPythonCheckedResult checker_test requires an accepted checker "
                "verdict (there is nothing to test the checker against otherwise)"
            )
        return self


class CpsatPythonJobStatus(BaseModel):
    """A background CP-SAT Python job's status snapshot.

    Mirrors ``SolveJobStatus``: ``result`` is present IFF ``state`` is a
    result-bearing terminal state (``succeeded`` or ``timeout``), absent for
    ``queued``/``running`` and for ``failed``/``cancelled``. The invariant
    ``result present ⇔ state ∈ {succeeded, timeout}`` is enforced.
    ``script_timeout_ms`` echoes the caller's SOLVER-child cap only, so a polling
    client can pace the solve phase (``remaining ≈ script_timeout_ms - elapsed_ms``).
    A checked job may remain ``running`` beyond ``script_timeout_ms`` for the checker
    phase, up to the echoed ``checker_timeout_ms``. ``message`` carries
    failure/cancel detail; a ``failed`` job has no result so its diagnostic
    lives only in ``message``.

    Checker fields (diagnostic only — never a save gate; saving still replays
    through ``save_verified_cpsat_python``):
    - ``checker`` is the checker's report on a result-bearing job whose
      supplied checker ran; ``CpsatCheckerReport.status`` is the verdict (no
      duplicate ``checker_status`` field exists).
    - ``checker_skipped_reason`` is set only when a supplied checker did not
      run (result not checker-eligible). Mutually exclusive with ``checker``,
      and both are restricted to result-bearing states.
    - ``checker_timeout_ms`` is a request echo like ``script_timeout_ms`` (constant
      across states): the effective checker timeout when a checker was
      supplied (the explicit value, else the ``script_timeout_ms`` default), ``None``
      when no checker was supplied.
    """

    job_id: str
    state: JobState
    script_timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: CpsatPythonResult | None = None
    message: str | None = None
    checker: CpsatCheckerReport | None = None
    checker_skipped_reason: str | None = None
    checker_timeout_ms: int | None = None
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> CpsatPythonJobStatus:
        has_result = self.result is not None
        expects_result = self.state in RESULT_BEARING_STATES
        if has_result and not expects_result:
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded' or 'timeout')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} requires a result "
                "(state 'succeeded'/'timeout' ⇔ result is present)"
            )
        if self.checker is not None and self.checker_skipped_reason is not None:
            raise ValueError(
                "CpsatPythonJobStatus checker and checker_skipped_reason are mutually "
                "exclusive (a checker either ran or was skipped, never both)"
            )
        if (self.checker is not None or self.checker_skipped_reason is not None) and (
            not expects_result
        ):
            raise ValueError(
                f"CpsatPythonJobStatus state={self.state!r} must not carry checker or "
                "checker_skipped_reason (checker outcomes appear only on state "
                "'succeeded' or 'timeout')"
            )
        return self


# ---------------------------------------------------------------------------
# CP-SAT Python verification gate schemas
# ---------------------------------------------------------------------------

CpsatObjectiveSense = Literal["maximize", "minimize"]

# The highest gate that passed during a save attempt. "none" means even the
# reported gate failed (nothing was saved). The level never claims a save
# happened — combine with `saved` for that.
CpsatVerificationLevel = Literal["none", "reported", "expectation", "checked"]


class CpsatExpectation(BaseModel):
    """Caller-supplied objective threshold for a CP-SAT save gate.

    A threshold gate is NOT an optimality proof — it only checks that the
    script's reported objective meets the supplied bound. For satisfaction
    problems (no objective), omit this and use a checker instead.
    """

    objective_sense: CpsatObjectiveSense
    objective_threshold: float

    @field_validator("objective_threshold", mode="before")
    @classmethod
    def _reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("objective_threshold must be a number, not a bool")
        return value

    @field_validator("objective_threshold", mode="after")
    @classmethod
    def _reject_non_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("objective_threshold must be a finite number (not NaN or ±inf)")
        return value


class SaveVerifiedPythonResult(BaseModel):
    """Outcome of a save_verified_cpsat_python request.

    ``saved`` is computed from ``reason``: True iff ``reason`` is None and
    ``target_dir`` is set. It reports PERSISTENCE only, never the verdict — a
    passing ``verify_only`` run deliberately returns ``reason=None`` with
    ``saved=False``. The verdict is ``reason is None`` (every supplied gate
    passed) plus the per-gate fields — ``reported_passed``,
    ``expectation_passed``, ``checker`` — and ``verification_level``, the
    highest gate that passed.

    Gate short-circuit order: reported → expectation → checker. Every gate
    downstream of the first failure carries its None/False default.
    """

    status: CpsatStatus
    target_dir: str | None
    reason: str | None
    solution: dict[str, Any] | None
    objective: float | int | None
    stdout: str
    stderr: str
    timed_out: bool
    truncated: bool
    duration_ms: int
    files: list[SavedModelArtifact] = Field(default_factory=list)

    # Verification gate summary — always present
    verification_level: CpsatVerificationLevel = "none"
    reported_passed: bool = False
    expectation: CpsatExpectation | None = None
    expectation_passed: bool | None = None
    checker: CpsatCheckerReport | None = None
    diagnostic: Diagnostic | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def saved(self) -> bool:
        return self.reason is None and self.target_dir is not None


# ---------------------------------------------------------------------------
# CP-SAT Python explicit-experiment schemas
# ---------------------------------------------------------------------------

# An experiment's overall outcome. ``winner`` ⇔ an accepted attempt was selected
# (its full ``CpsatPythonResult`` plus ``winner_index``/``winner_name`` are set);
# ``no_winner`` ⇔ no attempt was accepted. This ⇔ is enforced below.
CpsatExperimentStatus = Literal["winner", "no_winner"]

# How an experiment winner is chosen, surfaced as a typed (not free-form) value:
# optimization: best objective for the requested sense, then stronger status
# (optimal > feasible > timeout), then faster duration_ms, then earliest attempt
# order. Feasibility: stronger status, then faster duration_ms, then earliest
# attempt order. Never completion order.
CpsatExperimentSelectionPolicy = Literal[
    "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
    "accepted_status_then_duration_then_attempt_order",
]

# A per-attempt checker verdict (None when no checker was supplied for the
# experiment, or when the attempt failed base acceptance and the checker was
# never run on it).
CpsatExperimentCheckerStatus = Literal["accepted", "rejected", "error", "timeout"]


class CpsatPythonExperimentAttempt(BaseModel):
    """One explicit attempt in a ``run_cpsat_python_experiment`` request.

    An attempt supplies its script EXACTLY ONE of two ways: inline ``source``,
    or an on-disk ``script_path``. Setting both, or neither, is rejected before
    any attempt runs. ``source`` is a complete, independent CP-SAT Python
    script — the server does not diff or merge attempts, so each one must be
    runnable on its own. ``script_path`` is a local path to such a script; it
    runs with ``cwd`` set to the script's own parent directory (identically to
    ``run_cpsat_python_file``), so a relative ``open()`` of a sibling data file
    resolves. ``args`` are appended after the script path as ``sys.argv[1:]``
    and are only meaningful with ``script_path`` — supplying them alongside
    ``source`` is rejected rather than silently ignored. ``name``
    is optional; an unnamed attempt is assigned the display label
    ``attempt-{index}`` at execution time (see ``CpsatPythonExperimentAttemptResult``).
    ``seed`` injects ``OPENCONSTRAINT_MCP_CPSAT_SEED`` for a cooperating script,
    identically to the save path's seeded replay. ``config`` is an OPAQUE JSON
    object the server writes to a temp file and points
    ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` at — the server never sets OR-Tools
    parameters itself; only a cooperating script that reads the env var and
    applies fields it understands benefits from it. An empty ``config`` (``{}``)
    is normalized to "no config" everywhere (no temp file, no env var, no hash).
    ``script_timeout_ms`` overrides the request's ``default_script_timeout_ms`` for this one
    attempt.
    """

    name: str | None = None
    source: str | None = None
    script_path: str | None = None
    args: list[str] | None = None
    seed: StrictInt | None = None
    config: dict[str, JsonValue] = Field(default_factory=dict)
    script_timeout_ms: int | None = None


class CpsatPythonExperimentAttemptResult(BaseModel):
    """One attempt's observed outcome in an experiment's attempt table.

    ``name`` is always the RESOLVED display label (explicit, or the
    ``attempt-{index}`` default) — never ``None`` — so it can double as
    ``winner_name`` and as the uniqueness key attempts were validated over.
    ``config_sha256`` is the canonical-JSON hash of this attempt's ``config``,
    or ``None`` when the attempt ran with no config (``{}`` and omitted are
    equivalent). ``source_sha256`` is this attempt's exact script hash —
    provenance for a later save's replay-consistency check; unnormalized in
    both branches (the inline text as given, or the on-disk file's raw bytes).
    ``used_script_path`` records that this attempt ran from an on-disk
    ``script_path`` rather than inline ``source``, which makes it unusable as
    ``save_verified_cpsat_python`` provenance (that save's rerun is always
    inline ``source`` with a fresh temp-dir ``cwd``, so it can replay neither
    ``args`` nor a ``cwd``-relative sibling data file). It defaults to
    ``False`` because every attempt row produced before this field existed was
    inline-only, making ``False`` the correct value for such a row rather than
    a bypass. ``checker_status``
    is ``None`` when no checker was supplied for the experiment, or this
    attempt failed base acceptance before the checker could run.
    ``stderr_tail`` is a bounded tail of ``stderr``, populated only when
    ``status == "error"`` and ``stderr`` is non-empty — ``None`` for every
    other status (including ``timeout``, ``infeasible``, ``feasible``,
    ``optimal``, or an ``error`` with empty ``stderr``). This is separate
    from ``message``'s short single-line snippet: ``message`` stays concise
    for the printed attempt table, ``stderr_tail`` is a larger bounded tail
    for debugging, carried only in ``structuredContent``.
    ``best_objective_bound`` is diagnostic only — it is never consulted for
    ``accepted``/winner selection, so an ``unknown`` attempt with no
    incumbent can still surface search progress via this field.
    """

    index: int
    name: str
    seed: int | None = None
    config_sha256: str | None = None
    source_sha256: str
    used_script_path: bool = False
    script_timeout_ms: int
    status: CpsatStatus
    objective: float | int | None
    best_objective_bound: float | int | None = None
    accepted: bool
    checker_status: CpsatExperimentCheckerStatus | None = None
    message: str | None = None
    timed_out: bool
    truncated: bool
    duration_ms: int
    stderr_tail: str | None = None
    diagnostic: Diagnostic | None = None


class CpsatPythonExperimentResult(BaseModel):
    """Outcome of a CP-SAT Python explicit experiment: the winner and the full table.

    ``status="winner"`` carries the winning attempt's ``winner_index`` (a 0-based
    handle into ``attempts``), ``winner_name`` (equal to
    ``attempts[winner_index].name``), and the full ``winner`` ``CpsatPythonResult``;
    ``status="no_winner"`` leaves all three ``None`` because no attempt was
    accepted. The invariant ``winner ⇔ winner_index ⇔ winner_name ⇔ status ==
    "winner"`` is enforced, and — going one step further than
    ``PortfolioSolveResult`` — ``winner_index`` is bounds-checked against
    ``attempts`` and ``winner_name`` is checked to match the winning row's own
    ``name`` HERE, in the schema, so the save gate and ``server.py`` formatting
    can trust the winner fields without re-checking them defensively (unlike
    ``PortfolioSolveResult``'s ``winner_index``, whose bounds are instead
    checked eagerly by the minizinc save path's
    ``_validate_portfolio_result_consistency``).

    A ``timeout`` winner is a REPORTABLE best incumbent, not a SAVABLE one: it
    fails ``save_verified_cpsat_python``'s reported gate (``optimal``/``feasible``
    only).

    ``source_sha256`` is one hex digest per attempt, index-aligned with
    ``attempts`` (so ``source_sha256[i] == attempts[i].source_sha256``).
    ``checker_sha256``/``problem_sha256`` are the hashes of the experiment's
    shared ``checker``/``problem`` text, or ``None`` when omitted. All three are
    provenance, computed once for the request that produced this result — not a
    save-time trust decision (the save gate always re-runs the winner fresh).

    ``warnings`` carries non-blocking advisory messages and defaults to an
    empty list. Two independent sources populate it: the
    ``num_workers``-oversubscription check (only when triggered), and — added
    unconditionally whenever ``status == "winner"`` — a reproducibility
    disclaimer noting that an experiment winner is one observed run, not a
    guarantee that ``save_verified_cpsat_python``'s fresh re-run will find the
    same objective.
    """

    status: CpsatExperimentStatus
    winner_index: int | None = None
    winner_name: str | None = None
    winner: CpsatPythonResult | None = None
    attempts: list[CpsatPythonExperimentAttemptResult] = Field(default_factory=list)
    elapsed_ms: int
    objective_sense: CpsatObjectiveSense | None
    selection_policy: CpsatExperimentSelectionPolicy
    source_sha256: list[str] = Field(default_factory=list)
    checker_sha256: str | None = None
    problem_sha256: str | None = None
    warnings: list[str] = Field(default_factory=list)
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _winner_presence_matches_status(self) -> CpsatPythonExperimentResult:
        has_winner = self.winner is not None
        index_present = self.winner_index is not None
        name_present = self.winner_name is not None
        status_winner = self.status == "winner"
        if not (has_winner == index_present == name_present == status_winner):
            raise ValueError(
                "CpsatPythonExperimentResult requires winner, winner_index, "
                "winner_name, and status=='winner' to agree (all present "
                "together or all absent)"
            )
        self._validate_winner_attempt_fields()
        return self

    def _validate_winner_attempt_fields(self) -> None:
        if self.winner_index is None:
            return
        if not (0 <= self.winner_index < len(self.attempts)):
            raise ValueError(
                f"winner_index {self.winner_index} is out of range for "
                f"{len(self.attempts)} attempts"
            )
        if self.attempts[self.winner_index].name != self.winner_name:
            raise ValueError(
                "winner_name must equal attempts[winner_index].name "
                f"({self.winner_name!r} != {self.attempts[self.winner_index].name!r})"
            )
