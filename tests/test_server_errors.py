from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.core import MiniZincExecutionError
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas.cpsat import CpsatPythonExperimentAttempt
from openconstraint_mcp.schemas.diagnostics import InvalidSaveTargetError, UnsupportedFeatureError

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _as_mcp_error,
    _classify_domain_error,
    _translated_error,
    create_mcp_server,
)
from openconstraint_mcp.shared.job_errors import JobRejectedError

# --- _as_mcp_error: the single (domain exc -> RuntimeError) translation home ---
#
# RuntimeMissingError and MiniZincExecutionError both subclass RuntimeError, so a
# bare `pytest.raises(RuntimeError)` cannot tell a *translated* plain RuntimeError
# apart from the domain subclass passing through untouched. Every translation
# assertion therefore pins `type(...) is RuntimeError` plus the `__cause__` chain.


def _no_subprocess(*args: object, **kwargs: object) -> None:
    raise AssertionError("subprocess.run must not be invoked: validation precedes it")


def _tool_fn(name: str) -> Any:
    """Return a registered tool's underlying (decorated) function.

    Reaches past ``mcp.call_tool`` — which re-wraps a tool's exception in its own
    error type — to the decorated function itself, the only seam where a test can
    observe the exact ``RuntimeError`` type and its preserved ``__cause__``.
    """
    return create_mcp_server()._tool_manager.get_tool(name).fn


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeMissingError("runtime missing; run install-runtime"),
        MiniZincExecutionError("managed binary failed: bad config"),
        ValueError("model must not be empty"),
    ],
    ids=["runtime_missing", "execution_error", "value_error"],
)
def test_as_mcp_error_translates_default_domain_exceptions(exc: Exception) -> None:
    @_as_mcp_error()
    def tool() -> None:
        raise exc

    with pytest.raises(RuntimeError) as exc_info:
        tool()

    assert type(exc_info.value) is RuntimeError
    # The original message is always preserved; a classifiable pre-result error
    # additionally gains a `Diagnostic: <category> — …` first line.
    assert str(exc) in str(exc_info.value)
    assert exc_info.value.__cause__ is exc


def test_as_mcp_error_does_not_translate_unlisted_exception() -> None:
    boom = KeyError("unexpected")

    @_as_mcp_error()
    def tool() -> None:
        raise boom

    with pytest.raises(KeyError) as exc_info:
        tool()
    assert exc_info.value is boom


def test_as_mcp_error_narrow_set_translates_a_listed_type() -> None:
    boom = MiniZincExecutionError("bad config")

    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def tool() -> None:
        raise boom

    with pytest.raises(RuntimeError) as exc_info:
        tool()
    assert type(exc_info.value) is RuntimeError
    assert exc_info.value.__cause__ is boom


def test_as_mcp_error_narrow_set_skips_unlisted_value_error() -> None:
    # The list_available_solvers caught set omits ValueError; it must propagate.
    boom = ValueError("model must not be empty")

    @_as_mcp_error(RuntimeMissingError, MiniZincExecutionError)
    def tool() -> None:
        raise boom

    with pytest.raises(ValueError) as exc_info:
        tool()
    assert exc_info.value is boom


def test_as_mcp_error_returns_value_on_success() -> None:
    @_as_mcp_error()
    def tool() -> str:
        return "ok"

    assert tool() == "ok"


@pytest.mark.asyncio
async def test_as_mcp_error_translates_domain_exception_from_async_tool() -> None:
    # The async branch must translate inside the coroutine: a sync wrapper would
    # return the un-awaited coroutine from `try` and never reach `except`.
    boom = ValueError("model must not be empty")

    @_as_mcp_error()
    async def tool() -> None:
        raise boom

    with pytest.raises(RuntimeError) as exc_info:
        await tool()

    assert type(exc_info.value) is RuntimeError
    assert str(boom) in str(exc_info.value)
    assert exc_info.value.__cause__ is boom


@pytest.mark.asyncio
async def test_as_mcp_error_returns_value_from_async_tool_on_success() -> None:
    @_as_mcp_error()
    async def tool() -> str:
        return "ok"

    assert await tool() == "ok"


@pytest.mark.asyncio
async def test_as_mcp_error_does_not_translate_unlisted_exception_from_async_tool() -> None:
    boom = KeyError("not in the caught set")

    @_as_mcp_error()
    async def tool() -> None:
        raise boom

    with pytest.raises(KeyError) as exc_info:
        await tool()
    assert exc_info.value is boom


def test_as_mcp_error_preserves_signature_for_mcpserver() -> None:
    # MCPServer derives each tool's schema from the wrapped function's signature;
    # functools.wraps must keep it visible through the decorator.
    def tool(model: str, timeout_ms: int = 5) -> bool:
        return True

    decorated = _as_mcp_error()(tool)

    assert decorated.__wrapped__ is tool  # type: ignore[attr-defined]
    assert decorated.__name__ == "tool"
    assert inspect.signature(decorated) == inspect.signature(tool)


# --- per-tool wiring: each tool carries the correct caught set --------------


@pytest.mark.parametrize(
    "tool_name",
    ["solve_minizinc_model", "check_minizinc_model", "inspect_minizinc_model", "find_unsat_core"],
)
@pytest.mark.asyncio
async def test_string_tools_translate_value_error_with_cause(
    tool_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An empty model raises ValueError before the runtime gate; the default caught
    # set must convert it to a plain RuntimeError with the cause preserved. The
    # direct call passes no ctx, so the async wrappers must default it to None.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _no_subprocess)
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model="")

    assert type(exc_info.value) is RuntimeError
    assert "empty" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "tool_name",
    [
        "solve_minizinc_files",
        "check_minizinc_files",
        "inspect_minizinc_files",
        "find_unsat_core_files",
    ],
)
@pytest.mark.asyncio
async def test_file_tools_translate_value_error_with_cause(
    tool_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A missing model path raises ValueError before the runtime gate; same
    # translation invariant on the path-based tools.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _no_subprocess)
    missing = tmp_path / "nope.mzn"
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model_path=str(missing))

    assert type(exc_info.value) is RuntimeError
    assert "does not exist" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


@pytest.mark.asyncio
async def test_save_tool_translates_target_value_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A relative target_dir raises ValueError ahead of the runtime gate and any
    # subprocess; the default caught set converts it to a plain RuntimeError
    # whose message tells the client how to fix the call.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _no_subprocess)
    fn = _tool_fn("save_verified_minizinc_model")

    with pytest.raises(RuntimeError) as exc_info:
        await fn(model="solve satisfy;", target_dir="relative/project")

    assert type(exc_info.value) is RuntimeError
    assert "absolute" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_list_available_solvers_translates_execution_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    boom = MiniZincExecutionError("bad config")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.server.list_solvers", _raise)
    fn = _tool_fn("list_available_solvers")

    with pytest.raises(RuntimeError) as exc_info:
        fn()

    assert type(exc_info.value) is RuntimeError
    assert str(exc_info.value) == "bad config"
    assert exc_info.value.__cause__ is boom


def test_list_available_solvers_does_not_translate_value_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Its narrower caught set omits ValueError: a ValueError here is a real bug,
    # so it must propagate untouched rather than masquerade as an actionable
    # RuntimeError message.
    boom = ValueError("unexpected internal error")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.server.list_solvers", _raise)
    fn = _tool_fn("list_available_solvers")

    with pytest.raises(ValueError) as exc_info:
        fn()
    assert exc_info.value is boom


# --- background-job tools: unknown-id and queue-full translation ------------


@pytest.mark.parametrize("tool_name", ["get_solve_job", "cancel_solve_job"])
def test_job_lookup_tools_translate_unknown_id_with_cause(tool_name: str) -> None:
    # The registry raises ValueError for an unknown job_id; the default-caught
    # ValueError becomes a plain RuntimeError carrying the cause. These tools are
    # synchronous (fast registry reads), so they are called directly.
    fn = _tool_fn(tool_name)

    with pytest.raises(RuntimeError) as exc_info:
        fn(job_id="does-not-exist")

    assert type(exc_info.value) is RuntimeError
    assert "unknown" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_submit_solve_job_translates_queue_full_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # JobRejectedError subclasses RuntimeError but is NOT in the default caught
    # set, so submit_solve_job must name it explicitly; the assertion pins the
    # exact RuntimeError type to prove translation (not subclass passthrough).
    boom = JobRejectedError("Job queue is full (4 running + 16 queued).")

    def _raise(self: object, **kwargs: object) -> str:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.JobRegistry.submit", _raise)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="solve satisfy;")

    assert type(exc_info.value) is RuntimeError
    assert "queue is full" in str(exc_info.value)
    assert exc_info.value.__cause__ is boom


def test_submit_solve_job_translates_value_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty model raises ValueError in submit's up-front validation, before any
    # job or subprocess; the default caught set converts it to a plain RuntimeError.
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _no_subprocess)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="")

    assert type(exc_info.value) is RuntimeError
    assert "empty" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ValueError)


def test_submit_solve_job_translates_capability_gate_runtime_error_with_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gated control (free_search) makes admission resolve solver capabilities,
    # which runs list_solvers() — so an uninstalled/corrupt runtime raises
    # RuntimeMissingError at submit, BEFORE any job exists. The wrapper must
    # translate it like every other actionable runtime error (pin the exact
    # RuntimeError type to prove translation, not subclass passthrough).
    boom = RuntimeMissingError("runtime missing; run install-runtime")

    def _raise() -> object:
        raise boom

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.list_solvers", _raise)
    fn = _tool_fn("submit_solve_job")

    with pytest.raises(RuntimeError) as exc_info:
        fn(model="solve satisfy;", free_search=True)

    assert type(exc_info.value) is RuntimeError
    assert "install-runtime" in str(exc_info.value)
    assert exc_info.value.__cause__ is boom


# --- Stage 2: structured diagnostic on pre-result MCP errors ----------------
#
# The mcp SDK's tool-exception path surfaces only the message string, so the
# structured contract rides in a documented `Diagnostic: <category> — …` first
# line (see server._translated_error). These tests pin the classifier and prove
# the contract reaches clients through the real MCP tool path.


def test_classify_runtime_missing() -> None:
    diag = _classify_domain_error(RuntimeMissingError("runtime not found"))
    assert diag is not None
    assert diag.category == "runtime_missing"


def test_classify_unsupported_control() -> None:
    diag = _classify_domain_error(
        UnsupportedFeatureError("solver 'cp-sat' does not support free_search (the -f flag).")
    )
    assert diag is not None
    assert diag.category == "unsupported_feature"


def test_classify_invalid_save_target() -> None:
    diag = _classify_domain_error(
        InvalidSaveTargetError("target_dir must be an absolute path: rel")
    )
    assert diag is not None
    assert diag.category == "invalid_save_target"


def test_classify_refusing_to_overwrite_is_invalid_save_target() -> None:
    diag = _classify_domain_error(
        InvalidSaveTargetError(
            "refusing to overwrite the prior saved model at /x; pass overwrite=true"
        )
    )
    assert diag is not None
    assert diag.category == "invalid_save_target"


def test_classify_generic_value_error_is_invalid_request() -> None:
    diag = _classify_domain_error(ValueError("model must not be empty"))
    assert diag is not None
    assert diag.category == "invalid_request"


def test_classify_tabular_overwrite_refusal_is_invalid_request() -> None:
    # tabular_io.py's plain file-exists refusal reuses the words "refusing to
    # overwrite" in prose but is a plain ValueError, never
    # InvalidSaveTargetError — classification is by type, so message content
    # (however similar to save_target.py's prose) cannot collide.
    diag = _classify_domain_error(
        ValueError(
            "refusing to overwrite the existing file at /x; pass overwrite=true to replace it."
        )
    )
    assert diag is not None
    assert diag.category == "invalid_request"


def test_classify_value_error_containing_marker_prose_is_still_invalid_request() -> None:
    # Classification is by exception type, not message content, so even a
    # plain ValueError whose text happens to contain "target_dir" or "does
    # not support" (e.g. because a user-chosen path embeds that text) cannot
    # be misclassified the way the old message-marker classifier could.
    diag = _classify_domain_error(
        ValueError(
            "target_path must be an absolute path: "
            "does not support/target_dir/the prior save did not write/out.csv"
        )
    )
    assert diag is not None
    assert diag.category == "invalid_request"


def test_classify_experiment_budget_is_invalid_request() -> None:
    diag = _classify_domain_error(
        ValueError("projected experiment budget 300000 ms exceeds MAX_CPSAT_EXPERIMENT...")
    )
    assert diag is not None
    assert diag.category == "invalid_request"


@pytest.mark.parametrize("exc", [MiniZincExecutionError("corrupt"), JobRejectedError("queue full")])
def test_classify_non_prerequest_errors_pass_through(exc: Exception) -> None:
    # Runtime corruption and transient capacity are not client-repairable input
    # states, so they carry no Diagnostic and keep their verbatim message.
    assert _classify_domain_error(exc) is None


def test_translated_error_leads_with_diagnostic_line() -> None:
    err = _translated_error(RuntimeMissingError("runtime not found; run install-runtime"))
    assert str(err).startswith("Diagnostic: runtime_missing — runtime not found")


def test_translated_error_preserves_multiline_detail() -> None:
    err = _translated_error(ValueError("first line\nsecond line of detail"))
    lines = str(err).splitlines()
    assert lines[0] == "Diagnostic: invalid_request — first line"
    assert lines[1] == "second line of detail"


def test_translated_error_unclassified_stays_verbatim() -> None:
    err = _translated_error(MiniZincExecutionError("bad config"))
    assert str(err) == "bad config"
    assert not str(err).startswith("Diagnostic:")


@pytest.mark.asyncio
async def test_tool_malformed_model_path_exposes_invalid_request(tmp_path: Path) -> None:
    # Required through-the-tool proof: a nonexistent model_path raises ValueError
    # before the runtime gate, and the client sees the invalid_request contract.
    fn = _tool_fn("solve_minizinc_files")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(model_path=str(tmp_path / "nope.mzn"))
    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "does not exist" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_relative_target_dir_exposes_invalid_save_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _no_subprocess)
    fn = _tool_fn("save_verified_minizinc_model")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(model="solve satisfy;", target_dir="relative/project")
    assert str(exc_info.value).startswith("Diagnostic: invalid_save_target — ")


def test_tool_runtime_missing_exposes_runtime_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A gated control makes submit resolve capabilities via list_solvers; an
    # uninstalled runtime raises RuntimeMissingError before any job exists.
    def _raise() -> object:
        raise RuntimeMissingError("Managed MiniZinc runtime not found. Run install-runtime.")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.list_solvers", _raise)
    fn = _tool_fn("submit_solve_job")
    with pytest.raises(RuntimeError) as exc_info:
        fn(model="solve satisfy;", free_search=True)
    assert str(exc_info.value).startswith("Diagnostic: runtime_missing — ")


@pytest.mark.asyncio
async def test_tool_experiment_budget_exposes_invalid_request() -> None:
    # A per-attempt budget past the wall-clock cap is rejected before any child
    # runs; the client sees invalid_request through the experiment tool.
    fn = _tool_fn("run_cpsat_python_experiment")
    attempt = CpsatPythonExperimentAttempt(source="print('x')")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(attempts=[attempt], default_script_timeout_ms=10_000_000)
    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "budget" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_checked_run_budget_exposes_invalid_request(tmp_path: Path) -> None:
    script = tmp_path / "model.py"
    checker = tmp_path / "checker.py"
    script.write_text("print('model')", encoding="utf-8")
    checker.write_text("print('checker')", encoding="utf-8")
    fn = _tool_fn("run_cpsat_python_file_checked")

    with pytest.raises(RuntimeError) as exc_info:
        await fn(
            script_path=str(script),
            checker_path=str(checker),
            checker_timeout_ms=30_000,
            test_checker=True,
        )

    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "projected budget" in str(exc_info.value)


@pytest.mark.asyncio
async def test_tool_experiment_hash_race_exposes_indexed_invalid_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")

    def _disappeared(_path: Path) -> str:
        raise FileNotFoundError("disappeared before hashing")

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.path_sha256", _disappeared)
    fn = _tool_fn("run_cpsat_python_experiment")

    with pytest.raises(RuntimeError) as exc_info:
        await fn(attempts=[CpsatPythonExperimentAttempt(script_path=str(script))])

    assert str(exc_info.value).startswith(
        "Diagnostic: invalid_request — attempts[0].script_path became unreadable"
    )


# --- tabular I/O tools: every rejection reaches the client as an MCP error ------


@pytest.mark.asyncio
async def test_load_tabular_data_missing_file_is_an_invalid_request(tmp_path: Path) -> None:
    fn = _tool_fn("load_tabular_data")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(path=str(tmp_path / "absent.csv"))
    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "does not exist" in str(exc_info.value)


@pytest.mark.asyncio
async def test_load_tabular_data_unsupported_suffix_is_an_invalid_request(tmp_path: Path) -> None:
    source = tmp_path / "data.ods"
    source.write_text("", encoding="utf-8")
    fn = _tool_fn("load_tabular_data")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(path=str(source))
    assert "unsupported tabular file type" in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_tabular_result_relative_path_is_an_invalid_request() -> None:
    fn = _tool_fn("write_tabular_result")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(headers=["a"], rows=[["1"]], target_path="out.csv")
    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "absolute" in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_tabular_result_refuses_to_clobber_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    target.write_text("keep me\n", encoding="utf-8")

    fn = _tool_fn("write_tabular_result")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(headers=["a"], rows=[["1"]], target_path=str(target))
    assert str(exc_info.value).startswith("Diagnostic: invalid_request — ")
    assert "refusing to overwrite" in str(exc_info.value)
    assert target.read_text(encoding="utf-8") == "keep me\n"


@pytest.mark.asyncio
async def test_write_tabular_result_rejects_a_formula_string_for_csv(tmp_path: Path) -> None:
    fn = _tool_fn("write_tabular_result")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(headers=["a"], rows=[["=1+1"]], target_path=str(tmp_path / "out.csv"))
    assert "formula" in str(exc_info.value)
    assert not (tmp_path / "out.csv").exists()


@pytest.mark.asyncio
async def test_write_tabular_result_rejects_a_ragged_row(tmp_path: Path) -> None:
    fn = _tool_fn("write_tabular_result")
    with pytest.raises(RuntimeError) as exc_info:
        await fn(headers=["a", "b"], rows=[["1"]], target_path=str(tmp_path / "out.csv"))
    assert "every row must have exactly one cell per header" in str(exc_info.value)
