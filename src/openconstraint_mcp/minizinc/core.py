from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from ..runtime import RuntimeMissingError, get_minizinc_binary, is_runtime_installed
from ..schemas.diagnostics import Diagnostic, UnsupportedFeatureError
from ..schemas.minizinc import (
    DEFAULT_SOLVE_CONTROLS,
    CheckerReport,
    CheckerStatus,
    CheckResult,
    CheckStatus,
    ModelInspectionResult,
    SaveVerifiedModelResult,
    SolutionCheck,
    SolveControls,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
    SolveStatus,
    UnsatCoreConstraint,
    UnsatCoreResult,
)
from ..schemas.portfolio import PortfolioSolveControls, PortfolioSolveResult
from ..shared.childproc import ChildProcessTracker
from ..shared.childrun import ChildExecutionResult, execute_child
from ..shared.save_target import text_sha256, validate_save_target
from .artifacts import write_verified_model_dir
from .checker import _parse_checker_stream
from .diagnostics import (
    check_diagnostic,
    inspection_diagnostic,
    solve_diagnostic,
    unsat_core_diagnostic,
)
from .files import read_text_utf8, validate_checker_path, validate_model_data_paths
from .interface import parse_model_interface
from .stream import _parse_solve_stream
from .unsat_core import _parse_unsat_core

DEFAULT_SOLVER: str = "cp-sat"
FINDMUS_SOLVER: str = "org.minizinc.findmus"
# Canonical solver ids whose `.msc` stdFlags include `-n` (verified against the
# managed MiniZinc 2.9.7 runtime). Canonical-only and default-deny: a short alias
# ("gecode") or any unlisted solver — including the default cp-sat — is rejected
# with an actionable error rather than building a doomed `-n` command.
# `org.gecode.gist` is excluded deliberately: it is Gecode's Gist GUI search
# tool, inappropriate for a headless local-first server, even though it lists `-n`.
NUM_SOLUTIONS_SOLVERS: frozenset[str] = frozenset({"org.gecode.gecode", "org.chuffed.chuffed"})
DEFAULT_SOLVE_TIMEOUT_MS: int = 30_000
# Named separately from the solve budget so the check call site doesn't read as
# a solve constant. A compile-check is far cheaper than a solve, but reusing the
# same value keeps the two tools' timeout semantics aligned.
DEFAULT_CHECK_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
# Named separately from the solve budget so the findMUS call site describes the
# diagnostic operation even though it uses the same default wall-clock budget.
DEFAULT_UNSAT_CORE_TIMEOUT_MS: int = DEFAULT_SOLVE_TIMEOUT_MS
# Inspection is a preflight like check — cheaper, even, since it stops after type
# analysis — so it shares the check budget rather than an independent literal that
# could drift. The distinct name keeps the inspect call site from reading as a check.
DEFAULT_INSPECT_TIMEOUT_MS: int = DEFAULT_CHECK_TIMEOUT_MS
# MiniZinc's read-only type-analysis flag: it reports the model's interface
# (required params, output vars, method) and stops without flattening or solving.
# It coexists with the --solver/--time-limit args _build_minizinc_cmd always
# injects, but takes none of the solve transport (--json-stream/--statistics) or
# search-control flags.
INSPECT_FLAG: str = "--model-interface-only"
_MODEL_FILENAME: str = "model.mzn"
_DATA_FILENAME: str = "data.dzn"
_CHECKER_FILENAME: str = "checker.mzc.mzn"
# Solve runs use the json-stream transport: `--json-stream` emits one JSON object
# per line (each solution carries both the human `default` text and the `json`
# variable values; status and statistics arrive as their own sibling objects),
# `--output-mode json` selects the json section, and `--output-objective` adds
# `_objective` to each solution so the best objective is recoverable. `--statistics`
# is what makes the `{"type":"statistics"}` objects appear at all. Solve-only:
# check (`-c`) and findMUS keep their plain invocation.
_SOLVE_STREAM_ARGS: tuple[str, ...] = (
    "--statistics",
    "--json-stream",
    "--output-mode",
    "json",
    "--output-objective",
)


class MiniZincExecutionError(RuntimeError):
    """Raised when the managed MiniZinc binary fails to produce a usable result."""


def _format_unsat_core_message(core: Sequence[UnsatCoreConstraint]) -> str:
    if core:
        return (
            "findMUS reported a minimal unsatisfiable subset; "
            f"{len(core)} constraint location(s) resolved from the submitted model."
        )
    return (
        "findMUS reported a minimal unsatisfiable subset, but no submitted-model "
        "constraint locations were resolved; see stdout."
    )


def _require_minizinc_binary() -> Path:
    """Return the managed MiniZinc binary, raising if the runtime is absent.

    The runtime-presence gate shared by ``list_solvers`` and both runners, so
    the user-facing "not installed" message lives in one place.
    """
    if not is_runtime_installed():
        raise RuntimeMissingError(
            "Managed MiniZinc runtime not found. "
            "Run `openconstraint-mcp install-runtime` to set it up."
        )
    return get_minizinc_binary()


def _solver_capabilities(entry: dict[str, Any]) -> SolverCapabilities:
    """Derive a solver's capability facts from one ``--solvers-json`` entry.

    The ``-a/-f/-p/-r`` booleans are honest membership reads of the solver's
    declared ``stdFlags``; ``supports_num_solutions`` is **not** — it follows the
    canonical ``solver_supports_num_solutions`` allowlist, so ``org.gecode.gist``
    stays excluded despite listing ``-n`` and an allowlisted solver stays
    supported even when its ``stdFlags`` is malformed.

    ``stdFlags`` is trusted only when it is a JSON array. An absent key, a
    ``null``, or a scalar string degrades to an empty ``std_flags`` (default-deny
    for the four derived booleans) rather than crashing on ``list(None)`` or
    splitting a string into characters — guarding against runtime-version shape
    drift.
    """
    raw = entry.get("stdFlags")
    std_flags = [str(flag) for flag in raw] if isinstance(raw, list) else []
    return SolverCapabilities(
        supports_all_solutions="-a" in std_flags,
        supports_free_search="-f" in std_flags,
        supports_parallel="-p" in std_flags,
        supports_random_seed="-r" in std_flags,
        supports_num_solutions=solver_supports_num_solutions(str(entry.get("id", ""))),
        std_flags=std_flags,
    )


def list_solvers() -> SolverList:
    binary = _require_minizinc_binary()
    try:
        completed = subprocess.run(
            [str(binary), "--solvers-json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
    except (subprocess.CalledProcessError, OSError) as exc:
        stderr = (getattr(exc, "stderr", None) or "").strip()
        detail = stderr or str(exc)
        raise MiniZincExecutionError(
            f"Managed MiniZinc binary at {binary} failed to list solvers: {detail}. "
            "The runtime may be corrupt — try reinstalling with "
            "`openconstraint-mcp install-runtime`."
        ) from exc
    raw: list[dict[str, Any]] = json.loads(completed.stdout)
    solvers: list[SolverInfo] = [
        SolverInfo(
            id=str(solver_id := entry.get("id", "")),
            name=str(entry.get("name", solver_id)),
            version=entry.get("version"),
            tags=list(entry.get("tags", [])),
            capabilities=_solver_capabilities(entry),
        )
        for entry in raw
    ]
    return SolverList(solvers=solvers)


class _RunOutcome(NamedTuple):
    timed_out: bool
    returncode: int  # meaningful only when timed_out is False
    stdout: str
    stderr: str
    elapsed_ms: int
    truncated: bool = False
    truncation_killed: bool = False  # cap branch requested termination; child last seen running


# Fixed line appended to a truncated run's stderr so the raw stderr text itself
# records the output-cap overrun, alongside the structured ``truncated`` field and
# ``output_truncated`` diagnostic every result schema carries.
_OUTPUT_CAP_STDERR_LINE: str = "output exceeded the 1 MiB cap; process stopped"

# Outer wall-clock grace (ms) added on top of MiniZinc's own ``--time-limit`` so
# the binary normally stops itself first and we capture its partial output; only a
# child that overruns this grace is tree-killed. Matches the pre-executor 5 s grace.
_MINIZINC_OUTPUT_GRACE_MS: int = 5000


def _execute_minizinc_child(
    cmd: Sequence[str],
    *,
    timeout_ms: int,
    cwd: str,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> ChildExecutionResult:
    """Run a fully-built MiniZinc ``cmd`` through the shared capped executor.

    The single launch point for both runners. The child is launched as a
    process-group leader with combined stdout+stderr capped at ``MAX_OUTPUT_BYTES``
    (1 MiB) and tree-killed on overrun — the executor's contract, now shared with
    CP-SAT. It gets the outer ``timeout_ms + _MINIZINC_OUTPUT_GRACE_MS`` wall-clock
    cap so MiniZinc's own ``--time-limit`` normally fires first and we keep its
    partial output. ``tracker`` registers the live child for server-teardown
    termination; ``on_start`` publishes the handle for targeted cancellation — the
    executor supports both lifecycles natively. A launch ``OSError`` (binary
    missing/not executable) is wrapped as ``MiniZincExecutionError`` keyed on
    ``cmd[0]``.
    """
    try:
        return execute_child(
            list(cmd),
            Path(cwd),
            timeout_ms=timeout_ms + _MINIZINC_OUTPUT_GRACE_MS,
            tracker=tracker,
            on_start=on_start,
        )
    except OSError as exc:
        raise MiniZincExecutionError(
            f"Managed MiniZinc binary at {cmd[0]} failed to execute: {exc}. "
            "The runtime may be corrupt — try reinstalling with "
            "`openconstraint-mcp install-runtime`."
        ) from exc


def _outcome_from_child(child: ChildExecutionResult) -> _RunOutcome:
    """Adapt a raw ``ChildExecutionResult`` into MiniZinc's ``_RunOutcome``.

    Preserves the pre-executor contract: a timeout yields ``timed_out=True`` with
    the ``-1`` returncode sentinel (never read while timed out); a normal run
    carries the child's real exit code. ``truncated`` rides along on both branches,
    and when set the fixed cap-notice line is appended to ``stderr`` so the raw
    stderr text records the overrun alongside the structured flag.
    """
    stderr = child.stderr
    if child.truncated:
        base = stderr if stderr == "" or stderr.endswith("\n") else stderr + "\n"
        stderr = base + _OUTPUT_CAP_STDERR_LINE + "\n"
    if child.timed_out:
        return _RunOutcome(
            timed_out=True,
            returncode=-1,  # sentinel: never read while timed_out is True
            stdout=child.stdout,
            stderr=stderr,
            elapsed_ms=child.duration_ms,
            truncated=child.truncated,
            truncation_killed=child.truncation_killed,
        )
    return _RunOutcome(
        timed_out=False,
        returncode=child.return_code if child.return_code is not None else -1,
        stdout=child.stdout,
        stderr=stderr,
        elapsed_ms=child.duration_ms,
        truncated=child.truncated,
        truncation_killed=child.truncation_killed,
    )


def _invoke_minizinc(
    cmd: Sequence[str],
    *,
    timeout_ms: int,
    cwd: str,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> _RunOutcome:
    """Run a fully-built MiniZinc ``cmd`` (blocking) and capture the raw outcome.

    Shared by the inline runner (``cwd`` is a private temp dir) and the path-based
    runner (``cwd`` is the model's own directory). Delegates launch, the output
    cap, and the wall-clock cap to the shared executor via ``_execute_minizinc_child``;
    while the child runs it is registered with ``tracker`` (the server's per-run
    child tracker) so an abrupt server teardown terminates this in-flight child
    instead of orphaning it. A reaped leader is unregistered; one that survives
    termination stays registered for teardown to retry. With no ``tracker`` the
    behaviour is identical, just untracked. ``on_start`` publishes the live process
    handle (e.g. for a job registry's targeted cancellation); the executor supports
    both lifecycles natively, so a caller may pass either, both, or neither.
    """
    return _outcome_from_child(
        _execute_minizinc_child(
            cmd, timeout_ms=timeout_ms, cwd=cwd, tracker=tracker, on_start=on_start
        )
    )


def _build_minizinc_cmd(
    binary: Path,
    *,
    solver: str,
    timeout_ms: int,
    model_arg: str,
    extra_args: Sequence[str] = (),
    data_args: Sequence[str] = (),
) -> list[str]:
    """Assemble the managed-binary argv shared by both runners.

    Centralizes MiniZinc's positional contract — the model file precedes any
    data file (``model.mzn data.dzn``), with ``--solver``/``--time-limit`` and
    the caller's ``extra_args`` ahead of both — so the two runners can't drift
    on argument order. ``extra_args`` and ``data_args`` default to empty, so a
    minimal (transport-only, dataless) command needs neither.
    """
    return [
        str(binary),
        "--solver",
        solver,
        "--time-limit",
        str(timeout_ms),
        *extra_args,
        model_arg,
        *data_args,
    ]


def validate_model_and_timeout(model: str, timeout_ms: int) -> None:
    """Reject an empty/whitespace model or non-positive timeout with a clear error.

    The inline-argument gate shared by ``_run_managed_minizinc`` and
    ``save_verified_model`` (which validates every argument before its first
    subprocess), so the two cannot drift.
    """
    if not model.strip():
        raise ValueError("model must not be empty")
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")


def _run_managed_minizinc(
    model: str,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data: str | None = None,
    checker: str | None = None,
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> _RunOutcome:
    """Run the managed MiniZinc binary on ``model`` and report the raw outcome.

    Shared by ``run_prepared_solve`` (the solve argv; a background job passes
    ``on_start`` only, leaving ``tracker`` untracked as before) and ``check_model``
    (``extra_args=("-c",)``). The model is
    written into a private temp dir and ``subprocess.run`` is pinned to that dir
    via ``cwd`` so cwd-relative ``include`` statements resolve onto emptiness
    rather than the server's working directory. With no data the model file is
    the last command argument.

    When ``data`` is not ``None`` it is written verbatim to a sibling
    ``data.dzn`` in the same temp dir and appended as a positional argument
    *after* the model file — MiniZinc's documented ``<model>.mzn <data>.dzn``
    order, which (unlike the ``--data`` flag) every solver path accepts,
    including the findMUS meta-solver. ``None`` means no data file and no extra
    argument — byte-identical to a dataless run. An empty string is a valid
    "no parameters" input, so it is *not* rejected.

    When ``checker`` is not ``None`` it is written beside the model as a
    ``.mzc.mzn`` file and added to ``extra_args`` before the command is built, so
    ``_build_minizinc_cmd`` remains the single place that orders flags before
    positional model/data arguments.
    """
    validate_model_and_timeout(model, timeout_ms)
    binary = _require_minizinc_binary()
    with tempfile.TemporaryDirectory(prefix="openconstraint-mcp-") as tmp:
        tmp_dir = Path(tmp)
        cmd = _build_inline_cmd(
            binary,
            tmp_dir,
            model=model,
            solver=solver,
            timeout_ms=timeout_ms,
            extra_args=extra_args,
            data=data,
            checker=checker,
        )
        return _invoke_minizinc(
            cmd, timeout_ms=timeout_ms, cwd=str(tmp_dir), tracker=tracker, on_start=on_start
        )


def _build_inline_cmd(
    binary: Path,
    tmp_dir: Path,
    *,
    model: str,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data: str | None,
    checker: str | None,
) -> list[str]:
    """Write the inline model/data/checker into ``tmp_dir`` and build the argv.

    The temp-dir file contract for ``_run_managed_minizinc``, shared by its
    tracked (``solve_model``/``check_model``) and cancellable (background-job
    ``run_prepared_solve``) callers: ``model.mzn`` always; ``data.dzn``
    when ``data`` is not ``None`` (appended positionally after the model, MiniZinc's
    ``<model>.mzn <data>.dzn`` order); ``checker.mzc.mzn`` when ``checker`` is given
    (added to ``extra_args`` via ``--solution-checker`` before the positional
    arguments). Kept in one place so callers can't drift on filenames, positioning,
    or the checker flag; argv assembly itself stays in ``_build_minizinc_cmd``.
    """
    model_file = tmp_dir / _MODEL_FILENAME
    model_file.write_text(model, encoding="utf-8")
    data_args: list[str] = []
    if data is not None:
        data_file = tmp_dir / _DATA_FILENAME
        data_file.write_text(data, encoding="utf-8")
        data_args = [str(data_file)]
    effective_extra_args = tuple(extra_args)
    if checker is not None:
        checker_file = tmp_dir / _CHECKER_FILENAME
        checker_file.write_text(checker, encoding="utf-8")
        effective_extra_args = (
            *effective_extra_args,
            "--solution-checker",
            str(checker_file),
        )
    return _build_minizinc_cmd(
        binary,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=effective_extra_args,
        model_arg=str(model_file),
        data_args=data_args,
    )


def _merge_stderr(real_stderr: str, messages: Sequence[str]) -> str:
    """Fold json-stream diagnostics into the process's ``stderr`` channel.

    ``--json-stream`` routes model/solver diagnostics into the *stdout* stream as
    ``error``/``warning`` objects, so the real process ``stderr`` is usually
    empty. To keep the "read stderr for diagnostics" contract working regardless
    of channel, the parsed messages are appended here — skipping any the process
    already wrote to ``stderr`` so a double-reported message is not duplicated.
    With no new messages the real ``stderr`` is returned unchanged.
    """
    extra = [m for m in messages if m and m not in real_stderr.splitlines()]
    if not extra:
        return real_stderr
    base = real_stderr if real_stderr == "" or real_stderr.endswith("\n") else real_stderr + "\n"
    return base + "\n".join(extra) + "\n"


def _resolve_status(
    stream_status: SolveStatus | None, *, has_solution: bool, returncode: int
) -> SolveStatus:
    # The stream's own verdict wins when it gave one. Otherwise, apply the
    # return-code fallback: a solution with a clean exit is "satisfied" (the
    # single-`satisfy` case, which emits no status object), a non-zero exit is
    # "error", and an empty clean run is "unknown".
    if stream_status is not None:
        return stream_status
    if has_solution:
        return "satisfied"
    if returncode != 0:
        return "error"
    return "unknown"


def _build_solve_result(outcome: _RunOutcome, *, solver: str) -> SolveResult:
    # Parse the json-stream transcript once; both branches reuse the structured
    # solutions/statistics and the reconstructed human stdout. Diagnostics found
    # in the stream are folded into stderr (the stream routes them off the real
    # stderr channel).
    parsed = _parse_solve_stream(outcome.stdout)
    solution = parsed.solutions[-1] if parsed.solutions else None
    stderr = _merge_stderr(outcome.stderr, parsed.messages)
    if outcome.timed_out:
        # The outer subprocess cap fired: keep whatever the partial stream gave,
        # but the verdict is "timeout" and there is no real return code (expose
        # None, not the internal -1 sentinel). A burst overrun can flag `truncated`
        # even here (the deadline is checked before the size cap), so it rides along.
        return SolveResult(
            status="timeout",
            solver=solver,
            return_code=None,
            timed_out=True,
            truncated=outcome.truncated,
            stdout=parsed.stdout,
            stderr=stderr,
            elapsed_ms=outcome.elapsed_ms,
            statistics=parsed.statistics,
            solution=solution,
            solutions=parsed.solutions,
            objective=parsed.objective,
        )
    # When the cap branch requested tree termination (the child was last seen
    # running), its exit code may be the executor's artifact rather than the
    # model's, so mask it: expose return_code=None (matching the timeout contract)
    # and force the status resolver's return-code input to 0 so the rc-derived
    # "error" fallback never wins — the stream's own verdict is used when present,
    # else "satisfied" if partial solutions survived, else "unknown". This keys off
    # ``truncation_killed``, NOT ``truncated``: a burst writer that overran the cap
    # but exited BEFORE the loop observed it carries its genuine exit code, and
    # masking that would turn a real rc=1 failure into "unknown". (The signal is not
    # proven to have caused the exit — see childrun's accepted residual race — but
    # masking a possibly-executor-owned code is the right policy for a truncated run.)
    effective_returncode = 0 if outcome.truncation_killed else outcome.returncode
    status = _resolve_status(
        parsed.status, has_solution=bool(parsed.solutions), returncode=effective_returncode
    )
    return SolveResult(
        status=status,
        solver=solver,
        return_code=None if outcome.truncation_killed else outcome.returncode,
        timed_out=False,
        truncated=outcome.truncated,
        stdout=parsed.stdout,
        stderr=stderr,
        elapsed_ms=outcome.elapsed_ms,
        statistics=parsed.statistics,
        solution=solution,
        solutions=parsed.solutions,
        objective=parsed.objective,
    )


def _build_check_result(outcome: _RunOutcome, *, solver: str) -> CheckResult:
    # A pure `-c` compile emits no status object, so the process return code is
    # the whole signal here — unlike solve, which classifies from the stream.
    # Deliberately NO `truncation_killed` masking (unlike solve/unsat-core):
    # masking would report `ok` for a compile the server killed mid-run — a false
    # success, and CheckResult gates `save_verified_model`. A killed check fails
    # closed as `error`; the `output_truncated` diagnostic names the real cause.
    status: CheckStatus = (
        "timeout" if outcome.timed_out else ("ok" if outcome.returncode == 0 else "error")
    )
    result = CheckResult(
        status=status,
        solver=solver,
        truncated=outcome.truncated,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )
    result.diagnostic = check_diagnostic(result)
    return result


def _build_inspection_result(outcome: _RunOutcome, *, solver: str) -> ModelInspectionResult:
    """Classify a ``--model-interface-only`` run into a ModelInspectionResult.

    Mirrors ``_build_check_result``'s rc-driven contract (including its
    deliberate lack of ``truncation_killed`` masking — a killed run fails closed
    as ``error`` rather than claiming an interface was extracted) — timeout ->
    ``timeout``, a non-zero exit -> ``error`` with the diagnostic on stderr and no
    parse — then
    on a clean exit parses the single interface object. An unparseable interface on
    rc 0 degrades to ``error`` (with the parse failure folded into stderr) rather
    than mis-reporting a partial interface. ``interface`` is populated only on ``ok``.
    """
    result = _classify_inspection_outcome(outcome, solver=solver)
    result.diagnostic = inspection_diagnostic(result)
    return result


def _classify_inspection_outcome(outcome: _RunOutcome, *, solver: str) -> ModelInspectionResult:
    """Classify the run into a ModelInspectionResult (diagnostic set by the caller)."""
    if outcome.timed_out:
        return ModelInspectionResult(
            status="timeout",
            solver=solver,
            interface=None,
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    if outcome.returncode != 0:
        return ModelInspectionResult(
            status="error",
            solver=solver,
            interface=None,
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )
    try:
        interface = parse_model_interface(outcome.stdout)
    except ValueError as exc:
        return ModelInspectionResult(
            status="error",
            solver=solver,
            interface=None,
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=_merge_stderr(outcome.stderr, [f"interface parse failed: {exc}"]),
            elapsed_ms=outcome.elapsed_ms,
        )
    return ModelInspectionResult(
        status="ok",
        solver=solver,
        interface=interface,
        truncated=outcome.truncated,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def _build_unsat_core_result(
    outcome: _RunOutcome, model: str, *, model_filename: str = _MODEL_FILENAME
) -> UnsatCoreResult:
    result = _classify_unsat_core_outcome(outcome, model, model_filename=model_filename)
    result.diagnostic = unsat_core_diagnostic(result)
    return result


def _classify_unsat_core_outcome(
    outcome: _RunOutcome, model: str, *, model_filename: str = _MODEL_FILENAME
) -> UnsatCoreResult:
    if outcome.timed_out:
        return UnsatCoreResult(
            status="timeout",
            core=[],
            message="findMUS timed out before reporting a result.",
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    mus_present, core = _parse_unsat_core(outcome.stdout, model, model_filename=model_filename)
    if mus_present:
        return UnsatCoreResult(
            status="mus_found",
            core=core,
            message=_format_unsat_core_message(core),
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    # A cap tree-kill's exit code is the executor's artifact, not findMUS's
    # verdict, so mask it (same policy as `_build_solve_result`): the run falls
    # through to `no_core` — "no MUS reported", which the schema already defines
    # as a no-verdict status — with the `output_truncated` diagnostic naming the
    # real cause. Only `error` from findMUS's OWN nonzero exit survives.
    if outcome.returncode != 0 and not outcome.truncation_killed:
        return UnsatCoreResult(
            status="error",
            core=[],
            message="findMUS did not complete successfully; see stderr.",
            truncated=outcome.truncated,
            stdout=outcome.stdout,
            stderr=outcome.stderr,
            elapsed_ms=outcome.elapsed_ms,
        )

    message = (
        "findMUS was stopped at the 1 MiB output cap before reporting a "
        "minimal unsatisfiable subset."
        if outcome.truncation_killed
        else "findMUS completed without reporting a minimal unsatisfiable subset."
    )
    return UnsatCoreResult(
        status="no_core",
        core=[],
        message=message,
        truncated=outcome.truncated,
        stdout=outcome.stdout,
        stderr=outcome.stderr,
        elapsed_ms=outcome.elapsed_ms,
    )


def _derive_checker_status(solve: SolveResult, checks: Sequence[SolutionCheck]) -> CheckerStatus:
    """Map a built ``SolveResult`` + parsed checks to the honest aggregate.

    First match wins, so a stream-reported solve error is never hidden behind
    ``no_solution``/``completed``: ``timeout`` (the subprocess cap fired); then
    ``error`` — the solve verdict is ``error`` (a stream ``ERROR`` maps there
    independently of return code), the return code is a nonzero int (broken /
    missing checker, wrong suffix), or the verdict count misaligns with the
    produced solutions (a solution went unchecked); then ``no_solution`` (a clean
    run produced nothing, so no checker ran and both counts are zero); then
    ``violation`` (any per-solution checker rejection — the one verdict the
    server asserts on its own); else ``completed`` (the checker ran for every
    produced solution with no machine-readable violation — NOT "all author-correct").
    """
    if solve.timed_out:
        return "timeout"
    if (
        solve.status == "error"
        or (isinstance(solve.return_code, int) and solve.return_code != 0)
        or len(checks) != len(solve.solutions)
    ):
        return "error"
    if not solve.solutions:
        return "no_solution"
    if any(check.violation for check in checks):
        return "violation"
    return "completed"


def _build_checker_report(outcome: _RunOutcome, solve: SolveResult) -> CheckerReport:
    """Compose the nested checker report for a solve using ``--solution-checker``."""
    checks = _parse_checker_stream(outcome.stdout)
    return CheckerReport(
        status=_derive_checker_status(solve, checks),
        checks=checks,
        transcript=outcome.stdout,
    )


def _attach_checker_and_diagnose(
    result: SolveResult, outcome: _RunOutcome, *, attach_checker: bool
) -> SolveResult:
    """Attach the nested checker report (when one ran) and set the solve diagnostic.

    The single tail every solve path shares: ``run_prepared_solve`` (inline and
    background-job solves) and the path-based solve each build a ``SolveResult``,
    optionally attach a ``--solution-checker`` report, then derive the structured
    diagnostic from the finished result (checker verdict included). Centralized so
    they cannot drift.
    """
    if attach_checker:
        result.checker = _build_checker_report(outcome, result)
    result.diagnostic = solve_diagnostic(result)
    return result


def find_unsat_core(
    model: str,
    *,
    data: str | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> UnsatCoreResult:
    outcome = _run_managed_minizinc(
        model,
        solver=FINDMUS_SOLVER,
        timeout_ms=timeout_ms,
        extra_args=(),
        data=data,
        tracker=tracker,
    )
    return _build_unsat_core_result(outcome, model)


def solver_supports_num_solutions(solver: str) -> bool:
    """Return whether ``solver`` accepts the ``-n`` (num-solutions) flag.

    A canonical-id allowlist, default-deny (see ``NUM_SOLUTIONS_SOLVERS``). The
    one invariant shared by the core gate and any future server-side re-run
    decision, so the supported set is defined in a single place.
    """
    return solver in NUM_SOLUTIONS_SOLVERS


def build_solve_extra_args(solver: str, controls: SolveControls) -> tuple[str, ...]:
    """Build the solve ``extra_args``: the json-stream transport plus any
    optional solver/search-control flags.

    Validates the valued numeric flags (``parallel`` must be ``>= 1``;
    ``num_solutions`` must be ``>= 1``). With every flag at its default the
    result is exactly ``_SOLVE_STREAM_ARGS``, so a default solve is
    byte-identical to the transport-only invocation. ``free_search`` -> ``-f``;
    ``parallel`` -> ``-p N``; ``random_seed`` -> ``-r N`` (any int);
    ``all_solutions`` -> ``-a``; ``num_solutions`` -> ``-n N``. Flags are
    appended after the transport args; MiniZinc is order-insensitive among them.

    ``num_solutions`` is solver-gated (satisfaction-only ``-n``): it is appended
    only for a solver in ``NUM_SOLUTIONS_SOLVERS``; for any other solver
    (including the default cp-sat) it raises a ``ValueError`` naming the
    supported solvers, so the doomed ``-n`` command is never built.
    """
    parallel: int | None = controls.parallel
    random_seed: int | None = controls.random_seed
    num_solutions: int | None = controls.num_solutions
    if parallel is not None and parallel < 1:
        raise ValueError("parallel must be >= 1")
    flags: list[str] = []
    if controls.free_search:
        flags.append("-f")
    if parallel is not None:
        flags += ["-p", str(parallel)]
    if random_seed is not None:
        flags += ["-r", str(random_seed)]
    if controls.all_solutions:
        flags.append("-a")
    if num_solutions is None:
        return *_SOLVE_STREAM_ARGS, *flags
    if num_solutions < 1:
        raise ValueError("num_solutions must be >= 1")
    if not solver_supports_num_solutions(solver):
        raise UnsupportedFeatureError(
            f"solver '{solver}' does not support num_solutions (the -n flag). "
            "Retry with solver='org.chuffed.chuffed' or solver='org.gecode.gecode'."
        )
    flags += ["-n", str(num_solutions)]

    return *_SOLVE_STREAM_ARGS, *flags


def validate_solver_capabilities(
    solver: str, capabilities: SolverCapabilities, controls: SolveControls
) -> None:
    """Reject a requested ``-a/-f/-p/-r`` control the resolved solver omits.

    Pure: it runs no subprocess — the caller passes an already-resolved
    ``capabilities``, so the same resolved map can validate one solver
    (single solve) or many (a portfolio plan). A requested control whose matching
    ``supports_*`` field is False raises a ``ValueError`` naming the solver, the
    MCP control, and the MiniZinc flag, plus the actionable fix. ``num_solutions``
    is deliberately NOT checked here — it keeps its canonical allowlist gate in
    ``build_solve_extra_args`` (``org.gecode.gist`` lists ``-n`` but is excluded), so it
    must not be folded into these stdFlags-derived booleans.
    """
    checks = (
        (controls.all_solutions, capabilities.supports_all_solutions, "all_solutions", "-a"),
        (controls.free_search, capabilities.supports_free_search, "free_search", "-f"),
        (controls.parallel is not None, capabilities.supports_parallel, "parallel", "-p"),
        (controls.random_seed is not None, capabilities.supports_random_seed, "random_seed", "-r"),
    )
    for requested, supported, control, flag in checks:
        if requested and not supported:
            raise UnsupportedFeatureError(
                f"solver '{solver}' does not support {control} (the {flag} flag). "
                "Call list_available_solvers to see each solver's capabilities, or "
                f"choose a solver whose {control} capability is supported."
            )


def resolve_capability_map() -> dict[str, SolverCapabilities]:
    """Resolve the runtime-local ``solver_id -> capabilities`` map (one ``list_solvers()``).

    A single ``--solvers-json`` subprocess; the result is not cached. Keyed by
    exact solver ``id`` so capability enforcement matches the canonical-id stance of
    the ``num_solutions`` gate. Callers resolve this once per entry point and reuse
    it (a portfolio resolves it once for the whole plan).
    """
    return {solver.id: solver.capabilities for solver in list_solvers().solvers}


def enforce_solver_capabilities(solver: str, controls: SolveControls) -> None:
    """Lazily resolve runtime capabilities and reject unsupported ``-a/-f/-p/-r``.

    No-op — and NO ``--solvers-json`` subprocess — when none of the four gated
    controls is requested, so a default solve stays byte-identical and pays no
    capability-lookup cost. When at least one is requested, resolves the map
    once and applies three cases: (a) the solver resolves to an entry that omits the flag ->
    raise; (b) it resolves and declares the flag -> pass; (c) the solver string
    does not resolve to any entry ``id`` (a short alias like ``gecode`` or an
    unknown solver) -> pass through untouched and let MiniZinc resolve it, exactly
    as today. Each entry point calls this once; the job worker trusts admission and
    never re-resolves.
    """
    if not (
        controls.free_search
        or controls.all_solutions
        or controls.parallel is not None
        or controls.random_seed is not None
    ):
        return
    capabilities = resolve_capability_map().get(solver)
    if capabilities is None:
        return
    validate_solver_capabilities(solver, capabilities, controls)


def prepare_solve_args(solver: str, controls: SolveControls) -> tuple[str, ...]:
    """Validate ``controls``, enforce runtime capabilities, and return the solve argv.

    The single owner of the solve prelude every capability-checking entry point
    shares. Pure validation and arg build first (a bad ``parallel``/``num_solutions``
    raises before any subprocess), THEN the lazy capability resolution (one
    ``--solvers-json`` only when a gated control is requested).
    """
    extra_args: tuple[str, ...] = build_solve_extra_args(solver, controls)
    enforce_solver_capabilities(solver, controls)
    return extra_args


def run_prepared_solve(
    model: str,
    *,
    solver: str,
    data: str | None,
    checker: str | None,
    timeout_ms: int,
    extra_args: Sequence[str],
    tracker: ChildProcessTracker | None = None,
    on_start: Callable[[subprocess.Popen[str]], None] | None = None,
) -> SolveResult:
    """Run a prepared (validated, capability-enforced) inline solve and build its result.

    The enforcement-free solve shared by ``solve_model``, the internal solve of
    ``save_verified_model``, and the background job worker: each entry point
    validates controls and enforces capabilities once up front (so the save path
    resolves capabilities at most once, and the worker never re-resolves),
    then calls this to run and parse. ``extra_args`` already carries the
    json-stream transport plus any control flags from ``build_solve_extra_args``.
    ``tracker`` registers the child for server teardown; ``on_start`` publishes
    the live handle so a job registry can terminate the process tree to cancel.
    A job's result is thus byte-for-byte what the synchronous tool returns.
    """
    outcome = _run_managed_minizinc(
        model,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        data=data,
        checker=checker,
        tracker=tracker,
        on_start=on_start,
    )
    result = _build_solve_result(outcome, solver=solver)
    return _attach_checker_and_diagnose(result, outcome, attach_checker=checker is not None)


def solve_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    checker: str | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    controls: SolveControls = DEFAULT_SOLVE_CONTROLS,
    tracker: ChildProcessTracker | None = None,
) -> SolveResult:
    extra_args = prepare_solve_args(solver, controls)
    return run_prepared_solve(
        model,
        solver=solver,
        data=data,
        checker=checker,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        tracker=tracker,
    )


def check_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> CheckResult:
    outcome = _run_managed_minizinc(
        model,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=("-c",),
        data=data,
        tracker=tracker,
    )
    return _build_check_result(outcome, solver=solver)


def inspect_model(
    model: str,
    *,
    solver: str = DEFAULT_SOLVER,
    data: str | None = None,
    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> ModelInspectionResult:
    """Inspect a MiniZinc model's interface WITHOUT solving it.

    Wraps the managed runtime's ``--model-interface-only`` flag: it reports which
    parameters the model still needs as data (``required_parameters``), its output
    variables, their types, and the solve method, stopping after type analysis —
    no flattening, no search. With no ``data`` the required set is the model's full
    parameter list; supplying the matching ``data`` shrinks it, so an empty
    ``required_parameters`` signals the data is complete. ``status="ok"`` means
    only that the interface was extracted — NOT that the data is complete.
    """
    outcome = _run_managed_minizinc(
        model,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=(INSPECT_FLAG,),
        data=data,
        tracker=tracker,
    )
    return _build_inspection_result(outcome, solver=solver)


def _save_gate_diagnostic(gate_diagnostic: Diagnostic | None) -> Diagnostic:
    """Surface the failing gate's own diagnostic on a ``not_verified`` save.

    The compile check or the solve already classified WHY it failed
    (``syntax_or_compile_error``, ``infeasible``, ``timeout_*``,
    ``checker_failed``, …) — that specific category is more actionable than a
    bare ``not_verified``, so it is reused when present. A gate with no
    diagnostic (a defensive fallback that should not occur, since a failed gate
    is always classified) degrades to a generic ``not_verified``.
    """
    if gate_diagnostic is not None:
        return gate_diagnostic
    return Diagnostic(
        category="not_verified",
        message="the save verification gate failed; nothing was written",
    )


def _verification_failure(solve: SolveResult, *, checker_supplied: bool) -> str | None:
    """Explain why a solve fails the save gate, or ``None`` when it verifies.

    The gate accepts only ``satisfied``/``optimal`` with no subprocess timeout,
    untruncated output, and a clean exit; when a checker was supplied, the
    nested report must be ``completed`` (the checker ran for every solution
    with no machine-readable violation — it does NOT prove optimality).
    Ordered most-specific-first: a timed-out run is named as such (its
    ``return_code`` is ``None``), then a truncated run (a cap-terminated one also
    has ``return_code`` ``None``, so the rc check alone would report a useless
    "exited with code None"), then the status verdict, then the exit code,
    then the checker verdict.
    """
    if solve.timed_out:
        return "Solve timed out"
    if solve.truncated:
        return "Solve output exceeded the 1 MiB cap and was truncated"
    if solve.status not in ("satisfied", "optimal"):
        return f"Solve did not verify (status: {solve.status})"
    if solve.return_code != 0:
        return f"MiniZinc exited with code {solve.return_code}"
    if checker_supplied and (solve.checker is None or solve.checker.status != "completed"):
        checker_status = "missing" if solve.checker is None else solve.checker.status
        return f"Solution checker did not complete (status: {checker_status})"
    return None


def _validate_portfolio_result_consistency(
    portfolio_result: PortfolioSolveResult,
    *,
    model: str,
    data: str | None,
    solver: str,
    controls: SolveControls,
) -> None:
    """Eagerly reject a ``portfolio_result`` that cannot describe this save request.

    This guards only against *accidental* mismatch (wrong model attached, a
    stale portfolio, the wrong solver/seed/search configuration) — it is not,
    and cannot be, a proof that ``portfolio_result`` is honest. A client could
    construct a self-consistent fake ``portfolio_result`` that passes every
    check here; that is acceptable because the save decision itself never reads
    ``portfolio_result`` — only the fresh ``check_model``/``run_prepared_solve`` below
    gates the save (see ``save_verified_model``'s docstring). ``checker_sha256``
    is deliberately not checked here: it is informational-only provenance for
    the eventual log, never a save gate. The race's shared
    ``solve_controls`` (``free_search``/``parallel``/``all_solutions``/
    ``num_solutions``) must match this save's, since they change what the
    solver searches — a mismatch means the save is not replaying the winning
    attempt's run; ``timeout_ms`` is deliberately not compared (a budget, not
    search configuration, and every log row already records its per-attempt
    ``timeout_ms``).

    ``winner_index`` is bounds-checked defensively against ``attempts`` before
    indexing into it — nothing in ``PortfolioSolveResult`` guarantees that
    invariant — so a
    malformed client-supplied result raises a clear ``ValueError`` here instead
    of an unhandled ``IndexError``.
    """
    if portfolio_result.status != "winner":
        raise ValueError(
            "portfolio_result.status must be 'winner' to attach an experiment log "
            f"(got {portfolio_result.status!r}); a no_winner portfolio has nothing "
            "to attach"
        )
    winner_index = portfolio_result.winner_index
    if winner_index is None or not (0 <= winner_index < len(portfolio_result.attempts)):
        raise ValueError(
            f"portfolio_result.winner_index ({winner_index!r}) is out of range for "
            f"{len(portfolio_result.attempts)} attempts"
        )
    winning_attempt = portfolio_result.attempts[winner_index]
    if winning_attempt.solver != solver:
        raise ValueError(
            "portfolio_result's winning attempt was solved with "
            f"{winning_attempt.solver!r}, which does not match the supplied "
            f"solver {solver!r}"
        )
    if winning_attempt.seed != controls.random_seed:
        raise ValueError(
            f"portfolio_result's winning attempt's seed ({winning_attempt.seed!r}) "
            f"does not match the supplied random_seed ({controls.random_seed!r})"
        )
    if portfolio_result.models_sha256[winning_attempt.model_index] != text_sha256(model):
        raise ValueError(
            "portfolio_result.models_sha256 does not match the sha256 of the "
            "supplied model: the portfolio_result was attached to a different model"
        )
    expected_data_sha256 = text_sha256(data) if data is not None else None
    if portfolio_result.data_sha256 != expected_data_sha256:
        raise ValueError(
            "portfolio_result.data_sha256 does not match the sha256 of the "
            "supplied data: the portfolio_result was attached to a different "
            "data instance"
        )
    race_controls: PortfolioSolveControls = portfolio_result.solve_controls
    # Declared field order (free_search, parallel, all_solutions, num_solutions)
    # fixes which mismatch is reported first.
    for name in PortfolioSolveControls.model_fields:
        race_value: bool | int | None = getattr(race_controls, name)
        save_value: bool | int | None = getattr(controls, name)
        if race_value != save_value:
            raise ValueError(
                f"portfolio_result.solve_controls.{name} ({race_value!r}) does not "
                f"match the supplied {name} ({save_value!r}): the save must replay "
                "the winning attempt's search configuration"
            )


def save_verified_model(
    model: str,
    *,
    target_dir: Path,
    data: str | None = None,
    checker: str | None = None,
    problem: str | None = None,
    solver: str = DEFAULT_SOLVER,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    controls: SolveControls = DEFAULT_SOLVE_CONTROLS,
    overwrite: bool = False,
    portfolio_result: PortfolioSolveResult | None = None,
    tracker: ChildProcessTracker | None = None,
) -> SaveVerifiedModelResult:
    """Re-verify an inline model through the managed runtime, then save it.

    Trusts no prior client-side claim of success: it re-runs ``check_model``
    (compile gate) and ``solve_model`` (success gate) on the artifacts as
    supplied, and writes the project directory only when the check is ``ok``,
    the solve verifies (see ``_verification_failure``), and — when a checker
    is supplied — the checker report is ``completed``. Every argument,
    including the marker-gated ``target_dir`` overwrite policy, is validated
    before the first subprocess runs; argument/path problems raise
    ``ValueError``, while a failed verification gate returns a normal
    ``status="not_verified"`` result. Nothing is ever written on a failed
    gate, and the commit itself is staged-then-swapped so a failure cannot
    leave a partial directory behind.

    ``portfolio_result`` is PROVENANCE ONLY, never verification evidence. It is
    validated eagerly for self-consistency with this request (winner status,
    matching solver/seed, matching model/data hash, matching shared solve
    controls — see ``_validate_portfolio_result_consistency``) but every save
    decision still
    comes from the fresh ``check``/``solve`` below: ``portfolio_result.winner``'s
    status, solution, and objective are never read by any gate. On a
    successful save, ``portfolio_result``'s attempt table is copied into
    ``experiment-log.json`` as a durable record of the exploration that led
    here; it is never written on a failed save.
    """
    validate_model_and_timeout(model, timeout_ms)
    # Validates the solve controls with the exact solve_model rules and rejects an
    # unsupported -a/-f/-p/-r control before check, solve, or write (one
    # --solvers-json at most for the whole save). The built args are kept so
    # the internal solve neither rebuilds them nor re-enforces.
    extra_args = prepare_solve_args(solver, controls)
    if portfolio_result is not None:
        _validate_portfolio_result_consistency(
            portfolio_result, model=model, data=data, solver=solver, controls=controls
        )
    target = validate_save_target(target_dir, overwrite=overwrite)

    check = check_model(model, solver=solver, data=data, timeout_ms=timeout_ms, tracker=tracker)
    if check.status != "ok":
        return SaveVerifiedModelResult(
            status="not_verified",
            message=(
                f"Model failed the compile check (status: {check.status}); nothing was written."
            ),
            target_dir=str(target),
            check=check,
            diagnostic=_save_gate_diagnostic(check.diagnostic),
        )

    solve = run_prepared_solve(
        model,
        solver=solver,
        data=data,
        checker=checker,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        tracker=tracker,
    )
    failure = _verification_failure(solve, checker_supplied=checker is not None)
    if failure is not None:
        return SaveVerifiedModelResult(
            status="not_verified",
            message=f"{failure}; nothing was written.",
            target_dir=str(target),
            check=check,
            solve=solve,
            diagnostic=_save_gate_diagnostic(solve.diagnostic),
        )

    files, warning = write_verified_model_dir(
        target,
        model=model,
        data=data,
        checker=checker,
        problem=problem,
        check=check,
        solve=solve,
        solve_controls={"timeout_ms": timeout_ms, **controls.model_dump()},
        overwrite=overwrite,
        portfolio_result=portfolio_result,
    )
    message = f"Verified model saved to {target}."
    if warning is not None:
        message = f"{message} Warning: {warning}"
    return SaveVerifiedModelResult(
        status="saved",
        message=message,
        target_dir=str(target),
        files=files,
        check=check,
        solve=solve,
    )


def _run_managed_minizinc_paths(
    model_path: Path,
    *,
    solver: str,
    timeout_ms: int,
    extra_args: Sequence[str],
    data_path: Path | None = None,
    tracker: ChildProcessTracker | None = None,
) -> _RunOutcome:
    """Run the managed binary on the real ``model_path`` with ``cwd`` = its parent.

    The path-based counterpart of ``_run_managed_minizinc``: instead of copying
    the model into a private temp dir, it runs the resolved real path so
    relative ``include`` statements resolve against the model's own directory,
    like normal MiniZinc CLI usage. Receives the already-resolved absolute paths
    from the public functions. The data file, when present, is appended
    positionally after the model (MiniZinc's ``model.mzn data.dzn`` order, which
    every solver path — including findMUS — accepts).
    """
    if timeout_ms <= 0:
        raise ValueError("timeout_ms must be positive")
    binary = _require_minizinc_binary()
    data_args = [str(data_path)] if data_path is not None else []
    cmd = _build_minizinc_cmd(
        binary,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        model_arg=str(model_path),
        data_args=data_args,
    )
    return _invoke_minizinc(cmd, timeout_ms=timeout_ms, cwd=str(model_path.parent), tracker=tracker)


def solve_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    checker_path: Path | None = None,
    timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
    controls: SolveControls = DEFAULT_SOLVE_CONTROLS,
    tracker: ChildProcessTracker | None = None,
) -> SolveResult:
    """Solve a MiniZinc model read from ``model_path`` via the managed runtime.

    Runs the managed binary on the real ``model_path`` with ``cwd`` = its
    parent, like normal MiniZinc CLI usage, so a relative ``include`` resolves
    against the model's own directory. Returns the inline tool's ``SolveResult``
    shape. The optional solver/search-control flags behave exactly as in
    ``solve_model`` (see ``build_solve_extra_args``). When ``checker_path`` is
    supplied, it is validated as a ``.mzc``/``.mzc.mzn`` checker and added to the
    same solve invocation; the returned ``SolveResult.checker`` then carries the
    per-solution checker report.
    """
    model_path, data_path = validate_model_data_paths(model_path, data_path)
    checker_path = validate_checker_path(checker_path) if checker_path is not None else None
    # Reject an unsupported -a/-f/-p/-r control before the solve, same lazy
    # one-shot resolution as the inline path.
    extra_args = prepare_solve_args(solver, controls)
    if checker_path is not None:
        extra_args = (*extra_args, "--solution-checker", str(checker_path))
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=extra_args,
        data_path=data_path,
        tracker=tracker,
    )
    result = _build_solve_result(outcome, solver=solver)
    return _attach_checker_and_diagnose(result, outcome, attach_checker=checker_path is not None)


def check_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> CheckResult:
    """Compile-check a MiniZinc model read from ``model_path`` via the runtime.

    Same CLI-style execution as ``solve_model_path`` (real path, ``cwd`` = the
    model's parent); returns the inline ``check_model`` ``CheckResult`` shape.

    ``-c`` (compile only) writes ``<model>.fzn``/``.ozn`` into the run's cwd —
    here the user's model directory — so both are redirected into a private temp
    dir (auto-deleted) to keep the compile check from littering the project. Only
    the diagnostics and return code matter for the check, never the artifacts.
    """
    model_path, data_path = validate_model_data_paths(model_path, data_path)
    with tempfile.TemporaryDirectory(prefix="openconstraint-mcp-") as tmp:
        outcome = _run_managed_minizinc_paths(
            model_path,
            solver=solver,
            timeout_ms=timeout_ms,
            extra_args=(
                "-c",
                "--output-fzn-to-file",
                str(Path(tmp) / "out.fzn"),
                "--output-ozn-to-file",
                str(Path(tmp) / "out.ozn"),
            ),
            data_path=data_path,
            tracker=tracker,
        )
    return _build_check_result(outcome, solver=solver)


def inspect_model_path(
    model_path: Path,
    *,
    solver: str = DEFAULT_SOLVER,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> ModelInspectionResult:
    """Inspect a MiniZinc model read from ``model_path`` via the managed runtime.

    Same CLI-style execution as ``solve_model_path`` (real path, ``cwd`` = the
    model's parent), so a relative ``include`` resolves against the model's own
    directory; returns the inline ``inspect_model`` ``ModelInspectionResult`` shape.
    """
    model_path, data_path = validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=solver,
        timeout_ms=timeout_ms,
        extra_args=(INSPECT_FLAG,),
        data_path=data_path,
        tracker=tracker,
    )
    return _build_inspection_result(outcome, solver=solver)


def find_unsat_core_path(
    model_path: Path,
    *,
    data_path: Path | None = None,
    timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    tracker: ChildProcessTracker | None = None,
) -> UnsatCoreResult:
    """Compute a MUS for a model read from ``model_path`` via the runtime.

    Same CLI-style execution as ``solve_model_path``. The model runs under its
    real basename, so the structured ``core`` is filtered by ``model_path.name``
    (best-effort, basename-only — see ``_iter_model_spans``); raw ``stdout``
    stays authoritative.
    """
    model_path, data_path = validate_model_data_paths(model_path, data_path)
    outcome = _run_managed_minizinc_paths(
        model_path,
        solver=FINDMUS_SOLVER,
        timeout_ms=timeout_ms,
        extra_args=(),
        data_path=data_path,
        tracker=tracker,
    )
    model = read_text_utf8(model_path)
    return _build_unsat_core_result(outcome, model, model_filename=model_path.name)
