from __future__ import annotations

import functools
import inspect
import os
import sys
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from importlib import metadata
from pathlib import Path
from typing import Annotated, Any, Literal, ParamSpec, TypeVar, cast

from anyio import to_thread
from mcp.server.mcpserver import Context, MCPServer
from mcp_types import CallToolResult, TextContent
from pydantic import BaseModel, JsonValue, StrictInt

from .jobs.portfolio_registry import PortfolioJobRegistry
from .jobs.registry import JobRegistry
from .minizinc.core import (
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_INSPECT_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    MiniZincExecutionError,
    check_model,
    check_model_path,
    find_unsat_core_path,
    inspect_model,
    inspect_model_path,
    list_solvers,
    save_verified_model,
    solve_model,
    solve_model_path,
)
from .minizinc.core import find_unsat_core as _find_unsat_core
from .protocol_text import status
from .protocol_text.descriptions import (
    AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION,
    CANCEL_PORTFOLIO_JOB_DESCRIPTION,
    CANCEL_SOLVE_JOB_DESCRIPTION,
    CHECK_MINIZINC_FILES_DESCRIPTION,
    CHECK_MINIZINC_MODEL_DESCRIPTION,
    CHECK_RUNTIME_DESCRIPTION,
    CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    FIND_UNSAT_CORE_DESCRIPTION,
    FIND_UNSAT_CORE_FILES_DESCRIPTION,
    GET_CPSAT_PYTHON_JOB_DESCRIPTION,
    GET_PORTFOLIO_JOB_DESCRIPTION,
    GET_SOLVE_JOB_DESCRIPTION,
    INSPECT_MINIZINC_FILES_DESCRIPTION,
    INSPECT_MINIZINC_MODEL_DESCRIPTION,
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    LIST_CPSAT_PYTHON_JOBS_DESCRIPTION,
    LIST_PORTFOLIO_JOBS_DESCRIPTION,
    LIST_SOLVE_JOBS_DESCRIPTION,
    LOAD_TABULAR_DATA_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS_CORE,
    MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    RUN_CPSAT_PYTHON_DESCRIPTION,
    RUN_CPSAT_PYTHON_DESCRIPTION_CORE,
    RUN_CPSAT_PYTHON_EXPERIMENT_DESCRIPTION,
    RUN_CPSAT_PYTHON_FILE_CHECKED_DESCRIPTION,
    RUN_CPSAT_PYTHON_FILE_DESCRIPTION,
    RUN_CPSAT_PYTHON_FILE_DESCRIPTION_CORE,
    SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION,
    SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION_CORE,
    SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION,
    SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION,
    SUBMIT_PORTFOLIO_JOB_DESCRIPTION,
    SUBMIT_SOLVE_JOB_DESCRIPTION,
    WRITE_TABULAR_RESULT_DESCRIPTION,
)
from .protocol_text.prompts import (
    AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT,
    MINIZINC_SOLUTION_WORKFLOW_PROMPT,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT,
    SOLVE_CONSTRAINT_PROBLEM_PROMPT_CORE,
    SOLVE_CPSAT_PYTHON_PROMPT,
)
from .protocol_text.results import (
    format_cpsat_experiment_content,
    format_save_result_content,
    format_solve_result_content,
    format_solver_list_content,
    format_tabular_data_content,
)
from .pyexec.core import (
    DEFAULT_PYEXEC_TIMEOUT_MS,
    replay_env_scope,
    run_cpsat_python,
    run_cpsat_python_file,
    run_cpsat_python_file_checked,
    seed_config_env,
    validate_cpsat_random_seed,
)
from .pyexec.experiment import run_cpsat_python_experiment
from .pyexec.jobs import CpsatJobRegistry
from .pyexec.save import save_verified_cpsat_python
from .runtime import RuntimeMissingError, get_runtime_status
from .schemas.cpsat import (
    CpsatExpectation,
    CpsatObjectiveSense,
    CpsatPythonCheckedResult,
    CpsatPythonExperimentAttempt,
    CpsatPythonExperimentResult,
    CpsatPythonJobStatus,
    CpsatPythonResult,
    SaveVerifiedPythonResult,
)
from .schemas.diagnostics import Diagnostic, InvalidSaveTargetError, UnsupportedFeatureError
from .schemas.minizinc import (
    CheckResult,
    ModelInspectionResult,
    SaveVerifiedModelResult,
    SolveJobStatus,
    SolveResult,
    SolverList,
    UnsatCoreResult,
)
from .schemas.portfolio import PortfolioJobStatus, PortfolioSolveResult
from .schemas.problem_text import ProblemText
from .schemas.runtime import RuntimeStatus
from .schemas.tabular import TabularCell, TabularData, TabularWriteResult
from .shared.childproc import ChildProcessTracker
from .shared.job_errors import JobRejectedError
from .shared.tabular_io import DEFAULT_MAX_ROWS, read_tabular_data, write_tabular_data

_PACKAGE_NAME = "openconstraint-mcp"

# The advertised MCP toolset. `full` (the internal default) registers every tool
# and all four prompts; `core` (the user-facing `stdio` default) advertises only
# eight core tools and the one backend-neutral `solve_constraint_problem`
# prompt, for a materially smaller `tools/list` payload and a less ambiguous
# default choice set.
Toolset = Literal["core", "full"]
_VALID_TOOLSETS: tuple[Toolset, ...] = ("core", "full")

# Every tool the core profile removes after registration. Static (not derived via
# `asyncio.run(mcp.list_tools())`, which raises inside the running event loop the
# tests build the server in). Two nets guard drift: `remove_tool()` raises
# `ToolError` on an unknown name, so a renamed/deleted tool breaks core
# construction loudly, and the exact-set tests catch a new tool leaking into
# core. MAINTENANCE RULE: a newly registered tool must be added here unless it is
# deliberately core.
_FULL_ONLY_TOOL_NAMES = frozenset(
    {
        "inspect_minizinc_model",
        "find_unsat_core",
        "save_verified_minizinc_model",
        "inspect_minizinc_files",
        "find_unsat_core_files",
        "submit_solve_job",
        "get_solve_job",
        "cancel_solve_job",
        "list_solve_jobs",
        "submit_portfolio_job",
        "get_portfolio_job",
        "cancel_portfolio_job",
        "list_portfolio_jobs",
        "submit_cpsat_python_job",
        "submit_cpsat_python_file_job",
        "get_cpsat_python_job",
        "cancel_cpsat_python_job",
        "list_cpsat_python_jobs",
        "run_cpsat_python_file_checked",
        "run_cpsat_python_experiment",
        "save_verified_cpsat_python",
        "load_tabular_data",
        "write_tabular_result",
    }
)


def _homepage_url() -> str | None:
    """Return the project ``Homepage`` URL from package metadata, or ``None``.

    The build populates ``Project-URL`` entries from ``pyproject.toml``'s
    ``[project.urls]``, each formatted ``"<label>, <url>"`` — so the value
    carries a leading space after the comma that must be stripped. Single
    source of truth: switching to a dedicated site is a ``pyproject.toml`` edit
    only. Returns ``None`` if the package metadata is absent (cosmetic field;
    must not crash boot).
    """
    try:
        entries: list[str] = metadata.metadata(_PACKAGE_NAME).get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    for entry in entries:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage":
            return url.strip()
    return None


def _server_version() -> str:
    """Return the installed package version, or ``"unknown"`` if unavailable."""
    try:
        return metadata.version(_PACKAGE_NAME)
    except metadata.PackageNotFoundError:
        return "unknown"


def _log_boot_diagnostic() -> None:
    """Print a one-time startup banner (version, runtime dir, install state).

    Writes to ``stderr`` only: over stdio, ``stdout`` is the JSON-RPC channel,
    so any stray write there corrupts the protocol. This only *reads* the
    already-resolved runtime status — it never downloads or installs anything.
    """
    runtime_status = get_runtime_status()
    lines = [
        f"{_PACKAGE_NAME} {_server_version()}",
        f"runtime dir: {runtime_status.runtime_dir}",
    ]
    if runtime_status.installed:
        lines.append(f"runtime: installed ({runtime_status.minizinc_binary})")
    else:
        lines.append(f"runtime: NOT installed → run `{_PACKAGE_NAME} install-runtime`")
    print("\n".join(lines), file=sys.stderr, flush=True)


def _wrap_result[ResultT: BaseModel](
    result: ResultT, formatter: Callable[[ResultT], str]
) -> CallToolResult:
    """Wrap a result model as formatter-rendered text content plus full structured output."""
    return CallToolResult(
        content=[TextContent(type="text", text=formatter(result))],
        structured_content=result.model_dump(mode="json"),
    )


# The exact message the SDK raises from Context.report_progress/Context.log
# when no real JSON-RPC request is active (direct white-box calls in tests,
# in-process invocation). _report_status swallows only this case.
_CONTEXT_UNAVAILABLE_MESSAGE = "Context is not available outside of a request"


async def _report_status(
    ctx: Context | None,
    progress: float,
    message: str,
    *,
    total: float | None = None,
) -> None:
    """Send one status milestone to the client on both feedback channels.

    Emits ``notifications/progress`` (delivered only when the request carried
    ``_meta.progressToken`` — i.e. only reaches a client that registered a
    progress callback for the call; the SDK no-ops otherwise) and an
    ``info``-level ``notifications/message`` log. The log is BEST-EFFORT, not a
    guaranteed fallback: it reaches a client that never registered a progress
    callback only on a handshake-era session (see the SEP-2577 note below).
    ``total`` is omitted by default on purpose: these are indeterminate stage
    counters, not a solver completion percentage, and reporting a total would
    invite a misleading percent-complete UI. Outside a real request the SDK
    raises ``ValueError(_CONTEXT_UNAVAILABLE_MESSAGE)``; only that exact case
    is swallowed — any other error from tool code still propagates.

    ``Context.info`` is deprecated (SEP-2577: the logging capability is
    deprecated as of protocol 2026-07-28) with no non-deprecated replacement for
    server-initiated status push. Kept rather than dropped: on a handshake-era
    session (<= 2025-11-25) the notification is still delivered unconditionally,
    so removing it would cost those clients their only activity feedback when
    they did not request progress. On a 2026-07-28 session it is DROPPED unless
    that request's ``_meta`` opted in at ``info`` level — see
    ``mcp.server.session.send_log_message``. Progress notifications are
    unaffected either way.

    Keeping it means accepting one stderr line per call site per process:
    ``MCPDeprecationWarning`` subclasses ``UserWarning``, so it warns at
    runtime, not just under a type checker. That runtime warning STAYS — every
    rung (``info`` → ``log`` → ``send_log_message``) is ``@deprecated``, so
    there is no quieter API to drop to, and suppressing it in-process is unsafe:
    two of the three warnings fire inside the coroutine, so a ``catch_warnings``
    block would have to mutate the process-global filter across an ``await``
    that concurrent tool calls share. Only the test gate is quieted, by a
    message-pinned ``filterwarnings`` entry in ``pyproject.toml`` — a static
    ini filter in a test-only process, not a mutation mid-flight — because the
    three rungs otherwise repeat one accepted decision 27 times per run.
    """
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
        await ctx.info(message)  # type: ignore[deprecated]
    except ValueError as exc:
        if str(exc) != _CONTEXT_UNAVAILABLE_MESSAGE:
            raise


async def _status_starting(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 1-2 immediately before the blocking solver/core call."""
    await _report_status(ctx, 1, stages[0])
    await _report_status(ctx, 2, stages[1])


async def _status_finished(ctx: Context | None, stages: tuple[str, str, str, str]) -> None:
    """Emit stages 3-4 once the blocking solver/core call has returned.

    Runs for every structured result — including ``status="error"`` /
    ``"timeout"`` / ``"unsatisfiable"`` — so clients always see a final
    milestone before the response. Domain exceptions raised by the core call
    skip this on purpose: obscuring the original error with a late
    notification is worse than ending the stream early.
    """
    await _report_status(ctx, 3, stages[2])
    await _report_status(ctx, 4, stages[3])


# fn is zero-arg, so run_sync's variadic *args (a TypeVarTuple) binds to the empty
# tuple; PyCharm mis-models that and reports a false "Parameter 'args' unfilled,
# expected '*tuple[]'". mypy passes — suppress both inspections that emit it.
# noinspection PyArgumentList,PyTypeChecker
async def _run_blocking[T](fn: Callable[[], T]) -> T:
    """Run a blocking solver/core call in a worker thread.

    A core call executed inline freezes the event loop for the whole solve,
    which deterministically strands the last queued status notification (the
    stdio writer task holds it but never gets scheduled) until the solve
    finishes — defeating mid-solve feedback for exactly the clients it exists
    for. Off-loop execution also keeps the server responsive to pings and
    concurrent requests during long solves. Core functions share no mutable
    state across calls — the MiniZinc path shells out, the CP-SAT path builds
    a fresh model/solver per call — so thread safety is not a concern.
    """
    return await to_thread.run_sync(fn)


async def _run_tool_with_status[T](
    ctx: Context | None,
    stages: tuple[str, str, str, str],
    blocking_call: Callable[[], T],
) -> T:
    """Run status start -> blocking call -> status finish -> result."""
    await _status_starting(ctx, stages)
    result = await _run_blocking(blocking_call)
    await _status_finished(ctx, stages)
    return result


_P = ParamSpec("_P")
_R = TypeVar("_R")

# The domain exceptions every solving/checking tool translates: the managed
# runtime is absent, the managed binary failed, or an argument was rejected.
_DEFAULT_MCP_ERROR_TYPES: tuple[type[Exception], ...] = (
    RuntimeMissingError,
    MiniZincExecutionError,
    ValueError,
)


def _classify_domain_error(exc: Exception) -> Diagnostic | None:
    """Map a pre-result domain exception to a structured ``Diagnostic``, or None.

    Stage 2 gives clients a stable branch point for the errors raised *before*
    any result model exists. Classification is entirely by exception type, not
    message content: ``RuntimeMissingError`` -> ``runtime_missing``,
    ``UnsupportedFeatureError`` -> ``unsupported_feature``,
    ``InvalidSaveTargetError`` -> ``invalid_save_target``, and every other
    ``ValueError`` (both are ``ValueError`` subclasses, so this check must come
    last) falls into the coarse ``invalid_request`` bucket — malformed paths,
    invalid arguments, the experiment wall-clock budget rejection, and the
    tabular write tools' plain file-exists overwrite refusal, which carries
    none of ``save_target.py``'s manifest-gated directory semantics and so
    deliberately stays a plain ``ValueError``, never ``InvalidSaveTargetError``.

    A prior version of this function classified by message-substring/prefix
    marker instead of type. That was fragile in a way type-checking is not:
    every one of these messages embeds caller-controlled text (a solver id, a
    filesystem path) ahead of the only fixed words in the message, so a path
    or solver id that coincidentally contained a marker — even an anchored
    prefix, for the messages where the interpolated text isn't first —
    misclassified. See ``UnsupportedFeatureError``/``InvalidSaveTargetError``
    in ``schemas.diagnostics`` for the full reasoning.

    Exceptions that are neither (``MiniZincExecutionError`` runtime corruption,
    ``JobRejectedError`` transient capacity) return None and pass through as a
    plain message, since they are not a client-repairable input state.
    """
    if isinstance(exc, RuntimeMissingError):
        return Diagnostic(category="runtime_missing", message=str(exc))
    if isinstance(exc, UnsupportedFeatureError):
        return Diagnostic(category="unsupported_feature", message=str(exc))
    if isinstance(exc, InvalidSaveTargetError):
        return Diagnostic(category="invalid_save_target", message=str(exc))
    if not isinstance(exc, ValueError):
        return None
    return Diagnostic(category="invalid_request", message=str(exc))


def _translated_error(exc: Exception) -> RuntimeError:
    """Build the ``RuntimeError`` an MCP tool raises for a domain exception.

    When the exception classifies to a ``Diagnostic``, the first line is the
    documented fallback ``Diagnostic: <category> — <summary>`` (the mcp SDK's
    tool-exception path surfaces only the message string, so the contract rides
    in that line), with any remaining original detail preserved on following
    lines. An unclassified exception is re-raised with its verbatim message, as
    before.
    """
    diagnostic = _classify_domain_error(exc)
    if diagnostic is None:
        return RuntimeError(str(exc))
    first_line, _, rest = diagnostic.message.partition("\n")
    text = f"Diagnostic: {diagnostic.category} — {first_line}"
    if rest:
        text = f"{text}\n{rest}"
    return RuntimeError(text)


def _as_mcp_error(
    *exc_types: type[Exception],
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Translate a tool's domain exceptions into a plain MCP ``RuntimeError``.

    The single home for the ``(domain exception -> RuntimeError)`` invariant: on
    any of ``exc_types`` (defaulting to the runtime/execution/value triad), the
    domain error is re-raised as a ``RuntimeError`` (original preserved as
    ``__cause__``). Pre-result errors clients need to branch on are prefixed with
    a structured ``Diagnostic: <category> — …`` first line (see
    ``_translated_error``); others keep their verbatim message. Pass a narrower
    tuple to catch less — ``list_available_solvers`` takes no user arguments, so
    it deliberately does not translate ``ValueError`` (a ``ValueError`` there is a
    real bug, not an actionable user message).

    ``functools.wraps`` preserves the wrapped tool's signature and annotations so
    MCPServer derives an unchanged schema from the decorated tool.
    """
    caught = exc_types or _DEFAULT_MCP_ERROR_TYPES

    def _decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        if inspect.iscoroutinefunction(fn):
            # Async tools raise their domain exceptions only when awaited, so
            # the translation must happen inside a coroutine wrapper — a sync
            # wrapper would return the coroutine from `try` without ever
            # entering `except`.
            coro_fn = cast("Callable[_P, Awaitable[Any]]", fn)

            @functools.wraps(fn)
            async def _async_wrapper(*args: _P.args, **kwargs: _P.kwargs) -> Any:
                try:
                    return await coro_fn(*args, **kwargs)
                except caught as exc:
                    raise _translated_error(exc) from exc

            return cast("Callable[_P, _R]", _async_wrapper)

        @functools.wraps(fn)
        def _wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            try:
                return fn(*args, **kwargs)
            except caught as exc:
                raise _translated_error(exc) from exc

        return _wrapper

    return _decorator


def _make_lifespan(
    registry: JobRegistry,
    cpsat_registry: CpsatJobRegistry,
    child_tracker: ChildProcessTracker,
) -> Callable[[MCPServer[Any]], AbstractAsyncContextManager[None]]:
    """Build the server lifespan bound to the registries and ``child_tracker``.

    Teardown terminates every in-flight child so none is orphaned:
    ``registry.shutdown()`` covers MiniZinc background jobs (and portfolio attempts),
    ``cpsat_registry.shutdown()`` covers CP-SAT background jobs, and
    ``child_tracker.terminate_all()`` covers the synchronous tools' children.
    All three are independently guarded so a failure in one does not skip the others.
    """

    @asynccontextmanager
    async def _lifespan(server: MCPServer[Any]) -> AsyncGenerator[None]:
        _log_boot_diagnostic()
        try:
            yield
        finally:
            try:
                registry.shutdown()
            finally:
                try:
                    cpsat_registry.shutdown()
                finally:
                    child_tracker.terminate_all()

    return _lifespan


def _env_int(name: str, default: int, *, minimum: int) -> int:
    """Read an integer registry bound from ``os.environ[name]`` and enforce ``minimum``.

    Returns ``default`` when the variable is unset. Otherwise parses the value and
    requires an integer ``>= minimum``, raising a ``ValueError`` that NAMES the
    offending variable — for both a non-integer and an out-of-range value — so a
    malformed bound fails fast at boot instead of silently falling back to the
    default or reaching ``JobRegistry``'s parameter-named constructor check (which
    says ``max_running_jobs must be >= 1``, not which env var was wrong). The
    constructor's own guards stay as a backstop.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer (got {raw!r})") from None
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum} (got {value})")
    return value


def _run_cpsat_python_file_with_replay(
    script_path: Path,
    *,
    script_timeout_ms: int,
    args: list[str] | None,
    seed: int | None,
    config: dict[str, Any] | None,
    tracker: ChildProcessTracker,
) -> CpsatPythonResult:
    """Run a CP-SAT Python file, always clearing/setting both protocol env vars.

    Tool-layer replay support for a saved seeded/configured artifact: builds
    the child env overlay via ``replay_env_scope`` so an absent ``seed``/
    ``config`` explicitly clears ``OPENCONSTRAINT_MCP_CPSAT_SEED``/``_CONFIG``
    instead of leaking a value the server process happened to inherit. A
    non-empty ``config`` is written to a temp file kept alive for the run and
    torn down on every exit path.
    """
    with replay_env_scope(seed=seed, config=config) as env:
        return run_cpsat_python_file(
            script_path, script_timeout_ms=script_timeout_ms, args=args, tracker=tracker, env=env
        )


def _run_cpsat_python_file_checked_with_replay(
    script_path: Path,
    checker_path: Path,
    *,
    problem: str | None,
    script_timeout_ms: int,
    checker_timeout_ms: int | None,
    args: list[str] | None,
    test_checker: bool,
    seed: int | None,
    config: dict[str, Any] | None,
    tracker: ChildProcessTracker,
) -> CpsatPythonCheckedResult:
    """Run a checked CP-SAT file pair with the same replay env handling as above.

    The env overlay applies to the MODEL child only — the checker is a
    verification step, not a replayed solve, so it never receives a seed or a
    config file.
    """
    with replay_env_scope(seed=seed, config=config) as env:
        return run_cpsat_python_file_checked(
            script_path,
            checker_path,
            problem=problem,
            script_timeout_ms=script_timeout_ms,
            checker_timeout_ms=checker_timeout_ms,
            args=args,
            test_checker=test_checker,
            tracker=tracker,
            env=env,
        )


def create_mcp_server(toolset: str = "full") -> MCPServer:
    """Build a fresh MCPServer for the given ``toolset``.

    ``full`` (the default, for internal callers and existing tests) registers
    every tool and all four prompts. ``core`` registers the same surface, then
    removes every tool in ``_FULL_ONLY_TOOL_NAMES`` and skips the three detailed
    backend-specific prompts, so the advertised ``tools/list`` payload is the
    eight core tools only and the advertised ``prompts/list`` payload is
    ``solve_constraint_problem`` only. The user-facing ``stdio`` default is
    ``core``, enforced by ``run_stdio`` and the CLI.
    An unknown value is rejected before any server object is built.
    """
    if toolset not in _VALID_TOOLSETS:
        valid = ", ".join(repr(t) for t in _VALID_TOOLSETS)
        raise ValueError(f"toolset must be one of {valid}; got {toolset!r}")
    is_core = toolset == "core"

    # Core hides the tools/prompts these three descriptions cross-reference, so
    # it advertises portfolio/prompt/save-free description variants; the server
    # instructions likewise name only the core tools. Full keeps every string
    # unchanged.
    instructions = MCP_SERVER_INSTRUCTIONS_CORE if is_core else MCP_SERVER_INSTRUCTIONS
    solve_minizinc_model_desc = (
        SOLVE_MINIZINC_MODEL_DESCRIPTION_CORE if is_core else SOLVE_MINIZINC_MODEL_DESCRIPTION
    )
    run_cpsat_python_desc = (
        RUN_CPSAT_PYTHON_DESCRIPTION_CORE if is_core else RUN_CPSAT_PYTHON_DESCRIPTION
    )
    run_cpsat_python_file_desc = (
        RUN_CPSAT_PYTHON_FILE_DESCRIPTION_CORE if is_core else RUN_CPSAT_PYTHON_FILE_DESCRIPTION
    )
    # The backend-neutral prompt is served by both profiles, so its spliced
    # CP-SAT output-contract fragment follows the same split: core exposes no
    # checker-capable CP-SAT tool, so its variant does not say the server runs
    # the checker you supply.
    solve_constraint_problem_prompt = (
        SOLVE_CONSTRAINT_PROBLEM_PROMPT_CORE if is_core else SOLVE_CONSTRAINT_PROBLEM_PROMPT
    )

    # The single server-owned job registry (D1.1): one instance per server,
    # captured by the job-tool closures and torn down by the lifespan. This is
    # the deliberate, bounded exception to "no global mutable state". The three
    # bounds default to today's values and are overridable via env vars (a
    # malformed value fails fast at boot, naming the variable).
    registry = JobRegistry(
        max_running_jobs=_env_int("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", 4, minimum=1),
        max_queued_jobs=_env_int("OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS", 16, minimum=0),
        max_retained_terminal=_env_int("OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL", 64, minimum=1),
    )
    # The server-owned background-portfolio registry: it drives the SAME `registry`
    # for attempts and selects a winner lazily on each poll, so it owns no worker
    # pool and cannot starve the attempt pool. Retention of finished portfolio
    # records is bounded; the dominant capacity bound is the solve registry's.
    portfolios = PortfolioJobRegistry(registry)
    # The server-owned CP-SAT background-job registry (the deliberate, bounded
    # exception to "no global mutable state", like `registry`): parallel to the
    # MiniZinc registry but for CP-SAT Python jobs. Bounds are independently
    # overridable via CP-SAT-prefixed env vars; defaults match the MiniZinc values.
    cpsat_registry = CpsatJobRegistry(
        max_running_jobs=_env_int("OPENCONSTRAINT_MCP_CPSAT_MAX_RUNNING_JOBS", 4, minimum=1),
        max_queued_jobs=_env_int("OPENCONSTRAINT_MCP_CPSAT_MAX_QUEUED_JOBS", 16, minimum=0),
        max_retained_terminal=_env_int(
            "OPENCONSTRAINT_MCP_CPSAT_MAX_RETAINED_TERMINAL", 64, minimum=1
        ),
    )
    # The server-owned tracker of in-flight SYNCHRONOUS-tool children (the
    # bounded "no global mutable state" exception, like `registry`): the sync
    # MiniZinc/CP-SAT tools register their child while it runs so the lifespan can
    # terminate it on teardown instead of orphaning it. Background-job children
    # are handled by `registry.shutdown()` / `cpsat_registry.shutdown()`.
    child_tracker = ChildProcessTracker()
    mcp: MCPServer[Any] = MCPServer(
        "openconstraint-mcp",
        instructions=instructions,
        website_url=_homepage_url(),
        lifespan=_make_lifespan(registry, cpsat_registry, child_tracker),
    )

    @mcp.tool(description=CHECK_RUNTIME_DESCRIPTION)
    def check_runtime() -> RuntimeStatus:
        return get_runtime_status()

    @mcp.tool(description=LIST_AVAILABLE_SOLVERS_DESCRIPTION)
    # Narrower than the default: this tool takes no user arguments, so a
    # ValueError would be a real bug, not an actionable user message.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def list_available_solvers() -> Annotated[CallToolResult, SolverList]:
        return _wrap_result(list_solvers(), format_solver_list_content)

    @mcp.tool(description=solve_minizinc_model_desc)
    @_as_mcp_error()
    async def solve_minizinc_model(
        model: str,
        data: str | None = None,
        checker: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        stages = status.solve_stages(checker is not None)
        result = await _run_tool_with_status(
            ctx,
            stages,
            functools.partial(
                solve_model,
                model,
                solver=solver,
                data=data,
                checker=checker,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
                tracker=child_tracker,
            ),
        )
        return _wrap_result(result, format_solve_result_content)

    @mcp.tool(description=CHECK_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def check_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CheckResult:
        return await _run_tool_with_status(
            ctx,
            status.CHECK_STAGES,
            functools.partial(
                check_model,
                model,
                solver=solver,
                data=data,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            ),
        )

    @mcp.tool(description=INSPECT_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def inspect_minizinc_model(
        model: str,
        data: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> ModelInspectionResult:
        return await _run_tool_with_status(
            ctx,
            status.INSPECT_STAGES,
            functools.partial(
                inspect_model,
                model,
                solver=solver,
                data=data,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            ),
        )

    @mcp.tool(description=FIND_UNSAT_CORE_DESCRIPTION)
    @_as_mcp_error()
    async def find_unsat_core(
        model: str,
        data: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> UnsatCoreResult:
        return await _run_tool_with_status(
            ctx,
            status.UNSAT_CORE_STAGES,
            functools.partial(
                _find_unsat_core, model, data=data, timeout_ms=timeout_ms, tracker=child_tracker
            ),
        )

    @mcp.tool(description=SAVE_VERIFIED_MINIZINC_MODEL_DESCRIPTION)
    @_as_mcp_error()
    async def save_verified_minizinc_model(
        model: str,
        target_dir: str,
        data: str | None = None,
        checker: str | None = None,
        problem: ProblemText = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
        overwrite: bool = False,
        portfolio_result: PortfolioSolveResult | None = None,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SaveVerifiedModelResult]:
        result = await _run_tool_with_status(
            ctx,
            status.SAVE_STAGES,
            functools.partial(
                save_verified_model,
                model,
                target_dir=Path(target_dir),
                data=data,
                checker=checker,
                problem=problem,
                solver=solver,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
                overwrite=overwrite,
                portfolio_result=portfolio_result,
                tracker=child_tracker,
            ),
        )
        return _wrap_result(result, format_save_result_content)

    @mcp.tool(description=CHECK_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def check_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_CHECK_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CheckResult:
        return await _run_tool_with_status(
            ctx,
            status.CHECK_STAGES,
            functools.partial(
                check_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            ),
        )

    @mcp.tool(description=INSPECT_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def inspect_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_INSPECT_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> ModelInspectionResult:
        return await _run_tool_with_status(
            ctx,
            status.INSPECT_STAGES,
            functools.partial(
                inspect_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            ),
        )

    @mcp.tool(description=SOLVE_MINIZINC_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def solve_minizinc_files(
        model_path: str,
        data_path: str | None = None,
        checker_path: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, SolveResult]:
        stages = status.solve_stages(checker_path is not None)
        result = await _run_tool_with_status(
            ctx,
            stages,
            functools.partial(
                solve_model_path,
                Path(model_path),
                solver=solver,
                data_path=Path(data_path) if data_path is not None else None,
                checker_path=Path(checker_path) if checker_path is not None else None,
                timeout_ms=timeout_ms,
                free_search=free_search,
                parallel=parallel,
                random_seed=random_seed,
                all_solutions=all_solutions,
                num_solutions=num_solutions,
                tracker=child_tracker,
            ),
        )
        return _wrap_result(result, format_solve_result_content)

    @mcp.tool(description=FIND_UNSAT_CORE_FILES_DESCRIPTION)
    @_as_mcp_error()
    async def find_unsat_core_files(
        model_path: str,
        data_path: str | None = None,
        timeout_ms: int = DEFAULT_UNSAT_CORE_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> UnsatCoreResult:
        return await _run_tool_with_status(
            ctx,
            status.UNSAT_CORE_STAGES,
            functools.partial(
                find_unsat_core_path,
                Path(model_path),
                data_path=Path(data_path) if data_path is not None else None,
                timeout_ms=timeout_ms,
                tracker=child_tracker,
            ),
        )

    @mcp.tool(description=SUBMIT_SOLVE_JOB_DESCRIPTION)
    # Validation raises ValueError; a full bounded queue raises JobRejectedError
    # (a direct RuntimeError subclass, NOT in the default caught set). A gated
    # control (free_search/parallel/random_seed/all_solutions) makes admission
    # resolve solver capabilities via list_solvers(), so the runtime/binary triad
    # can fire here too — exactly as for submit_portfolio_job. All must surface as
    # actionable MCP errors.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError, ValueError, JobRejectedError)
    def submit_solve_job(
        model: str,
        data: str | None = None,
        checker: str | None = None,
        solver: str = DEFAULT_SOLVER,
        timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        random_seed: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> SolveJobStatus:
        job_id = registry.submit(
            model=model,
            solver=solver,
            data=data,
            checker=checker,
            timeout_ms=timeout_ms,
            free_search=free_search,
            parallel=parallel,
            random_seed=random_seed,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        return registry.get(job_id)

    @mcp.tool(description=GET_SOLVE_JOB_DESCRIPTION)
    # An unknown job_id is the only domain error (ValueError); the registry reads
    # touch no runtime, so the narrower caught set is honest.
    @_as_mcp_error(ValueError)
    def get_solve_job(job_id: str) -> SolveJobStatus:
        return registry.get(job_id)

    @mcp.tool(description=CANCEL_SOLVE_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_solve_job(job_id: str) -> SolveJobStatus:
        return registry.cancel(job_id)

    @mcp.tool(description=LIST_SOLVE_JOBS_DESCRIPTION)
    # Takes no arguments and only reads the registry, so no domain exception is
    # reachable — nothing to translate.
    def list_solve_jobs() -> list[SolveJobStatus]:
        return registry.list()

    @mcp.tool(description=SUBMIT_PORTFOLIO_JOB_DESCRIPTION)
    # Admission runs plan validation, the capability gate, and an atomic batch
    # admission synchronously — so it can raise the runtime/binary triad (resolving
    # a gated control), a plan ValueError, or JobRejectedError when the batch exceeds
    # the bounded queue. All must surface as actionable MCP errors.
    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError, ValueError, JobRejectedError)
    def submit_portfolio_job(
        models: list[str],
        solvers: list[str],
        data: str | None = None,
        checker: str | None = None,
        seed_count: int = 1,
        seeds: list[int] | None = None,
        per_attempt_timeout_ms: int = DEFAULT_SOLVE_TIMEOUT_MS,
        free_search: bool = False,
        parallel: int | None = None,
        all_solutions: bool = False,
        num_solutions: int | None = None,
    ) -> PortfolioJobStatus:
        job_id = portfolios.submit(
            models=models,
            solvers=solvers,
            data=data,
            checker=checker,
            seed_count=seed_count,
            seeds=seeds,
            per_attempt_timeout_ms=per_attempt_timeout_ms,
            free_search=free_search,
            parallel=parallel,
            all_solutions=all_solutions,
            num_solutions=num_solutions,
        )
        return portfolios.get(job_id)

    @mcp.tool(description=GET_PORTFOLIO_JOB_DESCRIPTION)
    # An unknown job_id is the only domain error (ValueError); the registry reads
    # touch no runtime, so the narrower caught set is honest.
    @_as_mcp_error(ValueError)
    def get_portfolio_job(job_id: str) -> PortfolioJobStatus:
        return portfolios.get(job_id)

    @mcp.tool(description=CANCEL_PORTFOLIO_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_portfolio_job(job_id: str) -> PortfolioJobStatus:
        return portfolios.cancel(job_id)

    @mcp.tool(description=LIST_PORTFOLIO_JOBS_DESCRIPTION)
    # Takes no arguments and only reads the registry, so no domain exception is
    # reachable — nothing to translate.
    def list_portfolio_jobs() -> list[PortfolioJobStatus]:
        return portfolios.list()

    @mcp.tool(
        name="submit_cpsat_python_job",
        description=SUBMIT_CPSAT_PYTHON_JOB_DESCRIPTION,
    )
    @_as_mcp_error(ValueError, JobRejectedError)
    def submit_cpsat_python_job(
        source: str,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        problem: ProblemText = None,
        checker: str | None = None,
        checker_timeout_ms: int | None = None,
    ) -> CpsatPythonJobStatus:
        job_id = cpsat_registry.submit_source(
            source,
            script_timeout_ms=script_timeout_ms,
            problem=problem,
            checker=checker,
            checker_timeout_ms=checker_timeout_ms,
        )
        return cpsat_registry.get(job_id)

    @mcp.tool(
        name="submit_cpsat_python_file_job",
        description=SUBMIT_CPSAT_PYTHON_FILE_JOB_DESCRIPTION,
    )
    @_as_mcp_error(ValueError, JobRejectedError)
    def submit_cpsat_python_file_job(
        script_path: str,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        args: list[str] | None = None,
        problem: ProblemText = None,
        checker: str | None = None,
        checker_path: str | None = None,
        checker_timeout_ms: int | None = None,
    ) -> CpsatPythonJobStatus:
        job_id = cpsat_registry.submit_file(
            Path(script_path),
            script_timeout_ms=script_timeout_ms,
            args=args,
            problem=problem,
            checker=checker,
            checker_path=Path(checker_path) if checker_path is not None else None,
            checker_timeout_ms=checker_timeout_ms,
        )
        return cpsat_registry.get(job_id)

    @mcp.tool(name="get_cpsat_python_job", description=GET_CPSAT_PYTHON_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def get_cpsat_python_job(job_id: str) -> CpsatPythonJobStatus:
        return cpsat_registry.get(job_id)

    @mcp.tool(name="cancel_cpsat_python_job", description=CANCEL_CPSAT_PYTHON_JOB_DESCRIPTION)
    @_as_mcp_error(ValueError)
    def cancel_cpsat_python_job(job_id: str) -> CpsatPythonJobStatus:
        return cpsat_registry.cancel(job_id)

    @mcp.tool(name="list_cpsat_python_jobs", description=LIST_CPSAT_PYTHON_JOBS_DESCRIPTION)
    def list_cpsat_python_jobs() -> list[CpsatPythonJobStatus]:
        return cpsat_registry.list()

    @mcp.tool(name="run_cpsat_python", description=run_cpsat_python_desc)
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_tool(
        source: str,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        ctx: Context | None = None,
    ) -> CpsatPythonResult:
        # No MCP-facing seed/config for this inline tool (see module docstring on
        # `run_cpsat_python`'s `env` param); always clear both protocol env vars so
        # a value inherited from the server's own launch environment cannot leak
        # into the child.
        return await _run_tool_with_status(
            ctx,
            status.CPSAT_PYTHON_STAGES,
            functools.partial(
                run_cpsat_python,
                source,
                script_timeout_ms=script_timeout_ms,
                tracker=child_tracker,
                env=seed_config_env(seed=None, config_path=None),
            ),
        )

    @mcp.tool(name="run_cpsat_python_file", description=run_cpsat_python_file_desc)
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_file_tool(
        script_path: str,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        args: list[str] | None = None,
        seed: StrictInt | None = None,
        config: dict[str, JsonValue] | None = None,
        ctx: Context | None = None,
    ) -> CpsatPythonResult:
        await _status_starting(ctx, status.CPSAT_PYTHON_STAGES)
        validated_seed = validate_cpsat_random_seed(seed) if seed is not None else None
        normalized_config: dict[str, Any] | None = config if config else None
        result = await _run_blocking(
            functools.partial(
                _run_cpsat_python_file_with_replay,
                Path(script_path),
                script_timeout_ms=script_timeout_ms,
                seed=validated_seed,
                config=normalized_config,
                args=args,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CPSAT_PYTHON_STAGES)
        return result

    @mcp.tool(
        name="run_cpsat_python_file_checked",
        description=RUN_CPSAT_PYTHON_FILE_CHECKED_DESCRIPTION,
    )
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_file_checked_tool(
        script_path: str,
        checker_path: str,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        args: list[str] | None = None,
        problem: ProblemText = None,
        checker_timeout_ms: int | None = None,
        test_checker: bool = False,
        seed: StrictInt | None = None,
        config: dict[str, JsonValue] | None = None,
        ctx: Context | None = None,
    ) -> CpsatPythonCheckedResult:
        await _status_starting(ctx, status.CPSAT_PYTHON_CHECKED_STAGES)
        validated_seed = validate_cpsat_random_seed(seed) if seed is not None else None
        normalized_config: dict[str, Any] | None = config if config else None
        result = await _run_blocking(
            functools.partial(
                _run_cpsat_python_file_checked_with_replay,
                Path(script_path),
                Path(checker_path),
                problem=problem,
                script_timeout_ms=script_timeout_ms,
                checker_timeout_ms=checker_timeout_ms,
                args=args,
                test_checker=test_checker,
                seed=validated_seed,
                config=normalized_config,
                tracker=child_tracker,
            )
        )
        await _status_finished(ctx, status.CPSAT_PYTHON_CHECKED_STAGES)
        return result

    @mcp.tool(
        name="run_cpsat_python_experiment", description=RUN_CPSAT_PYTHON_EXPERIMENT_DESCRIPTION
    )
    @_as_mcp_error(ValueError)
    async def run_cpsat_python_experiment_tool(
        attempts: list[CpsatPythonExperimentAttempt],
        objective_sense: CpsatObjectiveSense | None = None,
        default_script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        max_parallel_attempts: int = 1,
        problem: ProblemText = None,
        checker: str | None = None,
        checker_timeout_ms: int | None = None,
        include_winner_stdout: bool = True,
        ctx: Context | None = None,
    ) -> Annotated[CallToolResult, CpsatPythonExperimentResult]:
        result = await _run_tool_with_status(
            ctx,
            status.CPSAT_EXPERIMENT_STAGES,
            functools.partial(
                run_cpsat_python_experiment,
                attempts,
                objective_sense=objective_sense,
                default_script_timeout_ms=default_script_timeout_ms,
                max_parallel_attempts=max_parallel_attempts,
                problem=problem,
                checker=checker,
                checker_timeout_ms=checker_timeout_ms,
                include_winner_stdout=include_winner_stdout,
                tracker=child_tracker,
            ),
        )
        return _wrap_result(result, format_cpsat_experiment_content)

    @mcp.tool(name="save_verified_cpsat_python", description=SAVE_VERIFIED_CPSAT_PYTHON_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def save_verified_cpsat_python_tool(
        source: str,
        target_dir: str | None = None,
        problem: ProblemText = None,
        expectation: CpsatExpectation | None = None,
        checker: str | None = None,
        checker_timeout_ms: int | None = None,
        script_timeout_ms: int = DEFAULT_PYEXEC_TIMEOUT_MS,
        overwrite: bool = False,
        verify_only: bool = False,
        seed: StrictInt | None = None,
        config: dict[str, JsonValue] | None = None,
        experiment_result: CpsatPythonExperimentResult | None = None,
        ctx: Context | None = None,
    ) -> SaveVerifiedPythonResult:
        stages = status.cpsat_save_stages(with_checker=checker is not None)
        return await _run_tool_with_status(
            ctx,
            stages,
            functools.partial(
                save_verified_cpsat_python,
                source,
                target_dir=Path(target_dir) if target_dir is not None else None,
                problem=problem,
                expectation=expectation,
                checker=checker,
                checker_timeout_ms=checker_timeout_ms,
                script_timeout_ms=script_timeout_ms,
                overwrite=overwrite,
                verify_only=verify_only,
                seed=seed,
                config=config,
                experiment_result=experiment_result,
                tracker=child_tracker,
            ),
        )

    # The tabular tools take no solver and touch no managed runtime, so a
    # ValueError is the only domain exception they raise (every rejection —
    # bad path, non-scalar cell, refused to overwrite — is one).
    @mcp.tool(name="load_tabular_data", description=LOAD_TABULAR_DATA_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def load_tabular_data(
        path: str,
        sheet: str | None = None,
        has_header: bool = True,
        row_offset: int = 0,
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> Annotated[CallToolResult, TabularData]:
        result = await _run_blocking(
            functools.partial(
                read_tabular_data,
                Path(path),
                sheet=sheet,
                has_header=has_header,
                row_offset=row_offset,
                max_rows=max_rows,
            )
        )
        return _wrap_result(result, format_tabular_data_content)

    @mcp.tool(name="write_tabular_result", description=WRITE_TABULAR_RESULT_DESCRIPTION)
    @_as_mcp_error(ValueError)
    async def write_tabular_result(
        headers: list[str],
        rows: list[list[TabularCell]],
        target_path: str,
        overwrite: bool = False,
    ) -> TabularWriteResult:
        return await _run_blocking(
            functools.partial(
                write_tabular_data,
                headers,
                rows,
                Path(target_path),
                overwrite=overwrite,
            )
        )

    # Registered ABOVE the core early return, so both profiles expose it: this
    # is the one backend-neutral prompt, and it names core tools only.
    @mcp.prompt(
        name="solve_constraint_problem",
        description=SOLVE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    )
    def solve_constraint_problem(problem: str) -> str:
        return solve_constraint_problem_prompt.format(problem=problem)

    if is_core:
        # Finalize the core profile before any client connects: drop every
        # full-only tool and register only the backend-neutral
        # `solve_constraint_problem` prompt above. The three detailed prompts
        # below stay full-only because they reference save, portfolio,
        # experiment, inspection, and job tools core hides.
        # remove_tool() raises ToolError on an unknown name, so a rename or
        # deletion upstream breaks core construction loudly instead of silently
        # drifting.
        for name in _FULL_ONLY_TOOL_NAMES:
            mcp.remove_tool(name)
        return mcp

    @mcp.prompt(
        name="minizinc_solution_workflow",
        description=MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    )
    def minizinc_solution_workflow(problem: str) -> str:
        return MINIZINC_SOLUTION_WORKFLOW_PROMPT.format(problem=problem)

    @mcp.prompt(
        name="cpsat_python_solution_workflow",
        description=CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    )
    def cpsat_python_solution_workflow(problem: str) -> str:
        return SOLVE_CPSAT_PYTHON_PROMPT.format(problem=problem)

    @mcp.prompt(
        name="auto_tune_constraint_problem",
        description=AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    )
    def auto_tune_constraint_problem_prompt(problem: str) -> str:
        return AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT.format(problem=problem)

    return mcp


def run_stdio(toolset: str = "core") -> None:
    """Create the MCP server and run it over stdio for CLI/client use.

    Defaults to the ``core`` profile: the user-facing ``stdio`` entry point
    advertises the eight core tools and the ``solve_constraint_problem`` prompt
    only. Pass ``full`` for the complete tool and four-prompt surface.
    """
    create_mcp_server(toolset).run(transport="stdio")
