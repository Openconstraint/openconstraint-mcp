"""CP-SAT Python explicit-experiment orchestrator.

Runs a client-supplied list of explicit attempts — each a complete, independent
CP-SAT Python script, optionally paired with a seed and/or a cooperative JSON
config — through the existing CP-SAT child runner and checker, and selects the
best accepted incumbent. This generalizes the (removed) seed sweep: instead of
one source run under N seeds, an experiment runs N independently-specified
attempts, each of which may vary source, seed, and/or config.

The server does not generate attempts, does not mutate OR-Tools objects, and
does not set solver parameters itself. It only writes a non-empty ``config`` to
a temp file and points ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` at it (and a seed via
``OPENCONSTRAINT_MCP_CPSAT_SEED``); a cooperating script decides how to apply
either.

Attempts run through a bounded ``ThreadPoolExecutor`` (``max_parallel_attempts``
workers, default 1 = serial); ``run_cpsat_python`` blocks on a subprocess wait,
so a worker thread spends nearly all its time off the GIL. Results are
assembled in original attempt order regardless of completion order, and winner
tie-breaks use that same original order — never completion order.

This is a synchronous, budget-gated tool, not a background job: a projected
worst-case wall-clock estimate is checked before any child runs (see
``_check_wall_clock_budget``), and the request is rejected outright when it
would exceed the budget.

Imports only the dependency-light leaves (``childproc``, ``proc``,
``save_target``, ``hashing``, ``schemas``, ``eligibility``, ``script_path``)
and the pyexec siblings ``core``/``checker``; never ``minizinc`` or
``runtime``.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import NamedTuple

from ..schemas.cpsat import (
    CpsatExperimentSelectionPolicy,
    CpsatObjectiveSense,
    CpsatPythonExperimentAttempt,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from ..shared.childproc import ChildProcessTracker
from ..shared.hashing import path_sha256
from ..shared.save_target import text_sha256
from .checker import run_checker
from .core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    canonical_json_byte_length,
    config_sha256,
    cpsat_child_timeout_overhead_ms,
    effective_checker_timeout_ms,
    replay_env_scope,
    run_cpsat_python,
    run_cpsat_python_file,
    validate_checker_args,
    validate_cpsat_random_seed,
)
from .diagnostics import experiment_attempt_diagnostic, experiment_diagnostic
from .eligibility import diagnostic_incumbent_eligibility
from .script_path import validate_script_args, validate_script_path

# Ceiling for this tool's projected worst-case wall-clock admission check (see
# `_check_wall_clock_budget`). Higher than `core.MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS`
# only because the two projections are not comparable: this one charges every
# attempt its full timeout plus overhead and then multiplies the SLOWEST attempt
# by the batch count, an upper bound parallelism can only undershoot, while the
# self-test's sequential projection is tight. Both tools block one synchronous
# MCP call, so it is the real wait a cap admits — not its nominal value — that
# has to stay inside typical MCP client timeouts.
MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS: int = 210_000

# A config dict is a small cooperative parameter bag, not a data payload — bound
# its canonical JSON encoding well under the 1 MiB child-output cap.
MAX_EXPERIMENT_CONFIG_BYTES: int = 64 * 1024

# Placeholder written into a suppressed winner's stdout when the caller opts
# out via include_winner_stdout=False. A fixed, recognizable sentinel (not an
# empty string) so a client can tell "the script printed nothing" apart from
# "the server omitted this by request".
_WINNER_STDOUT_OMITTED_SENTINEL: str = "<omitted: include_winner_stdout=False>"

# The hard ceiling on max_parallel_attempts, independent of any client-requested
# value: never oversubscribe the local machine by more than a handful of
# concurrent CP-SAT children (each of which may itself use multiple workers).
_MAX_PARALLEL_ATTEMPTS_CAP_LIMIT: int = 4

# Unconditional advisory attached to every winner: an experiment winner is one
# observed run, not a reproducibility guarantee. This is deliberately NOT
# gated on inspecting attempt.seed or config["num_workers"] — the server
# cannot see how (or whether) a script's source actually applies those, so a
# narrower "only warn when seed is unset" heuristic would be both brittle and
# falsely reassuring when it stays silent.
_REPRODUCIBILITY_WARNING: str = (
    "This winner reflects one observed run, not a reproducibility guarantee. "
    "CP-SAT's randomized search, LNS, restarts, parallel portfolio search "
    "(num_workers > 1), and short time limits can all produce a "
    "different objective on replay — save_verified_cpsat_python re-runs this "
    "script fresh and may find a worse (or better) result. For stronger "
    "reproducibility, set explicit solver parameters such as random_seed, "
    "consider num_workers = 1, and verify with the same timeout — but "
    "exact determinism is not guaranteed."
)

# Stronger status wins ties; lower rank is better.
_STATUS_RANK: dict[str, int] = {"optimal": 0, "feasible": 1, "timeout": 2}

_OPTIMIZATION_SELECTION_POLICY: CpsatExperimentSelectionPolicy = (
    "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
)
_FEASIBILITY_SELECTION_POLICY: CpsatExperimentSelectionPolicy = (
    "accepted_status_then_duration_then_attempt_order"
)


def _max_parallel_attempts_cap() -> int:
    return min(os.cpu_count() or 1, _MAX_PARALLEL_ATTEMPTS_CAP_LIMIT)


class _ResolvedScript(NamedTuple):
    """A ``script_path`` attempt's validated path plus the digest of its bytes.

    Both are captured in the upfront validation pass so the digest describes
    content that was provably readable at request time, and so a later change to
    the file on disk is detectable rather than silently mis-attributed.
    """

    path: Path
    sha256: str


def _script_invalidated_result(reason: str) -> CpsatPythonResult:
    """A per-attempt error result for a script that moved out from under the run.

    The upfront pass validated and hashed every ``script_path`` before anything
    ran, but a serial experiment can take minutes, during which an earlier
    attempt's own child or an external editor can rewrite or delete the file.
    Turning that into ONE failed attempt — rather than letting ``path_sha256``
    or ``validate_script_path`` raise out of ``pool.map`` — keeps the rows for
    attempts that already completed, the same guarantee the spawn-failure path
    provides.

    ``duration_ms`` is 0 and ``return_code`` ``None``: this result describes the
    invalidation, not a child's verdict. Any output the run did produce is
    deliberately dropped, because it cannot be attributed to known bytes.

    No ``diagnostic`` is set, unlike ``core._spawn_failure_result``. That one is
    handed to a client verbatim as a tool result, so it has to carry its own;
    this one only ever feeds ``_run_attempt``, whose row diagnostic
    ``experiment_attempt_diagnostic`` derives from the result itself. Setting it
    here would be redundant, not missing.
    """
    return CpsatPythonResult(
        status="error",
        solution=None,
        objective=None,
        stdout="",
        stderr=reason,
        return_code=None,
        timed_out=False,
        truncated=False,
        duration_ms=0,
    )


def _attempt_eligibility(
    result: CpsatPythonResult, objective_sense: CpsatObjectiveSense | None
) -> tuple[bool, str | None]:
    """Return ``(eligible, reject_reason)``; ``reject_reason`` is set iff not eligible.

    The status/solution gate is the shared ``eligibility`` leaf (also used by
    background jobs); the optimization-mode objective check layered on top is
    experiment-specific, so it stays here.
    """
    eligible, reject_reason = diagnostic_incumbent_eligibility(result)
    if not eligible:
        return False, reject_reason
    if objective_sense is None:
        return True, None
    objective = result.objective
    if objective is None or isinstance(objective, bool) or not math.isfinite(objective):
        return False, "objective is missing or non-numeric"
    return True, None


def _validate_objective_sense(objective_sense: object) -> CpsatObjectiveSense | None:
    if objective_sense is None:
        return None
    if objective_sense not in ("maximize", "minimize"):
        raise ValueError("objective_sense must be 'maximize', 'minimize', or None")
    return objective_sense


def _selection_policy(
    objective_sense: CpsatObjectiveSense | None,
) -> CpsatExperimentSelectionPolicy:
    if objective_sense is None:
        return _FEASIBILITY_SELECTION_POLICY
    return _OPTIMIZATION_SELECTION_POLICY


def _resolved_name(attempt: CpsatPythonExperimentAttempt, index: int) -> str:
    return attempt.name if attempt.name is not None else f"attempt-{index}"


def _validate_attempts(
    attempts: Sequence[CpsatPythonExperimentAttempt],
) -> tuple[list[str], list[_ResolvedScript | None]]:
    """Validate attempts and return resolved display names plus scripts, index-aligned.

    ``resolved_scripts[i]`` is the validated absolute ``Path`` plus its content
    digest for a ``script_path`` attempt, and ``None`` for an inline ``source``
    attempt — this is the one place a client-supplied ``script_path`` string
    becomes a ``Path``. Every attempt is validated here, before ANY attempt
    runs, so a bad ``script_path`` at index 2 rejects the whole request rather
    than only itself after indexes 0 and 1 have already spawned children.

    The digest is taken HERE, in the same pass that proved the file readable,
    so every attempt row has a truthful ``source_sha256`` even if the file is
    later edited or deleted mid-experiment. ``_run_attempt`` re-hashes after its
    run and fails that ONE attempt when the bytes moved, rather than reporting a
    digest for content that never executed.

    Raises ``ValueError`` for: an empty attempts list, an attempt that sets
    both or neither of ``source``/``script_path``, ``args`` without
    ``script_path``, an ``args`` entry containing a NUL character,
    an ``args`` list whose total encoding exceeds ``MAX_CHILD_ARGV_BYTES``, an
    empty/whitespace-only source, an unusable ``script_path`` (missing, not a
    file, empty, or non-UTF-8), an out-of-range seed, an oversized config, a
    non-positive ``script_timeout_ms``, or a name collision (explicit vs. explicit, or
    explicit vs. a defaulted ``attempt-{index}`` label).
    """
    if not attempts:
        raise ValueError("attempts must not be empty")

    names: list[str] = []
    resolved_paths: list[_ResolvedScript | None] = []
    seen: set[str] = set()
    for index, attempt in enumerate(attempts):
        name = _resolved_name(attempt, index)
        if name in seen:
            raise ValueError(f"duplicate attempt name (explicit or defaulted): {name!r}")
        seen.add(name)
        names.append(name)

        # Presence is `is not None`, never truthiness: an explicitly supplied
        # source="" alongside script_path is a both-set error (not "only
        # script_path"), and args=[] alongside source is an args-without-
        # script_path error (not "args omitted").
        if (attempt.source is None) == (attempt.script_path is None):
            raise ValueError(f"attempts[{index}] must set exactly one of source or script_path")
        if attempt.script_path is None:
            if attempt.args is not None:
                raise ValueError(
                    f"attempts[{index}].args supplied without script_path: args are only "
                    "passed to a script_path attempt, never to an inline source"
                )
            assert attempt.source is not None  # guaranteed by the exactly-one-of check above
            if not attempt.source.strip():
                raise ValueError(f"attempts[{index}].source must be non-empty")
            resolved_paths.append(None)
        else:
            # `validate_script_path` below rejects a NUL in `script_path` itself;
            # `args` needs its own check (NUL and total size) or `Popen` would
            # raise mid-run — as a raw OSError for the size case, after earlier
            # attempts' children had already executed.
            validate_script_args(attempt.args, parameter=f"attempts[{index}].args")
            resolved = validate_script_path(
                Path(attempt.script_path), parameter=f"attempts[{index}].script_path"
            )
            # Hash the raw bytes off disk: Path.read_text() would translate
            # CRLF/lone-CR to "\n" before hashing, which is exactly the
            # normalization text_sha256's exact-hash contract forbids.
            try:
                source_hash = path_sha256(resolved)
            except OSError as exc:
                raise ValueError(
                    f"attempts[{index}].script_path became unreadable during validation: "
                    f"{resolved} ({exc})"
                ) from exc
            resolved_paths.append(_ResolvedScript(resolved, source_hash))
        if attempt.seed is not None:
            validate_cpsat_random_seed(attempt.seed, label=f"attempts[{index}].seed")
        if attempt.config:
            size = canonical_json_byte_length(attempt.config)
            if size > MAX_EXPERIMENT_CONFIG_BYTES:
                raise ValueError(
                    f"attempts[{index}].config canonical JSON is {size} bytes, "
                    f"exceeding MAX_EXPERIMENT_CONFIG_BYTES={MAX_EXPERIMENT_CONFIG_BYTES}"
                )
        if attempt.script_timeout_ms is not None and attempt.script_timeout_ms <= 0:
            raise ValueError(f"attempts[{index}].script_timeout_ms must be positive")
    return names, resolved_paths


def _validate_max_parallel_attempts(max_parallel_attempts: object) -> int:
    if isinstance(max_parallel_attempts, bool) or not isinstance(max_parallel_attempts, int):
        raise ValueError("max_parallel_attempts must be a non-bool positive integer")
    if max_parallel_attempts < 1:
        raise ValueError("max_parallel_attempts must be >= 1")
    cap = _max_parallel_attempts_cap()
    if max_parallel_attempts > cap:
        raise ValueError(
            f"max_parallel_attempts={max_parallel_attempts} exceeds the server cap "
            f"{cap} (= min(os.cpu_count(), {_MAX_PARALLEL_ATTEMPTS_CAP_LIMIT}))"
        )
    return max_parallel_attempts


def _effective_script_timeout_ms(
    attempt: CpsatPythonExperimentAttempt, default_script_timeout_ms: int
) -> int:
    if attempt.script_timeout_ms is not None:
        return attempt.script_timeout_ms
    return default_script_timeout_ms


class _AttemptBudget(NamedTuple):
    """One attempt's admission-budget components, in ms.

    ``checker_timeout_ms``/``checker_budget_ms`` are ``None``/``0`` when the
    experiment has no checker — there is nothing to charge a checker budget for.
    """

    script_timeout_ms: int
    checker_timeout_ms: int | None
    attempt_budget_ms: int
    checker_budget_ms: int
    total_ms: int


def _attempt_budget_breakdown(
    attempt: CpsatPythonExperimentAttempt,
    *,
    default_script_timeout_ms: int,
    checker_present: bool,
    checker_timeout_ms: int | None,
) -> _AttemptBudget:
    """Break one attempt's projected worst-case wall-clock time into its components.

    Single source of truth for the admission-budget math, so the pass/fail gate
    (``_check_wall_clock_budget``) and its rejection message can never disagree
    about how a projected total was derived.
    """
    overhead = cpsat_child_timeout_overhead_ms()
    script_timeout_ms = _effective_script_timeout_ms(attempt, default_script_timeout_ms)
    attempt_budget_ms = script_timeout_ms + overhead
    effective_checker_ms: int | None = None
    checker_budget_ms = 0
    if checker_present:
        effective_checker_ms = effective_checker_timeout_ms(
            checker_timeout_ms=checker_timeout_ms, default_script_timeout_ms=script_timeout_ms
        )
        checker_budget_ms = effective_checker_ms + overhead
    return _AttemptBudget(
        script_timeout_ms=script_timeout_ms,
        checker_timeout_ms=effective_checker_ms,
        attempt_budget_ms=attempt_budget_ms,
        checker_budget_ms=checker_budget_ms,
        total_ms=attempt_budget_ms + checker_budget_ms,
    )


def _check_wall_clock_budget(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    *,
    default_script_timeout_ms: int,
    max_parallel_attempts: int,
    checker_present: bool,
    checker_timeout_ms: int | None,
) -> None:
    """Reject a projected over-budget request before any child runs.

    Batches attempts by ``max_parallel_attempts``: the projection is
    ``ceil(len(attempts) / max_parallel_attempts) * worst_case_of_the_slowest_attempt``,
    a conservative upper bound on wall-clock time for a bounded thread-pool
    schedule (parallelism can only reduce the true wall-clock time relative to
    this bound, never exceed it). On rejection, the error breaks the total down
    by the slowest attempt's own components so a caller can see whether the
    culprit is attempt count, per-attempt timeout, or the checker timeout —
    instead of only a single opaque "over budget" total. When batching (not a
    single attempt alone) is the culprit, the hint also names concrete
    single-lever fixes (a max attempt count, a min ``max_parallel_attempts``,
    or a max per-attempt total) derived from the same breakdown, so a caller
    does not have to invert the budget formula by hand.
    """
    breakdowns = [
        _attempt_budget_breakdown(
            attempt,
            default_script_timeout_ms=default_script_timeout_ms,
            checker_present=checker_present,
            checker_timeout_ms=checker_timeout_ms,
        )
        for attempt in attempts
    ]
    batches = math.ceil(len(attempts) / max_parallel_attempts)
    slowest = max(breakdowns, key=lambda b: b.total_ms)
    projected_ms = batches * slowest.total_ms
    if projected_ms <= MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS:
        return

    if slowest.total_ms > MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS:
        # Even a single run of the slowest attempt (batches=1) is already over
        # the cap: no attempt-count or max_parallel_attempts change can fit it.
        hint = (
            "the slowest attempt alone already exceeds the cap, so reducing "
            "attempt count or raising max_parallel_attempts cannot fit it — for "
            "a single attempt near or over this cap, use run_cpsat_python "
            "instead of run_cpsat_python_experiment"
        )
    else:
        # Each lever is solved holding the other two fixed, from the same
        # batches/slowest values the projection above already used:
        #   - batches_max: the most batches of the slowest attempt that fit
        #     the cap; every other bound follows from it.
        #   - max_attempts_to_fit: batches_max * max_parallel_attempts (the
        #     largest attempt count admitted at today's parallelism).
        #   - min_parallel_to_fit: ceil(attempt_count / batches_max) (the
        #     least parallelism that admits today's attempt count), flagged
        #     when it exceeds this machine's own parallelism cap.
        #   - max_slowest_total_ms: MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS //
        #     batches (today's batches, not batches_max) — the ceiling the
        #     slowest attempt's script_timeout_ms + overhead + checker budget must
        #     drop under at today's attempt count and parallelism.
        batches_max = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // slowest.total_ms
        max_attempts_to_fit = batches_max * max_parallel_attempts
        min_parallel_to_fit = math.ceil(len(attempts) / batches_max)
        max_slowest_total_ms = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // batches
        parallel_cap = _max_parallel_attempts_cap()
        parallel_note = (
            f" (exceeds this machine's max_parallel_attempts cap of {parallel_cap})"
            if min_parallel_to_fit > parallel_cap
            else ""
        )
        hint = (
            f"reduce attempt count to <= {max_attempts_to_fit}, or increase "
            f"max_parallel_attempts to >= {min_parallel_to_fit}{parallel_note}, or "
            "reduce the slowest attempt's script_timeout_ms + overhead + checker budget "
            f"to <= {max_slowest_total_ms} ms total"
        )
    raise ValueError(
        f"projected experiment budget {projected_ms} ms exceeds "
        f"MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS={MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS} ms. "
        f"Breakdown (slowest attempt): attempt_count={len(attempts)}, "
        f"max_parallel_attempts={max_parallel_attempts}, batches={batches}, "
        f"per_attempt_timeout_ms={slowest.script_timeout_ms}, "
        f"checker_timeout_ms={slowest.checker_timeout_ms}, "
        f"attempt_budget_ms={slowest.attempt_budget_ms}, "
        f"checker_budget_ms={slowest.checker_budget_ms}, "
        f"overhead_ms={slowest.attempt_budget_ms - slowest.script_timeout_ms}, "
        f"total_budget_ms={projected_ms}, "
        f"max_budget_ms={MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS} ({hint})"
    )


def _oversubscription_warning(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    names: Sequence[str],
    max_parallel_attempts: int,
) -> str | None:
    """Advisory-only: flag attempts whose config['num_workers'] combined with
    max_parallel_attempts may oversubscribe this machine's CPUs.

    This only observes the cooperative config["num_workers"] convention (see
    prompts.py's step 6 guidance) — a script that sets
    solver.parameters.num_workers any other way is invisible here. Never
    blocks the experiment; this is advisory, not a budget gate.
    """
    cpu_count = os.cpu_count() or 1
    offenders: list[tuple[str, int]] = []
    for name, attempt in zip(names, attempts, strict=True):
        num_workers = attempt.config.get("num_workers")
        if not isinstance(num_workers, int) or isinstance(num_workers, bool):
            continue
        if max_parallel_attempts * num_workers > cpu_count:
            offenders.append((name, num_workers))
    if not offenders:
        return None
    offenders_text = ", ".join(f"{name!r} (num_workers={n})" for name, n in offenders)
    max_workers = max(n for _, n in offenders)
    return (
        f"max_parallel_attempts={max_parallel_attempts} combined with attempt(s) "
        f"{offenders_text} may request up to {max_parallel_attempts * max_workers} "
        f"CP-SAT workers, exceeding this machine's cpu_count={cpu_count}; consider "
        "lowering num_workers or max_parallel_attempts."
    )


# Bounds the stderr tail folded into an errored attempt's ``message``, so a
# runaway traceback (or a script that dumps megabytes to stderr) can't blow up
# the attempt table.
_STDERR_SNIPPET_MAX_CHARS: int = 500

# Number of trailing non-blank stderr lines to keep. 2 covers the common case
# of a chained exception (a "During handling..." cause line followed by the
# final exception line) without the snippet growing past what a one-bullet
# attempt row should show.
_STDERR_SNIPPET_MAX_LINES: int = 2

# Bounds the raw stderr tail carried in structuredContent for a
# status="error" attempt — a much larger allowance than the one-line
# _STDERR_SNIPPET_MAX_CHARS used for the attempt table's `message`, so a
# client debugging a script exception can see the full traceback (not just
# its final line) without the printed table growing.
_ATTEMPT_STDERR_TAIL_MAX_CHARS: int = 4000


def _stderr_snippet(stderr: str) -> str | None:
    """Return the last couple of non-blank stderr lines, bounded, or ``None`` if empty.

    A Python traceback's most useful line — the exception type and message — is
    the last line printed, so tailing stderr surfaces it without parsing the
    traceback structure itself. Each line is truncated on its own (keeping its
    head, e.g. the exception type prefix) rather than tail-truncating the whole
    joined snippet, which could otherwise cut the prefix off a long final line.
    Lines are joined with " | " so the result stays single-line-safe for the
    plain-text, one-bullet-per-attempt formatter.
    """
    lines = [line for line in stderr.splitlines() if line.strip()]
    if not lines:
        return None
    tail = lines[-_STDERR_SNIPPET_MAX_LINES:]
    per_line_max = _STDERR_SNIPPET_MAX_CHARS // len(tail)
    truncated = [line if len(line) <= per_line_max else line[:per_line_max] for line in tail]
    return " | ".join(truncated)


def _run_attempt(
    index: int,
    attempt: CpsatPythonExperimentAttempt,
    name: str,
    *,
    resolved_path: _ResolvedScript | None,
    default_script_timeout_ms: int,
    objective_sense: CpsatObjectiveSense | None,
    checker: str | None,
    problem: str | None,
    checker_timeout_ms: int | None,
    tracker: ChildProcessTracker | None,
) -> tuple[CpsatPythonExperimentAttemptResult, CpsatPythonResult | None]:
    """Run one attempt end to end; return its result row and, if accepted, its raw result.

    ``resolved_path`` is the already-validated path AND content digest threaded
    from ``_validate_attempts`` — set for a ``script_path`` attempt, ``None`` for
    an inline ``source`` one. It is never re-derived from ``attempt.script_path``
    here, so the upfront pass stays the single place a path is resolved and the
    single place its digest is taken.

    The config temp file (when ``attempt.config`` is non-empty) lives in a
    ``replay_env_scope`` block scoped to exactly this call: it is created right
    before the child runs and removed right after the runner returns,
    whether the child exited cleanly, errored, or was tree-killed on timeout —
    no config temp file outlives its attempt.
    """
    script_timeout_ms = _effective_script_timeout_ms(attempt, default_script_timeout_ms)
    config_hash = config_sha256(attempt.config)

    with replay_env_scope(seed=attempt.seed, config=attempt.config) as env:
        if resolved_path is not None:
            # The digest comes from the upfront validation pass, so it is always
            # present and always describes bytes that were readable at request time.
            source_hash = resolved_path.sha256
            try:
                result = run_cpsat_python_file(
                    resolved_path.path,
                    script_timeout_ms=script_timeout_ms,
                    args=attempt.args,
                    tracker=tracker,
                    env=env,
                )
            except ValueError as exc:
                # The runner revalidates a path that was valid at request time.
                result = _script_invalidated_result(
                    f"{resolved_path.path} became unusable during the experiment: {exc}"
                )
            else:
                # Re-hash AFTER the run: if the file moved between validation and
                # now, `result` describes bytes `source_hash` does not, so the
                # attempt is invalidated instead of being reported with a digest
                # that never ran.
                try:
                    current_hash = path_sha256(resolved_path.path)
                except OSError as exc:
                    result = _script_invalidated_result(
                        f"{resolved_path.path} became unusable during the experiment: {exc}"
                    )
                else:
                    if current_hash != source_hash:
                        result = _script_invalidated_result(
                            f"{resolved_path.path} changed on disk during the experiment; "
                            "this attempt's result cannot be attributed to its recorded "
                            "source_sha256"
                        )
        else:
            assert attempt.source is not None  # guaranteed by _validate_attempts' exactly-one-of
            source_hash = text_sha256(attempt.source)
            result = run_cpsat_python(
                attempt.source, script_timeout_ms=script_timeout_ms, tracker=tracker, env=env
            )

    base_eligible, base_reject_reason = _attempt_eligibility(result, objective_sense)
    accepted = False
    checker_status = None
    message = base_reject_reason
    if not base_eligible and result.status == "error":
        snippet = _stderr_snippet(result.stderr)
        if snippet is not None:
            message = f"{base_reject_reason}: {snippet}"
    stderr_tail = (
        result.stderr[-_ATTEMPT_STDERR_TAIL_MAX_CHARS:]
        if result.status == "error" and result.stderr
        else None
    )
    if base_eligible and checker is not None:
        report = run_checker(
            checker=checker,
            run_result=result,
            problem=problem,
            timeout_ms=effective_checker_timeout_ms(
                checker_timeout_ms=checker_timeout_ms,
                default_script_timeout_ms=script_timeout_ms,
            ),
            tracker=tracker,
        )
        checker_status = report.status
        if report.status == "accepted":
            accepted = True
        else:
            message = f"checker {report.status}"
    elif base_eligible:
        accepted = True

    row = CpsatPythonExperimentAttemptResult(
        index=index,
        name=name,
        seed=attempt.seed,
        config_sha256=config_hash,
        source_sha256=source_hash,
        used_script_path=resolved_path is not None,
        script_timeout_ms=script_timeout_ms,
        status=result.status,
        objective=result.objective,
        best_objective_bound=result.best_objective_bound,
        accepted=accepted,
        checker_status=checker_status,
        message=message,
        timed_out=result.timed_out,
        truncated=result.truncated,
        duration_ms=result.duration_ms,
        stderr_tail=stderr_tail,
        diagnostic=experiment_attempt_diagnostic(
            result, accepted=accepted, checker_status=checker_status, message=message
        ),
    )
    return row, (result if accepted else None)


def _winner_sort_key(
    item: tuple[int, CpsatPythonResult], objective_sense: CpsatObjectiveSense | None
) -> tuple[float, int, int, int]:
    """Sort key for accepted candidates; the minimum is the winner (lower is better).

    In feasibility mode, status wins first. In optimization mode, base
    acceptance guarantees ``objective`` is a finite number, so negating it for
    ``maximize`` is safe. Ties break by stronger status, then faster
    ``duration_ms``, then earliest attempt order (the index component) — never
    completion order.
    """
    index, result = item
    if objective_sense is None:
        return 0.0, _STATUS_RANK[result.status], result.duration_ms, index
    objective = result.objective
    assert objective is not None  # guaranteed by optimization-mode acceptance
    objective_key = -objective if objective_sense == "maximize" else objective
    return objective_key, _STATUS_RANK[result.status], result.duration_ms, index


def run_cpsat_python_experiment(
    attempts: Sequence[CpsatPythonExperimentAttempt],
    *,
    objective_sense: CpsatObjectiveSense | None = None,
    default_script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    max_parallel_attempts: int = 1,
    problem: str | None = None,
    checker: str | None = None,
    checker_timeout_ms: int | None = None,
    include_winner_stdout: bool = True,
    tracker: ChildProcessTracker | None = None,
) -> CpsatPythonExperimentResult:
    """Run every explicit attempt and return the best accepted incumbent plus the table.

    Each attempt runs its own complete script (the server never diffs or
    merges attempts), supplied EXACTLY ONE of two ways: inline ``source``, or
    an on-disk ``script_path`` (plus optional ``args``) that runs from the
    script's own directory, as ``run_cpsat_python_file`` does. An attempt that
    ran from ``script_path`` is marked ``used_script_path=True`` in the
    attempt table and cannot serve as ``save_verified_cpsat_python``
    provenance, whose rerun is always inline with a fresh temp-dir ``cwd``.
    Attempts are optionally seeded via ``OPENCONSTRAINT_MCP_CPSAT_SEED`` and
    configured via ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` — both are cooperative
    protocols a script may ignore. Attempts run through a bounded thread pool
    (``max_parallel_attempts`` workers, default 1 = serial); results are
    assembled in original attempt order regardless of completion order.

    Acceptance is two ordered gates (short-circuiting like the save path): base
    acceptance (status ∈ {optimal, feasible, timeout}, non-empty solution, and
    in optimization mode only a finite numeric objective), then — only for
    base-eligible attempts — the optional checker gate (accepted iff the checker
    returns ``accepted``). The checker is never spent on an attempt that already
    failed base acceptance.

    In optimization mode (``objective_sense`` set), the winner is the accepted
    attempt with the best objective for ``objective_sense``, breaking ties by
    stronger status then earliest attempt order. In feasibility mode
    (``objective_sense`` omitted/``None``), the winner is the accepted attempt
    with the strongest status, then earliest attempt order. Returns a
    ``CpsatPythonExperimentResult`` with ``status="winner"`` and the winning
    ``CpsatPythonResult``/index/name, or ``status="no_winner"`` when nothing was
    accepted. A ``timeout`` winner is a reportable best incumbent, not a savable
    one (it fails the save reported gate). Whenever there is a winner,
    ``warnings`` always carries a reproducibility disclaimer — an experiment
    result is one observed run, not a guarantee that
    ``save_verified_cpsat_python`` will reproduce the same objective on
    replay — alongside any ``num_workers``-oversubscription advisory.

    Raises ``ValueError`` for an invalid request — including a projected budget
    over ``MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS`` or a ``max_parallel_attempts``
    over the server cap — before any child is spawned.
    """
    validated_objective_sense = _validate_objective_sense(objective_sense)
    validate_checker_args(checker=checker, checker_timeout_ms=checker_timeout_ms)
    if default_script_timeout_ms <= 0:
        raise ValueError("default_script_timeout_ms must be positive")
    validated_max_parallel = _validate_max_parallel_attempts(max_parallel_attempts)
    names, resolved_paths = _validate_attempts(attempts)
    oversubscription_warning = _oversubscription_warning(attempts, names, validated_max_parallel)

    _check_wall_clock_budget(
        attempts,
        default_script_timeout_ms=default_script_timeout_ms,
        max_parallel_attempts=validated_max_parallel,
        checker_present=checker is not None,
        checker_timeout_ms=checker_timeout_ms,
    )

    start = time.monotonic()

    def _run(
        item: tuple[int, CpsatPythonExperimentAttempt],
    ) -> tuple[CpsatPythonExperimentAttemptResult, CpsatPythonResult | None]:
        index, attempt = item
        return _run_attempt(
            index,
            attempt,
            names[index],
            resolved_path=resolved_paths[index],
            default_script_timeout_ms=default_script_timeout_ms,
            objective_sense=validated_objective_sense,
            checker=checker,
            problem=problem,
            checker_timeout_ms=checker_timeout_ms,
            tracker=tracker,
        )

    with ThreadPoolExecutor(max_workers=validated_max_parallel) as pool:
        # map() yields in input order regardless of completion order, so the
        # pool's own concurrency is never traded away for ordered results.
        results = list(pool.map(_run, enumerate(attempts)))

    attempt_rows = [row for row, _ in results]
    accepted = [(index, result) for index, (_, result) in enumerate(results) if result is not None]
    elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)
    winner = (
        min(accepted, key=lambda item: _winner_sort_key(item, validated_objective_sense))
        if accepted
        else None
    )

    source_sha256 = [row.source_sha256 for row in attempt_rows]
    checker_sha = text_sha256(checker) if checker is not None else None
    problem_sha = text_sha256(problem) if problem is not None else None

    winner_index = None
    winner_name = None
    winner_result = None
    if winner is not None:
        winner_index, winner_result = winner
        winner_name = names[winner_index]

    if not include_winner_stdout and winner_result is not None:
        winner_result = winner_result.model_copy(update={"stdout": _WINNER_STDOUT_OMITTED_SENTINEL})

    warnings = [oversubscription_warning] if oversubscription_warning else []
    if winner is not None:
        warnings.append(_REPRODUCIBILITY_WARNING)

    experiment_result = CpsatPythonExperimentResult(
        status="winner" if winner is not None else "no_winner",
        winner_index=winner_index,
        winner_name=winner_name,
        winner=winner_result,
        attempts=attempt_rows,
        elapsed_ms=elapsed_ms,
        objective_sense=validated_objective_sense,
        selection_policy=_selection_policy(validated_objective_sense),
        source_sha256=source_sha256,
        checker_sha256=checker_sha,
        problem_sha256=problem_sha,
        warnings=warnings,
    )
    experiment_result.diagnostic = experiment_diagnostic(experiment_result)
    return experiment_result
