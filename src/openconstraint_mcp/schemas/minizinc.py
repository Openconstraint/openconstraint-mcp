from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .artifacts import SavedModelArtifact
from .diagnostics import Diagnostic
from .job_state import RESULT_BEARING_STATES, JobState


class SolverCapabilities(BaseModel):
    # Read from the solver config's stdFlags (--solvers-json): which standard
    # solve controls the managed runtime declares for this solver. ENFORCED — a
    # requested all_solutions/free_search/parallel/random_seed is rejected before
    # solving when the matching flag is absent (matched by canonical solver id).
    supports_all_solutions: bool = False  # -a
    supports_free_search: bool = False  # -f
    supports_parallel: bool = False  # -p
    supports_random_seed: bool = False  # -r
    # NOT stdFlags-derived. The conservative, canonical num_solutions gate
    # (org.gecode.gecode / org.chuffed.chuffed only). org.gecode.gist lists -n in
    # stdFlags but is excluded, so this deliberately diverges from std_flags.
    supports_num_solutions: bool = False  # -n
    # Raw stdFlags, advisory/informational. May include flags the server exposes
    # no named control for; NOT a passthrough surface.
    std_flags: list[str] = Field(default_factory=list)


class SolverInfo(BaseModel):
    id: str
    name: str
    version: str | None = None
    tags: list[str] = Field(default_factory=list)
    capabilities: SolverCapabilities = Field(default_factory=SolverCapabilities)


class SolverList(BaseModel):
    solvers: list[SolverInfo]
    capability_note: str = "Detailed solver capabilities are available on request for every solver."


SolveStatus = Literal[
    "satisfied",
    "optimal",
    "unsatisfiable",
    "unknown",
    "unbounded",
    "unsat_or_unbounded",
    "error",
    "timeout",
]


# A `--solution-checker` aggregate verdict — honest, NOT pass/fail (Decision D4).
# `completed` means the checker ran for every produced solution with no nested
# UNSATISFIABLE; it does NOT mean "all author-correct" (author CORRECT/INCORRECT
# text is a convention the server never adjudicates). `violation` is the one
# verdict the server asserts on its own (a constraint-style checker rejected a
# solution). `error` covers a stream ERROR, a nonzero return code (broken/missing
# checker, wrong suffix), or a checker-verdict count that misaligns with the
# produced solutions.
CheckerStatus = Literal["completed", "violation", "no_solution", "error", "timeout"]


class SolutionCheck(BaseModel):
    """One produced solution's checker verdict, index-aligned with the solve's ``solutions``.

    ``violation`` is the one machine-readable signal — True iff this solution
    carried a nested checker ``status: UNSATISFIABLE`` (a constraint-style
    rejection). ``output`` is the best-effort per-solution verdict text (the
    author's ``CORRECT``/``INCORRECT`` text, surfaced verbatim and NOT
    interpreted) or, for a rejection, the violation diagnostic; the verbatim
    record lives in ``CheckerReport.transcript``.
    """

    violation: bool
    output: str


class CheckerReport(BaseModel):
    """A ``--solution-checker`` run's per-solution verdicts and honest aggregate.

    Populated on a ``SolveResult`` only when a checker was supplied (otherwise
    ``SolveResult.checker`` is ``None``). ``checks`` is the best-effort structured
    view, one entry per produced solution, index-aligned with the solve's
    ``solutions`` when ``status`` is ``completed`` or ``violation`` — and that
    ``solutions`` list INCLUDES checker-rejected solutions (a violation does not
    suppress the solution), so on ``status == "violation"`` consult the
    per-solution ``checks`` rather than assuming every produced solution is valid.
    ``transcript`` is the raw ``--json-stream`` transcript (solve + checker
    objects) exactly as the subprocess emitted it — the AUTHORITATIVE checker
    record; ``SolveResult.stdout`` carries only the reconstructed solution text
    (the solve parser drops checker objects), which is why the transcript is
    preserved here. The checker never proves optimality — ``SolveResult.status``
    remains the only proof of completeness/optimality.
    """

    status: CheckerStatus
    checks: list[SolutionCheck] = Field(default_factory=list)
    transcript: str


class SolveResult(BaseModel):
    status: SolveStatus
    solver: str
    return_code: int | None
    timed_out: bool
    # True when the child's combined stdout+stderr exceeded the 1 MiB output cap.
    # Tree termination is requested if the child was last seen running, but a burst
    # writer can overrun the cap and exit on its own before the poll loop observes it.
    # Partial parsed solutions are kept (each json-stream line is complete).
    # `return_code` is None when the cap branch requested termination (the child was
    # last seen running, so its exit code may be the executor's artifact, not the
    # model's); an overrun the loop never observed, where the child exited on its
    # own (with ANY code), keeps its genuine code.
    # The structured diagnostic is `output_truncated` either way.
    truncated: bool = False
    stdout: str
    stderr: str
    elapsed_ms: int
    statistics: dict[str, str] = Field(default_factory=dict)
    # Structured solutions parsed from the driver's json-stream (the `output.json`
    # section of each solution object, with `_objective` removed). `solution` is
    # the last/best solution, `solutions` the full ordered list (improving
    # sequence for optimization, enumeration for `all_solutions`), and `objective`
    # the best solution's `_objective` — None for satisfaction and no-solution runs.
    solution: dict[str, Any] | None = None
    solutions: list[dict[str, Any]] = Field(default_factory=list)
    objective: int | float | None = None
    # Populated iff a solution checker was supplied to the solve (the optional
    # `--solution-checker`); None for an ordinary solve. The checker validates
    # each produced solution; it never proves optimality (see CheckerReport).
    checker: CheckerReport | None = None
    # Stage 2 structured diagnostic (None on a clean success). Additive: the
    # authoritative outcome stays `status`; this is the stable branch point.
    diagnostic: Diagnostic | None = None


class SolveControls(BaseModel):
    """The five MiniZinc solve controls, carried as one internal record.

    Internal only: it appears in no MCP tool schema (tools keep flat parameters
    and build one of these). Range checks deliberately live in
    ``build_solve_extra_args``, not in field validators, so a bad value keeps its
    existing error text. Field order is load-bearing: the saved manifest's
    ``solve_controls`` splices ``model_dump()`` after ``timeout_ms``.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    free_search: bool = False
    parallel: int | None = None
    random_seed: int | None = None
    all_solutions: bool = False
    num_solutions: int | None = None


DEFAULT_SOLVE_CONTROLS: SolveControls = SolveControls()


def job_state_for_result(result: SolveResult) -> JobState:
    """Map a produced ``SolveResult`` to its terminal ``JobState`` (D1.9).

    Total over all eight ``SolveStatus`` values for the *result-present* paths: a
    subprocess timeout (``result.timed_out``) or a stream ``timeout`` verdict maps
    to ``"timeout"``; every other status — INCLUDING ``"error"`` — maps to
    ``"succeeded"``, because ``status="error"`` is a normal structured
    solver/driver verdict (e.g. cp-sat rejecting an out-of-range ``random_seed``),
    not a job-machinery failure. The result-absent terminal states are set by the
    registry, not derived here: ``"failed"`` for a runner exception (no
    ``SolveResult`` produced) and ``"cancelled"`` for user cancellation — both
    result-absent, consistent with ``result present ⇔ state ∈ {succeeded, timeout}``.
    """
    if result.timed_out or result.status == "timeout":
        return "timeout"
    return "succeeded"


class SolveJobStatus(BaseModel):
    """A background solve job's status snapshot.

    ``result`` is present IFF ``state`` is a result-bearing terminal state
    (``succeeded`` or ``timeout``); it is ``None`` for ``queued``/``running`` (not
    finished) and for ``failed``/``cancelled`` (no usable result). This is the
    load-bearing D1.9 invariant — ``result present ⇔ state ∈ {succeeded, timeout}``
    — and it is *enforced*, so a client can branch on ``state`` and trust
    ``result``'s presence (``result is None`` alone does not imply ``failed``).
    Partial mid-run statistics are not provided in this increment: a
    ``running`` job reports ``state``, ``elapsed_ms``, and the requested
    ``timeout_ms`` only; ``statistics`` and the ``SolveResult`` populate on
    terminal success. ``timeout_ms`` echoes the caller's solve time-limit (the
    same value passed to ``submit_solve_job``) and is present in every state, so
    a polling client can pace itself against the remaining budget
    (``remaining ≈ timeout_ms - elapsed_ms``) instead of guessing a poll
    interval. ``message`` carries failure/cancel detail and never replaces a
    ``SolveResult.stderr`` (a ``failed`` job has no result, so its diagnostic
    lives only in ``message``).
    """

    job_id: str
    state: JobState
    solver: str
    timeout_ms: int
    submitted_at_ms: int
    started_at_ms: int | None = None
    finished_at_ms: int | None = None
    elapsed_ms: int | None = None
    result: SolveResult | None = None
    message: str | None = None
    diagnostic: Diagnostic | None = None

    @model_validator(mode="after")
    def _result_presence_matches_state(self) -> SolveJobStatus:
        has_result = self.result is not None
        expects_result = self.state in RESULT_BEARING_STATES
        if has_result and not expects_result:
            raise ValueError(
                f"SolveJobStatus state={self.state!r} must not carry a result "
                "(result is present only for state 'succeeded' or 'timeout')"
            )
        if expects_result and not has_result:
            raise ValueError(
                f"SolveJobStatus state={self.state!r} requires a result "
                "(state 'succeeded'/'timeout' ⇔ result is present)"
            )
        return self


CheckStatus = Literal[
    "ok",  # rc == 0 — the model compiled (flattened) for the chosen solver
    "error",  # rc != 0 — syntax/type/include/domain/unsupported-construct error (see stderr)
    "timeout",  # subprocess wall-clock cap fired during compilation
]


class CheckResult(BaseModel):
    status: CheckStatus
    solver: str
    # True when the child's combined stdout+stderr exceeded the 1 MiB output cap
    # (same contract as `SolveResult.truncated`): stdout/stderr are partial and
    # the diagnostic is `output_truncated`. `status` stays rc-driven — a burst
    # writer can overrun the cap and still exit 0, so `ok` and truncation coexist,
    # while a cap tree-kill surfaces as `error` (deliberate fail-closed: the
    # server never claims a compile it killed succeeded — no solve-style masking).
    truncated: bool = False
    stdout: str
    stderr: str
    elapsed_ms: int
    diagnostic: Diagnostic | None = None


SaveStatus = Literal[
    "saved",  # verification gate passed and the artifact directory was written
    "not_verified",  # a check/solve/checker gate failed — NOTHING was written
]


class SaveVerifiedModelResult(BaseModel):
    """Outcome of a save-verified-model request.

    ``status="saved"`` means the server re-verified the supplied artifacts
    through the managed runtime (clean check, satisfied/optimal solve, checker
    ``completed`` when one was supplied) and wrote the artifact directory.
    ``status="not_verified"`` means a verification gate failed and nothing was
    written — ``target_dir`` is echoed for both outcomes, so on
    ``not_verified`` it names the directory that was *not* written. ``check``
    is always present (the compile gate runs first); ``solve`` is ``None``
    exactly when that check gate failed and no solve ran. ``files`` defaults
    to empty and is populated only on ``status="saved"``.
    """

    status: SaveStatus
    message: str
    target_dir: str
    files: list[SavedModelArtifact] = Field(default_factory=list)
    check: CheckResult
    solve: SolveResult | None = None
    diagnostic: Diagnostic | None = None


# MiniZinc 2.9.7 `--model-interface-only` base-type vocabulary, verified against
# the managed binary. `set of`/array/`opt` are reported as MODIFIERS on a base
# type (`set`/`dim`/`optional`), not as their own tags, so they are absent here;
# an enum collapses to "int" (the enum name lives only in `--model-types-only`).
# `tuple`/`record` get their own tag with no component breakdown in this mode.
# `ann` is MiniZinc's annotation type (e.g. a `seq_search` strategy list); like
# any base type it can carry `dim`, so `array of ann` reports `ann` at dim 1.
InterfaceBaseType = Literal["int", "bool", "float", "string", "tuple", "record", "ann"]
# MiniZinc's "method" value — the solve kind, directly from the interface output.
SolveMethod = Literal["sat", "min", "max"]
InspectStatus = Literal[
    "ok",  # rc == 0 — the interface was extracted (NOT a data-completeness signal)
    "error",  # rc != 0, or rc 0 with unparseable interface output (see stderr)
    "timeout",  # subprocess wall-clock cap fired during type analysis
]


class InterfaceType(BaseModel):
    """One model-interface entry's type, mapped from MiniZinc's interface JSON.

    Plain public fields with NO Pydantic aliases: ``parse_model_interface`` maps
    MiniZinc's raw ``type``/``set``/``dim``/``optional`` keys onto these names as it
    builds each entry, so both the advertised ``outputSchema`` and the emitted
    ``structuredContent`` use ``base_type``/``is_set``/``is_optional`` and cannot
    disagree.
    """

    base_type: InterfaceBaseType
    dim: int = 0  # array dimensionality (0 = scalar)
    is_set: bool = False
    is_optional: bool = False  # MiniZinc `opt` type (reported as "optional": true)


class ModelInterface(BaseModel):
    """A MiniZinc model's interface, extracted without solving.

    ``required_parameters`` are the parameters still needing a value given any
    data supplied (MiniZinc's ``input``); an empty map means the data is
    complete. ``output_variables`` (MiniZinc's ``output``) are advisory — with an
    ``output`` item they track the model's output variables and exclude
    functionally-defined vars; treat them as "the model's output variables", not
    "every decision variable".
    """

    method: SolveMethod
    required_parameters: dict[str, InterfaceType]
    output_variables: dict[str, InterfaceType]
    has_output_item: bool
    globals: list[str] = Field(default_factory=list)
    included_files: list[str] = Field(default_factory=list)


class ModelInspectionResult(BaseModel):
    """Outcome of inspecting a MiniZinc model's interface (no solving).

    ``status="ok"`` means only that the interface was *extracted* — it is NOT a
    data-completeness signal. A no-data inspection is ``ok`` with a non-empty
    ``required_parameters`` (the whole point of the tool); completeness is
    signalled solely by ``interface.required_parameters == {}``. ``interface`` is
    populated only when ``status == "ok"``.
    """

    status: InspectStatus
    solver: str
    interface: ModelInterface | None = None
    # Output-cap overrun flag; same contract as `CheckResult.truncated`.
    truncated: bool = False
    stdout: str
    stderr: str
    elapsed_ms: int
    diagnostic: Diagnostic | None = None


class UnsatCoreConstraint(BaseModel):
    line: int | None = None
    column: int | None = None
    end_line: int | None = None
    end_column: int | None = None
    source: str


UnsatCoreStatus = Literal["mus_found", "no_core", "error", "timeout"]


class UnsatCoreResult(BaseModel):
    """Outcome of a findMUS (org.minizinc.findmus) run.

    `core` reports *a* minimal unsatisfiable subset (MUS): constraints that
    are jointly unsatisfiable and from which none can be removed without
    losing unsatisfiability. "Minimal" does NOT mean globally smallest — a
    model may have several MUSes of differing sizes and findMUS returns one.
    `stdout` holds findMUS's raw output and is authoritative; `core` is a
    best-effort structured view and may be empty even when status is
    "mus_found". `no_core` means findMUS finished without reporting a MUS,
    NOT that the model is satisfiable — a tight `timeout_ms` can also surface
    as `no_core` (findMUS may stop at its own --time-limit with rc 0), as can
    an output-cap tree-kill with no MUS in the capped transcript (the kill's
    exit code is the server's artifact, so it is masked rather than reported
    as `error`).
    Clients branch on `status`; there is no derived `core_found` flag.
    `truncated` is the output-cap overrun flag (same contract as
    `CheckResult.truncated`); a truncated transcript may have lost MUS lines,
    so a `no_core`/`mus_found` verdict parsed from it may be incomplete —
    the diagnostic is `output_truncated`.
    """

    status: UnsatCoreStatus
    truncated: bool = False
    core: list[UnsatCoreConstraint] = Field(
        default_factory=list,
        description=(
            "Best-effort structured view of the minimal unsatisfiable subset "
            "(MUS) — minimal, not necessarily globally smallest. May be empty "
            'even when status is "mus_found"; stdout is the authoritative output.'
        ),
    )
    message: str
    stdout: str
    stderr: str
    elapsed_ms: int
    diagnostic: Diagnostic | None = None
