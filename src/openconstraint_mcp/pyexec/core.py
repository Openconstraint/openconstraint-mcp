"""Subprocess executor for OR-Tools CP-SAT Python scripts.

The server executes user/LLM-provided Python in a child process using the
server's own interpreter (``sys.executable``), which ships ``ortools``.

Security posture: timeout + output cap + process-tree kill is a **robustness**
boundary, not a security sandbox. No network blocking, AST filtering, or
syscall restriction is applied. This is a local-only tool; a cloud deployment
would require a real sandbox.

Output contract (executor ↔ script): the script must print, as its **last**
stdout block, a final JSON object (same-shaped intermediate blocks are allowed
during search — see the timeout note below):
    {"status": "<CpsatStatus value>", "objective": <number|null>, "solution": {...},
     "best_objective_bound": <number|null>}

``status``, ``objective``, and ``solution`` are REQUIRED and type-checked:
``status`` must be one of the script vocabulary below, ``objective`` a finite
int/float or ``null`` (a pure feasibility model still emits the key), and
``solution`` a JSON object (``{}`` is well-typed — emptiness is an acceptance
question, not an envelope one) whose every number is finite at ANY depth —
``NaN``/``Infinity`` nested inside a solution is rejected exactly like a
non-finite ``objective``. Extra keys (``stats``, a supplementary
``result_file``, …) are allowed and ignored. On a clean exit, a missing or
ill-typed required key yields ``status="error"`` with no incumbent and a
``child_process_error`` diagnostic naming the offending ``field``; on a timeout
the status stays ``"timeout"`` and the drop is reported through
``rejected_partial_field``/``rejected_partial_reason`` instead.

``best_objective_bound`` is optional (a script predating it is parsed as
``None``) and diagnostic only — it is OR-Tools' ``solver.best_objective_bound``,
not a proven objective, and is never consulted for acceptance or winner
selection. It is most useful on ``status="unknown"``, where ``objective`` is
``None`` but the solver may still have made bound progress.

FEASIBILITY-PROBLEM PITFALL: for a pure satisfaction model with no
``model.minimize``/``maximize`` call, ``model.has_objective()`` is ``False``,
and OR-Tools does NOT raise for that case — both ``solver.objective_value``
and ``solver.best_objective_bound`` silently return ``0.0``. A script that
omits the ``if model.has_objective() else None`` guard would report a
meaningless ``best_objective_bound: 0.0`` instead of ``null`` for a
feasibility problem. The executor cannot detect or correct this server-side
(it only parses whatever number the script prints) — the guard must live in
the script itself, which is why the canonical snippet and the
``cpsat_python_solution_workflow`` prompt both apply it.

The executor parses the last JSON object it finds in stdout and maps the
``status`` field to ``CpsatStatus``; any unrecognized value becomes ``"error"``
and is reported as an envelope violation of ``status``. Like every other
violation this yields NO incumbent: the block's ``solution``/``objective`` are
dropped rather than attached to a status the executor could not classify.

The child runs unbuffered (``python -u``), so a script MAY print byte-bounded
intermediate result blocks of the same shape during search from a
``CpSolverSolutionCallback``. On a clean exit the final block wins as usual; on
a timeout the executor recovers the last intermediate block's
``solution``/``objective``/``best_objective_bound`` (status stays ``"timeout"``
— a partial is unproven), and only when that block satisfies the same required
envelope shape — a malformed partial is not recovered as an incumbent. The drop
is not silent: the timeout diagnostic's ``details`` gain
``rejected_partial_field``/``rejected_partial_reason``, so a client still learns
what to repair without the category changing away from ``timeout_*``.

Canonical emit snippet (inlined in scripts, never imported from here):

    import json
    status_map = {
        "OPTIMAL": "optimal",
        "FEASIBLE": "feasible",
        "INFEASIBLE": "infeasible",
        "UNKNOWN": "unknown",
        "MODEL_INVALID": "error",
    }
    print(json.dumps({
        "status": status_map.get(solver.status_name(status), "error"),
        "objective": solver.objective_value if model.has_objective() else None,
        "solution": {v.name: solver.value(v) for v in variables},
        "best_objective_bound": solver.best_objective_bound if model.has_objective() else None,
    }))
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from subprocess import Popen
from typing import Any, cast

from ..schemas.cpsat import (
    CPSAT_MUTATION_NAMES,
    CpsatCheckerReport,
    CpsatCheckerTestReport,
    CpsatMutationOutcome,
    CpsatPythonCheckedResult,
    CpsatPythonResult,
    CpsatStatus,
)
from ..shared.childproc import ChildProcessTracker
from ..shared.childrun import (
    ChildExecutionResult,
    ChildSpawnError,
    execute_child,
    validate_timeout_ms,
)
from ..shared.job_errors import exception_summary
from ..shared.proc import process_tree_terminate_worst_case_ms
from ..shared.save_target import text_sha256
from .checker import checker_infrastructure_report, run_checker_file
from .diagnostics import (
    checked_result_diagnostic,
    cpsat_result_diagnostic,
    output_contract_diagnostic,
)
from .eligibility import diagnostic_incumbent_eligibility
from .env_vars import CPSAT_CONFIG_ENV_VAR, CPSAT_SEED_ENV_VAR
from .mutation import SolutionMutation, generate_mutations
from .script_path import validate_script_args, validate_script_path, write_python_source

DEFAULT_PYEXEC_TIMEOUT_MS: int = 30_000

# Ceiling for the checker self-test's projected worst-case wall clock (see
# `_resolve_checked_checker_timeout_ms`), which runs the checker sequentially
# against the baseline plus every mutation. That projection is TIGHT — the runs
# really are sequential, and children that time out realize it — so this number
# is close to the wall clock a client actually waits. Keep enough margin for
# typical MCP client timeouts. `run_cpsat_python_experiment` carries a higher
# nominal cap (`MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS` in experiment.py) because
# its projection is a loose upper bound, not because a longer real wait is
# acceptable there — both tools block one synchronous MCP call.
MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS: int = 120_000
_CPSAT_EXECUTOR_POLL_SLACK_MS: int = 250

# Floor for a checker timeout DERIVED from the self-test budget. The derived
# value shrinks as `script_timeout_ms` grows, and it feeds the BASELINE checker as well
# as the mutants — so an unfloored derivation would let an opt-in diagnostic
# starve the primary verdict, timing out a checker that would have been given
# the full `script_timeout_ms` had the caller not asked for the probe. Below this the
# call rejects instead, which sends the caller to a smaller `script_timeout_ms`.
# Well above interpreter startup, so it never fails a checker that would run.
_MIN_SELF_TEST_CHECKER_TIMEOUT_MS: int = 2_000

# OR-Tools CP-SAT's random_seed parameter is a signed int32. Reject values outside
# that range before they reach a child process.
CPSAT_RANDOM_SEED_MIN: int = -2_147_483_648
CPSAT_RANDOM_SEED_MAX: int = 2_147_483_647

VERIFIED_STATUSES: frozenset[CpsatStatus] = frozenset[CpsatStatus]({"optimal", "feasible"})

# Statuses a script may legitimately report. "timeout" is executor-determined, so a
# script claiming it is treated as a contract violation and normalized to "error".
# The tuple carries the order the violation message lists them in (solution-bearing
# first); the frozenset is derived from it so the two can never disagree.
_SCRIPT_STATUS_ORDER: tuple[str, ...] = ("optimal", "feasible", "infeasible", "unknown", "error")
_SCRIPT_STATUSES: frozenset[str] = frozenset(_SCRIPT_STATUS_ORDER)

# Bounds the offending status echoed back in the envelope-violation reason. The
# status comes from the child's stdout, capped only at MAX_OUTPUT_BYTES (1 MiB),
# and the reason is copied into BOTH the diagnostic's message and its `details`
# — so an unbounded echo would amplify one oversized string threefold and blow
# the `Diagnostic` contract's "compact details" rule. 40 chars covers every real
# status (the longest legal one is "infeasible") plus the case/typo mistakes a
# client needs to see to repair its emit block.
_STATUS_ECHO_MAX_CHARS: int = 40

# Bounds the offending key path echoed back with a nested-finiteness violation
# (see `_nonfinite_violation`). Same threefold-amplification argument as the
# status echo above, but a path is assembled from the child's OWN key names, so
# it grows with the payload instead of with a fixed vocabulary: a 412 KB
# solution nested under 200-char keys yields a 408 KB path. Elided in the
# MIDDLE, because the two ends are what locate the value — the root, and the
# offending leaf key.
_KEY_PATH_MAX_CHARS: int = 120

# Four mutation rows share a client's context. Keep their useful checker signal
# without letting parsed errors repeat most of the 1 MiB child-output allowance.
_MUTATION_ERRORS_MAX_BYTES: int = 8 * 1024


def validate_checker_timeout_ms(checker_timeout_ms: int | None) -> None:
    """Reject a non-positive explicit checker timeout.

    The one check shared by the inline-``checker`` tools and the path-based
    ``checker_path`` tool, which cannot supply a timeout without a checker (its
    checker is required) and so has nothing else in common with
    ``validate_checker_args``.
    """
    if checker_timeout_ms is not None and checker_timeout_ms <= 0:
        raise ValueError("checker_timeout_ms must be positive")


def validate_checker_args(
    *,
    checker: str | None,
    checker_timeout_ms: int | None,
    checker_path: Path | None = None,
) -> None:
    """Validate shared optional-checker arguments for CP-SAT Python tools.

    ``checker`` (inline source) and ``checker_path`` (an on-disk checker run in
    place) are MUTUALLY EXCLUSIVE — exactly one, or neither. Supplying both is
    rejected rather than resolved by a precedence rule, mirroring the
    experiment tool's ``source``/``script_path`` pairing. A checker of either
    form satisfies the "``checker_timeout_ms`` needs a checker" rule.
    """
    if checker is not None and checker_path is not None:
        raise ValueError(
            "checker and checker_path are mutually exclusive: supply at most one of them"
        )
    if checker_timeout_ms is not None and checker is None and checker_path is None:
        raise ValueError("checker_timeout_ms supplied without checker: no checker will run")
    validate_checker_timeout_ms(checker_timeout_ms)
    if checker is not None and not checker.strip():
        raise ValueError("checker must be non-empty after stripping whitespace")


def effective_checker_timeout_ms(
    *, checker_timeout_ms: int | None, default_script_timeout_ms: int
) -> int:
    """Return the checker timeout after applying the tool's default timeout fallback."""
    return checker_timeout_ms if checker_timeout_ms is not None else default_script_timeout_ms


def cpsat_child_timeout_overhead_ms() -> int:
    """Return conservative cleanup/poll overhead for one timed-out CP-SAT child."""
    return process_tree_terminate_worst_case_ms() + _CPSAT_EXECUTOR_POLL_SLACK_MS


def _resolve_checked_checker_timeout_ms(
    *, script_timeout_ms: int, checker_timeout_ms: int | None
) -> int:
    """Resolve a SELF-TESTING checker timeout within the synchronous ceiling.

    Enforced only for ``test_checker=True``, which is what turns one checker
    child into ``1 + len(CPSAT_MUTATION_NAMES)`` sequential ones. A plain
    checked run is two children and keeps its historical freedom to ask for the
    solve time the problem needs: capping the synchronous path is a separate
    decision that has not been taken. Self-testing is new and opt-in, so a
    ceiling on it strands no existing caller.

    An omitted ``checker_timeout_ms`` is DERIVED from what the ceiling leaves,
    down to ``_MIN_SELF_TEST_CHECKER_TIMEOUT_MS``; below that the call rejects
    rather than hand the baseline checker a budget too small to run in.
    """
    overhead_ms = cpsat_child_timeout_overhead_ms()
    checker_runs = 1 + len(CPSAT_MUTATION_NAMES)
    model_budget_ms = script_timeout_ms + overhead_ms
    fixed_budget_ms = model_budget_ms + checker_runs * overhead_ms
    no_fallback = "checker self-testing has no background-job equivalent"
    if checker_timeout_ms is None:
        max_checker_timeout_ms = (
            MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS - fixed_budget_ms
        ) // checker_runs
        if max_checker_timeout_ms < _MIN_SELF_TEST_CHECKER_TIMEOUT_MS:
            raise ValueError(
                f"fixed budget {fixed_budget_ms} ms (script_timeout_ms={script_timeout_ms} + "
                f"{checker_runs} checker runs x {overhead_ms} ms overhead) leaves only "
                f"{max_checker_timeout_ms} ms per checker child, under the "
                f"{_MIN_SELF_TEST_CHECKER_TIMEOUT_MS} ms floor "
                f"(MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS={MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS} ms; "
                f"reduce script_timeout_ms, or drop test_checker — {no_fallback})"
            )
        # The floor bounds the DERIVED cap, not the caller's own model timeout: a
        # deliberately short `script_timeout_ms` still yields a checker timeout that
        # matches it, since that caller asked for a fast run end to end.
        checker_timeout_ms = min(script_timeout_ms, max_checker_timeout_ms)
    checker_budget_ms = checker_timeout_ms + overhead_ms
    projected_ms = model_budget_ms + checker_runs * checker_budget_ms
    if projected_ms <= MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS:
        return checker_timeout_ms
    raise ValueError(
        f"projected budget {projected_ms} ms (script_timeout_ms={script_timeout_ms} + "
        f"checker_timeout_ms={checker_timeout_ms}, x{checker_runs} runs incl. overhead) "
        f"exceeds MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS={MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS} ms "
        f"(reduce script_timeout_ms and/or checker_timeout_ms, or drop test_checker "
        f"— {no_fallback})"
    )


def validate_cpsat_random_seed(seed: object, *, label: str = "seed") -> int:
    """Validate a seed for OR-Tools CP-SAT's ``random_seed`` parameter."""
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ValueError(
            f"{label} must be a non-bool integer in the CP-SAT random_seed range "
            f"{CPSAT_RANDOM_SEED_MIN}..{CPSAT_RANDOM_SEED_MAX}, got {seed!r}"
        )
    if not (CPSAT_RANDOM_SEED_MIN <= seed <= CPSAT_RANDOM_SEED_MAX):
        raise ValueError(
            f"{label} must be in the CP-SAT random_seed range "
            f"{CPSAT_RANDOM_SEED_MIN}..{CPSAT_RANDOM_SEED_MAX}, got {seed!r}"
        )
    return seed


def _canonical_json_dumps(value: dict[str, Any]) -> str:
    """Serialize value with sorted keys and no extra whitespace.

    The single definition of "canonical" for CP-SAT config hashing, so
    ``config_sha256`` and ``canonical_json_byte_length`` can never disagree
    about what they are hashing/measuring.
    """
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def canonical_json_byte_length(value: dict[str, Any]) -> int:
    """Return the byte length of value's canonical JSON encoding (for size bounds)."""
    return len(_canonical_json_dumps(value).encode("utf-8"))


def config_sha256(config: dict[str, Any] | None) -> str | None:
    """Return config's canonical hash, or ``None`` for the "no config" state.

    Sorted keys mean two dicts with the same keys in different insertion order
    hash identically. Shared by the experiment executor (execution-time config
    hash) and the save gate (save-time mismatch check) so the two can never
    drift apart.

    An empty dict (``{}``) and an omitted config (``None``) both mean "no
    config" — no temp file, no env var, and this returns ``None`` for both, so
    hashes and the save replay gate never have to distinguish ``{}`` from absent.
    """
    if not config:
        return None
    return text_sha256(_canonical_json_dumps(config))


def write_config_file(directory: Path, config: dict[str, Any]) -> Path:
    """Write config as JSON into directory and return the file path.

    The caller supplies directory — typically a per-attempt or per-save-run
    ``tempfile.TemporaryDirectory()`` — so the file's lifetime is scoped to that
    context and cleaned up on every exit path, including a timeout tree-kill.
    """
    path = directory / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def seed_config_env(*, seed: int | None, config_path: Path | None) -> dict[str, str | None]:
    """Build the child env overlay for an optional seed and/or config file path.

    Always returns both protocol keys — set to the requested value, or
    explicitly ``None`` when not requested. ``execute_child`` treats a ``None``
    value as "delete this key from the inherited environment", so an
    attempt/replay documented as seed=None/config=None actually clears any
    ``OPENCONSTRAINT_MCP_CPSAT_SEED``/``_CONFIG`` the *parent* (server) process
    happens to have inherited from its own launch environment, instead of
    silently letting a stale value leak into the child.
    """
    return {
        CPSAT_SEED_ENV_VAR: str(seed) if seed is not None else None,
        CPSAT_CONFIG_ENV_VAR: str(config_path) if config_path is not None else None,
    }


@contextmanager
def replay_env_scope(
    *, seed: int | None, config: dict[str, Any] | None
) -> Generator[dict[str, str | None]]:
    """Yield the child env overlay for a seed/config replay run.

    A non-empty ``config`` is written into a ``tempfile.TemporaryDirectory()``
    scoped to the block, torn down on every exit path (clean return, error, or a
    timeout tree-kill); an empty or absent ``config`` yields the seed-only
    overlay with no temp directory at all. ``{}`` and ``None`` are treated
    identically ("no config") — the same normalization ``config_sha256`` documents
    — so a caller does not need to pre-normalize ``{}`` to ``None`` before calling.
    """
    if config:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = write_config_file(Path(tmp_dir), config)
            yield seed_config_env(seed=seed, config_path=config_path)
    else:
        yield seed_config_env(seed=seed, config_path=None)


def normalize_objective(raw: object) -> float | int | None:
    """Accept only a finite real number; bool, non-numeric, and non-finite become None.

    ``int`` is always mathematically finite, so it is returned as-is — including
    values too large to fit a float, which would overflow ``math.isfinite``. The
    finiteness check applies only to ``float`` (rejecting ``nan``/``inf``).
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    return raw


def parse_last_json(text: str) -> dict[str, Any] | None:
    """Return the last top-level JSON object found in ``text``, or ``None``.

    Scans forward, decoding each top-level object with ``raw_decode`` so trailing
    output after the final JSON block (a stray log line, a late callback) does not
    defeat parsing, and so a nested object (e.g. ``solution``) inside the payload
    is never mistaken for the result. The last object that decodes wins.
    """
    decoder = json.JSONDecoder()
    found: dict[str, Any] | None = None
    index = text.find("{")
    while index >= 0:
        try:
            obj, end = decoder.raw_decode(text, index)
        except json.JSONDecodeError:
            index = text.find("{", index + 1)
            continue
        if isinstance(obj, dict):
            found = obj
        index = text.find("{", end)
    return found


def _elide_key_path(path: str) -> str:
    """Middle-elide a key path over ``_KEY_PATH_MAX_CHARS``, keeping both ends."""
    if len(path) <= _KEY_PATH_MAX_CHARS:
        return path
    keep = (_KEY_PATH_MAX_CHARS - 1) // 2
    return f"{path[:keep]}…{path[-keep:]}"


def _nonfinite_violation(solution: dict[str, Any]) -> tuple[str, str] | None:
    """Return the FIRST ``(key path, reason)`` non-finite float in ``solution``.

    Walks every ``dict`` value and ``list`` element at any depth: a
    nested ``NaN``/``Infinity`` is worse than a top-level one, because the
    consumers disagree about it. ``json.dumps`` writes a bare ``NaN`` into the
    checker payload and the saved artifact, while a strict MCP client's decoder
    rejects the response or silently nulls the value.

    Only ``float`` is checked — ``int`` is finite at any magnitude, and
    ``str``/``bool``/``None`` are legal decision values — so this deliberately
    does NOT reuse ``normalize_objective``, whose broader rejection of bool and
    non-numeric types would throw out valid solutions.

    The traversal is an explicit stack, not recursion: CPython's JSON decoder is
    bounded by its C stack rather than ``sys.getrecursionlimit()``, so every
    supported version accepts input that decodes fine and then blows a recursive
    Python walk with ``RecursionError`` (see
    ``test_envelope_violation_walks_nesting_deeper_than_the_recursion_limit``).

    Path steps are bracketed and JSON-quoted (``solution["tasks"][3]["start"]``)
    so a literal key containing ``.`` or ``[`` stays distinguishable from real
    nesting, and the finished path is length-capped by ``_elide_key_path``.
    """
    stack: list[tuple[str, object]] = [("solution", solution)]
    while stack:
        path, value = stack.pop()
        if isinstance(value, float) and not math.isfinite(value):
            return _elide_key_path(path), f"must be a finite number; got {value}"
        if isinstance(value, dict):
            children = [(f"{path}[{json.dumps(key)}]", item) for key, item in value.items()]
        elif isinstance(value, list):
            children = [(f"{path}[{index}]", item) for index, item in enumerate(value)]
        else:
            continue
        # Reversed, so `pop()` takes children left to right and the reported path
        # is the first offender in the client's OWN payload order — the one its
        # eye lands on when scanning for the value to repair.
        stack.extend(reversed(children))
    return None


def _envelope_violation(parsed: dict[str, Any]) -> tuple[str, str] | None:
    """Return the first ``(field, reason)`` stdout-envelope violation, or ``None``.

    The required-key/type gate for a parsed result block: ``status`` in the
    script vocabulary, ``objective`` a finite number or ``null``, ``solution`` a
    JSON object carrying no non-finite float at any depth (see
    ``_nonfinite_violation``). Extra keys are ignored, and an empty ``solution``
    (``{}``) is WELL-TYPED here — "solution-bearing status but nothing in it" is
    an acceptance rule owned by ``cpsat_result_diagnostic`` and the
    save/experiment gates, not an envelope type rule. One site for the rules so
    the clean-exit and timeout (partial-recovery) paths can never drift.
    """
    if "status" not in parsed:
        return "status", "required key is missing"
    raw_status = parsed["status"]
    if not isinstance(raw_status, str):
        return "status", f"must be a string, got {type(raw_status).__name__}"
    if raw_status not in _SCRIPT_STATUSES:
        echo = raw_status[:_STATUS_ECHO_MAX_CHARS]
        elided = "(truncated)" if len(raw_status) > _STATUS_ECHO_MAX_CHARS else ""
        return "status", (f"must be one of {', '.join(_SCRIPT_STATUS_ORDER)}; got {echo!r}{elided}")
    if "objective" not in parsed:
        return "objective", "required key is missing"
    objective = parsed["objective"]
    if objective is not None and normalize_objective(objective) is None:
        return "objective", "must be a finite number or null"
    if "solution" not in parsed:
        return "solution", "required key is missing"
    if not isinstance(parsed["solution"], dict):
        return "solution", f"must be a JSON object, got {type(parsed['solution']).__name__}"
    return _nonfinite_violation(parsed["solution"])


def _extract_solution_objective(
    parsed: dict[str, Any],
) -> tuple[dict[str, Any], float | int | None, float | int | None]:
    """Pull the solution dict, objective, and best_objective_bound out of a result block.

    Called only for a block that already cleared ``_envelope_violation`` — both
    call sites (clean exit, timeout partial recovery) gate on it — so
    ``solution`` is present and a dict, and ``objective`` is numeric-or-null;
    both are read straight through with no re-check. The optional
    ``best_objective_bound`` is diagnostic only and is NOT part of the envelope
    gate, so it keeps its permissive normalization here: a non-numeric or
    non-finite bound becomes ``None`` rather than failing the whole result.
    """
    solution: dict[str, Any] = parsed["solution"]
    objective: float | int | None = parsed["objective"]
    best_objective_bound = normalize_objective(parsed.get("best_objective_bound"))
    return solution, objective, best_objective_bound


def _result_from_child(child: ChildExecutionResult) -> CpsatPythonResult:
    """Parse a raw ``ChildExecutionResult`` into the CP-SAT result contract.

    This is the CP-SAT protocol layer: the generic ``execute_child`` knows
    nothing about ``status``/``objective``/``solution``, so the clean-exit,
    timeout, and truncation shapes are decided here in one place.

    An envelope violation is routed by whether the run TIMED OUT. On a clean
    exit it selects ``output_contract_diagnostic`` (the result is a contract
    error); on a timeout the status is executor-owned, so the violation is
    passed as ``rejected_partial`` into the timeout diagnostic's ``details``
    instead — same category, but the dropped partial's field stays visible. The
    violation never reaches the public result model; the diagnostic is its whole
    client-visible surface.
    """
    result, violation = _classify_child_result(child)
    if violation is not None and not result.timed_out:
        field, reason = violation
        result.diagnostic = output_contract_diagnostic(
            field=field, reason=reason, return_code=result.return_code
        )
    else:
        result.diagnostic = cpsat_result_diagnostic(result, rejected_partial=violation)
    return result


def _spawn_failure_result(exc: OSError) -> CpsatPythonResult:
    """Turn a launch failure into the CP-SAT error contract instead of an ``OSError``.

    ``execute_child`` raises whatever ``Popen`` raises, and a spawn can fail for
    reasons no preflight can rule out: fd exhaustion (``EMFILE``), memory
    pressure (``ENOMEM``), or an argv that clears ``validate_script_args``'
    conservative bound but not the kernel's real one (``E2BIG``). Left
    unhandled, that aborts an experiment whose earlier attempts already ran.
    Background jobs deliberately preserve the exception so their registry
    reports the infrastructure failure as ``state="failed"``.

    Lives at the protocol layer rather than inside the shared executor so the
    MiniZinc runner's own launch wrapper (``MiniZincExecutionError``) keeps
    firing. ``return_code`` is ``None`` because no child ever existed to exit —
    the same contract the timeout branch uses, never a synthesized code.
    """
    result = CpsatPythonResult(
        status="error",
        solution=None,
        objective=None,
        stdout="",
        stderr=f"failed to start the Python child process: {exc}",
        return_code=None,
        timed_out=False,
        truncated=False,
        duration_ms=0,
    )
    result.diagnostic = cpsat_result_diagnostic(result)
    return result


def _error_result(child: ChildExecutionResult) -> CpsatPythonResult:
    """Build the no-incumbent error result shared by every clean-exit failure.

    Truncated output, an absent or unparseable result block, a nonzero exit, and
    an envelope violation all produce the same shape — ``status="error"`` with no
    solution or objective — and differ only in the diagnostic
    ``_result_from_child`` derives from them.
    """
    return CpsatPythonResult(
        status="error",
        solution=None,
        objective=None,
        stdout=child.stdout,
        stderr=child.stderr,
        return_code=child.return_code,
        timed_out=False,
        truncated=child.truncated,
        duration_ms=child.duration_ms,
    )


def _classify_child_result(
    child: ChildExecutionResult,
) -> tuple[CpsatPythonResult, tuple[str, str] | None]:
    """Classify a finished child into a result plus an optional envelope violation.

    The ``(field, reason)`` second element is PRIVATE to this module and
    ``_result_from_child``: it feeds the diagnostic and is never added to
    ``CpsatPythonResult``. It is NOT a "this run is a contract error" flag — on
    the timeout path it reports a dropped partial while the status stays the
    executor-owned ``"timeout"``.
    """
    if child.timed_out:
        # Recover the best-so-far from any intermediate result blocks (e.g. one per
        # improved solution from a CpSolverSolutionCallback); the last wins, and the
        # unbuffered child (-u) is what lets it survive the kill. Status stays the
        # executor-owned "timeout" — a partial is unproven, never "optimal". A
        # partial failing the envelope gate is dropped rather than recovered, but
        # its violation is still returned so the drop reaches the diagnostic.
        partial = parse_last_json(child.stdout)
        partial_violation = _envelope_violation(partial) if partial is not None else None
        recoverable = partial if partial is not None and partial_violation is None else None
        solution, objective, best_objective_bound = (
            _extract_solution_objective(recoverable)
            if recoverable is not None
            else (None, None, None)
        )
        return (
            CpsatPythonResult(
                status="timeout",
                solution=solution,
                objective=objective,
                best_objective_bound=best_objective_bound,
                stdout=child.stdout,
                stderr=child.stderr,
                # The child was killed; its exit code (SIGTERM -> -15 on POSIX) is not a
                # real return code. Report null by contract — matching the MiniZinc-path
                # tools — so clients don't misread a timeout as a child error.
                return_code=None,
                timed_out=True,
                truncated=child.truncated,
                duration_ms=child.duration_ms,
            ),
            partial_violation,
        )

    if child.truncated:
        return _error_result(child), None

    parsed = parse_last_json(child.stdout)
    if parsed is None or child.return_code != 0:
        return _error_result(child), None

    violation = _envelope_violation(parsed)
    if violation is not None:
        return _error_result(child), violation

    solution, objective, best_objective_bound = _extract_solution_objective(parsed)
    return (
        CpsatPythonResult(
            # The envelope gate above already proved membership in _SCRIPT_STATUSES.
            status=cast(CpsatStatus, parsed["status"]),
            solution=solution,
            objective=objective,
            best_objective_bound=best_objective_bound,
            stdout=child.stdout,
            stderr=child.stderr,
            return_code=child.return_code,
            timed_out=False,
            truncated=False,
            duration_ms=child.duration_ms,
        ),
        None,
    )


def _python_script_argv(script: Path, args: list[str] | None = None) -> list[str]:
    # -u: unbuffered child stdout/stderr so prints reach the capture files as
    # they happen (not on a full buffer). This is what lets a flushed
    # intermediate result block survive a timeout kill (see the partial
    # recovery on the timeout path above).
    # Anything in `args` trails the script path, so the child sees it as
    # `sys.argv[1:]`.
    return [sys.executable, "-u", str(script), *(args or ())]


def run_cpsat_python(
    source: str,
    *,
    script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
    spawn_failure_as_result: bool = True,
) -> CpsatPythonResult:
    """Execute OR-Tools CP-SAT Python ``source`` in a child process.

    Writes ``source`` to a temporary file, runs it with ``sys.executable``
    (the server's own venv, which ships ``ortools``), and captures stdout/stderr
    to bounded temp files sharing a combined ``MAX_OUTPUT_BYTES`` budget
    (stderr gets what stdout leaves). Returns a ``CpsatPythonResult`` with the
    parsed solution and execution metadata.

    Raises ``ValueError`` on a non-positive ``script_timeout_ms`` — matching the
    MiniZinc path's ``validate_model_and_timeout`` so a zero/negative cap is
    rejected up front rather than spawning a child only to kill it immediately.

    When a ``tracker`` is supplied (the server's per-run child tracker), the live
    child is registered for the duration of the run so an abrupt server teardown
    can terminate it instead of orphaning it. A reaped leader is unregistered;
    one that survives termination stays registered for teardown to retry.

    ``env`` is an INTERNAL environment overlay merged on top of the parent's
    environment for the child (callers are the experiment attempt runner and
    the seeded/configured save replay, which inject ``OPENCONSTRAINT_MCP_CPSAT_SEED``
    / ``_CONFIG``, or explicitly clear them via a ``None`` value — see
    ``seed_config_env``). It is NOT an MCP-facing parameter — the server never
    exposes arbitrary environment variables.

    ``spawn_failure_as_result`` is INTERNAL. Background jobs set it false so a
    child that never launches reaches their ``failed`` state; synchronous and
    experiment callers keep the structured error result.

    For an existing local file, use ``run_cpsat_python_file`` instead — it runs
    the script in its own directory so relative file/import references resolve.
    """
    validate_timeout_ms(script_timeout_ms, label="script_timeout_ms")
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        script = tmp / "script.py"
        write_python_source(script, source)
        # Run from the temp dir: an inline snippet has no sibling files to find.
        try:
            child = execute_child(
                _python_script_argv(script),
                cwd=tmp,
                timeout_ms=script_timeout_ms,
                tracker=tracker,
                on_start=on_start,
                env=env,
            )
        except ChildSpawnError as exc:
            if not spawn_failure_as_result:
                raise
            return _spawn_failure_result(exc)
        return _result_from_child(child)


def run_cpsat_python_file(
    script_path: Path,
    *,
    script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    args: list[str] | None = None,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
    spawn_failure_as_result: bool = True,
) -> CpsatPythonResult:
    """Execute an existing OR-Tools CP-SAT Python file in its own directory.

    The path-based counterpart to ``run_cpsat_python``: instead of pasting the
    full source, the caller passes a local script path. The script runs with
    ``cwd`` set to its parent directory, so a relative ``open()`` of a sibling
    data file or ``import`` of a helper module resolves — the iteration win over
    copying the whole file inline. Mirrors the MiniZinc file tools
    (``solve_model_path``), which likewise run from the model's directory so a
    relative ``include`` resolves.

    Validates the path (exists / regular file / non-empty / UTF-8) and ``args``
    (no embedded NUL, and a bounded total encoding — both of which ``Popen``
    rejects at spawn time) with a clear ``ValueError`` before any child is
    spawned. Same execution contract, output
    cap, timeout, tree-kill, and INTERNAL ``env`` overlay (see ``run_cpsat_python``)
    as ``run_cpsat_python``.

    ``args`` are appended after the script path, so the child reads them as
    ``sys.argv[1:]`` — for a script that takes its data file (or a flag) on the
    command line. Omitting it reproduces the plain ``python -u script.py``
    invocation exactly.
    """
    resolved = validate_script_path(script_path)
    validate_script_args(args)
    validate_timeout_ms(script_timeout_ms, label="script_timeout_ms")
    try:
        child = execute_child(
            _python_script_argv(resolved, args),
            cwd=resolved.parent,
            timeout_ms=script_timeout_ms,
            tracker=tracker,
            on_start=on_start,
            env=env,
        )
    except ChildSpawnError as exc:
        if not spawn_failure_as_result:
            raise
        return _spawn_failure_result(exc)
    return _result_from_child(child)


def _run_checker_or_report(
    checker_path: Path,
    run_result: CpsatPythonResult,
    *,
    problem: str | None,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
) -> CpsatCheckerReport:
    """Run the on-disk checker, turning an infrastructure fault into a report.

    The one call shape both the baseline check and every mutation-test mutant
    go through, so a temp-file/spawn failure can never void the completed model
    result on either path.
    """
    try:
        return run_checker_file(
            checker_path,
            run_result,
            problem=problem,
            timeout_ms=timeout_ms,
            tracker=tracker,
        )
    except Exception as exc:  # noqa: BLE001 - a checker fault must not void the run
        return checker_infrastructure_report(exc)


def _compact_mutation_errors(errors: list[str]) -> list[str]:
    """Cap a mutation row's errors to 8 KiB of compact JSON, including its marker."""

    def size(values: list[str]) -> int:
        return len(json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))

    if size(errors) <= _MUTATION_ERRORS_MAX_BYTES:
        return errors

    compact: list[str] = []
    for index, error in enumerate(errors):
        marker = f"... checker errors truncated ({len(errors) - index} affected)"
        if size([*compact, error, marker]) <= _MUTATION_ERRORS_MAX_BYTES:
            compact.append(error)
            continue

        low, high = 0, len(error)
        while low < high:
            middle = (low + high + 1) // 2
            if size([*compact, error[:middle], marker]) <= _MUTATION_ERRORS_MAX_BYTES:
                low = middle
            else:
                high = middle - 1
        if low:
            compact.append(error[:low])
        compact.append(marker)
        return compact
    return compact


def _run_checker_test(
    checker_path: Path,
    run_result: CpsatPythonResult,
    *,
    problem: str | None,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
) -> CpsatCheckerTestReport:
    """Re-run the checker against deterministic mutations of the solution.

    Each applied mutation costs exactly one more checker child, reusing the same
    checker path, ``problem``, effective timeout, and tracker as the baseline so
    the probe cannot differ from the verdict it is testing. The mutant payload is
    carried on a ``model_copy`` of the run result — the caller's result is never
    mutated — and only a ``rejected`` verdict counts: an ``error``/``timeout``
    mutant proves nothing, and a skipped mutation carries its reason instead.
    See ``CpsatCheckerTestReport`` for why zero rejections is inconclusive.

    Each mutant's full ``CpsatCheckerReport`` is projected down to a compact
    ``CpsatMutationOutcome`` (see that class for why) and its raw output
    discarded; the accepted baseline is not repeated here at all — the caller
    already returns that one report in full as ``checker``.

    A fault while building or recording one mutant becomes that row's
    ``skipped_reason``, so it cannot suppress the other mutation rows. A
    per-mutant checker infrastructure fault is absorbed more specifically by
    ``_run_checker_or_report`` and recorded as an ``error`` report.

    GENERATION gets its own boundary because it runs before any row exists.
    ``generate_mutations`` is stdlib over a pydantic-validated ``dict``, but its
    ``copy.deepcopy`` recurses in Python where the JSON decode that produced the
    solution did not, so a deeply nested payload can arrive intact and raise
    ``RecursionError`` there. This diagnostic is opt-in and its verdicts are
    inconclusive by design; it must never cost the caller a completed model
    result and an accepted baseline. A generation fault therefore degrades to
    the full fixed row set, every row skipped with the reason.
    """
    try:
        mutations = generate_mutations(run_result.solution, run_result.objective)
    except Exception as exc:  # noqa: BLE001 - a probe must not void the checked run
        reason = f"mutation generation failed: {exception_summary(exc)}"
        mutations = [
            SolutionMutation(name=name, skipped_reason=reason) for name in CPSAT_MUTATION_NAMES
        ]

    outcomes: list[CpsatMutationOutcome] = []
    for mutation in mutations:
        if not mutation.applied:
            outcomes.append(
                CpsatMutationOutcome(name=mutation.name, skipped_reason=mutation.skipped_reason)
            )
            continue
        try:
            mutant = run_result.model_copy(
                update={"solution": mutation.solution, "objective": mutation.objective}
            )
            report = _run_checker_or_report(
                checker_path,
                mutant,
                problem=problem,
                timeout_ms=timeout_ms,
                tracker=tracker,
            )
            outcomes.append(
                CpsatMutationOutcome(
                    name=mutation.name,
                    # Compact row, no diagnostic: see CpsatMutationOutcome for why.
                    status=report.status,
                    errors=_compact_mutation_errors(report.errors),
                    duration_ms=report.duration_ms,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one probe must not void the checked run
            outcomes.append(
                CpsatMutationOutcome(
                    name=mutation.name,
                    skipped_reason=f"mutation probe failed: {exception_summary(exc)}",
                )
            )

    return CpsatCheckerTestReport(mutations=outcomes)


def run_cpsat_python_file_checked(
    script_path: Path,
    checker_path: Path,
    *,
    problem: str | None = None,
    script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
    checker_timeout_ms: int | None = None,
    args: list[str] | None = None,
    test_checker: bool = False,
    tracker: ChildProcessTracker | None = None,
    env: dict[str, str | None] | None = None,
) -> CpsatPythonCheckedResult:
    """Run an on-disk CP-SAT script, then verify it with an on-disk checker.

    ``run_cpsat_python_file`` plus a mandatory verification pass, in one
    synchronous call. Both paths are resolved and validated (exists / regular
    file / non-empty / UTF-8) BEFORE any child is spawned, with a ``ValueError``
    naming the offending parameter; ``checker_timeout_ms`` must be positive when
    given and otherwise defaults to ``script_timeout_ms``. With ``test_checker`` on, an
    omitted checker timeout is capped at the largest value that fits the
    synchronous wall-clock budget.

    The model runs first, in its own directory (see ``run_cpsat_python_file``).
    The checker runs only against a checkable incumbent — the shared
    ``diagnostic_incumbent_eligibility`` gate — and otherwise is skipped with
    ``checker_skipped_reason`` set. Note ``timeout`` IS a checkable status: a
    timed-out run WITH a recovered incumbent is checked, one without is not.

    A checker that times out, exits nonzero, or emits malformed output does not
    fail this call — it yields a non-``accepted`` ``CpsatCheckerReport``. The
    model result always survives. The top-level ``diagnostic`` is composed by
    ``checked_result_diagnostic``; see that function for its precedence and for
    why a checker self-test never contributes one.

    ``test_checker`` (opt-in, default off) adds a checker self-test: after — and
    only after — an ``accepted`` baseline verdict, the same checker is re-run
    against deterministic mutations of the solution, and ``checker_test``
    reports whether at least one was rejected (see ``CpsatCheckerTestReport``
    for why zero rejections is inconclusive). Any other baseline leaves
    ``checker_test`` ``None``, since there is nothing to test the checker
    against. A fault while probing one mutation becomes that row's
    ``skipped_reason`` rather than escaping, so the other rows' verdicts stand.
    The probe never alters the run's own ``status``/``objective``/``solution``.

    Projected worst-case wall clock is additive:
    ``(script_timeout_ms + tree-kill grace) + (checker_timeout_ms + tree-kill grace)``,
    plus ``(applied mutations) × (checker_timeout_ms + tree-kill grace)`` when
    ``test_checker`` is on. Only the ``test_checker`` projection is GATED: with
    the self-test on, the call conservatively assumes all four mutations apply
    and rejects an explicit projection over ``MAX_CPSAT_SELF_TEST_WALL_CLOCK_MS``
    before any child runs. An omitted ``checker_timeout_ms`` is reduced to fit
    — the derived value is the BASELINE checker's budget too, so the call
    rejects instead of deriving one under the self-test floor.
    A plain checked run stays ungated — ``script_timeout_ms`` has no upper bound, since
    a caller must be able to ask for the solve time the problem needs. Background
    jobs have no checker self-test.
    """
    resolved_script = validate_script_path(script_path)
    resolved_checker = validate_script_path(checker_path, parameter="checker_path")
    validate_checker_timeout_ms(checker_timeout_ms)
    validate_timeout_ms(script_timeout_ms, label="script_timeout_ms")
    if test_checker:
        effective_checker_timeout = _resolve_checked_checker_timeout_ms(
            script_timeout_ms=script_timeout_ms, checker_timeout_ms=checker_timeout_ms
        )
    else:
        effective_checker_timeout = effective_checker_timeout_ms(
            checker_timeout_ms=checker_timeout_ms, default_script_timeout_ms=script_timeout_ms
        )

    run_result = run_cpsat_python_file(
        resolved_script,
        script_timeout_ms=script_timeout_ms,
        args=args,
        tracker=tracker,
        env=env,
    )

    eligible, skipped_reason = diagnostic_incumbent_eligibility(run_result)
    report: CpsatCheckerReport | None = None
    if eligible:
        report = _run_checker_or_report(
            resolved_checker,
            run_result,
            problem=problem,
            timeout_ms=effective_checker_timeout,
            tracker=tracker,
        )

    checker_test: CpsatCheckerTestReport | None = None
    if test_checker and report is not None and report.status == "accepted":
        checker_test = _run_checker_test(
            resolved_checker,
            run_result,
            problem=problem,
            timeout_ms=effective_checker_timeout,
            tracker=tracker,
        )

    return CpsatPythonCheckedResult(
        **run_result.model_dump(exclude={"diagnostic"}),
        diagnostic=checked_result_diagnostic(run_result, report),
        checker=report,
        checker_skipped_reason=skipped_reason,
        checker_timeout_ms=effective_checker_timeout,
        checker_test=checker_test,
    )
