from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult

from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.schemas.cpsat import (
    CpsatPythonCheckedResult,
    CpsatPythonExperimentAttempt,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from openconstraint_mcp.schemas.minizinc import (
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
)

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    _CONTEXT_UNAVAILABLE_MESSAGE,
    _report_status,
    _run_blocking,
    create_mcp_server,
)
from openconstraint_mcp.shared.childrun import ChildExecutionResult
from openconstraint_mcp.shared.job_errors import JobRejectedError
from openconstraint_mcp.shared.save_target import EXPERIMENT_LOG_FILENAME, text_sha256
from tests.minizinc.helpers import (
    UNSAT_CORE_MODEL,
    UNSAT_CORE_STDOUT,
    checker_pass,
    checker_violation,
    child_result,
    solution_obj,
    solution_obj_json_only,
    stream,
)


def _patch_capabilities(
    monkeypatch: pytest.MonkeyPatch, caps_by_id: dict[str, SolverCapabilities]
) -> None:
    """Point the capability enforcer's ``list_solvers()`` at ``caps_by_id``."""
    solvers = [
        SolverInfo(id=solver_id, name=solver_id, capabilities=caps)
        for solver_id, caps in caps_by_id.items()
    ]
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.list_solvers", lambda: SolverList(solvers=solvers)
    )


def _patch_full_capabilities(monkeypatch: pytest.MonkeyPatch, *solver_ids: str) -> None:
    """Report each ``solver_ids`` entry as supporting every ``-a/-f/-p/-r`` control.

    The forwards-flags tests mock ``subprocess.run`` to return a solve stream; the
    capability enforcer's ``list_solvers()`` would otherwise try to parse that
    stream as ``--solvers-json``. This points the enforcer at a fully-capable map
    so the gated controls resolve and forward instead of raising.
    """
    full = SolverCapabilities(
        supports_all_solutions=True,
        supports_free_search=True,
        supports_parallel=True,
        supports_random_seed=True,
    )
    _patch_capabilities(monkeypatch, {solver_id: full for solver_id in solver_ids})


@pytest.mark.asyncio
async def test_list_available_solvers_surfaces_actionable_error_on_binary_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[str(fake_minizinc_binary), "--solvers-json"],
            stderr="bad config\n",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("list_available_solvers", {})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "bad config" in message


def _structured(result: Any) -> dict[str, Any]:
    """Extract the structured-content dict from an MCPServer call_tool result.

    ``MCPServer.call_tool()`` returns a direct ``CallToolResult`` for every
    tool call.
    """
    assert isinstance(result, CallToolResult)
    assert result.structured_content is not None
    return result.structured_content


def _content_text(result: Any) -> str:
    assert isinstance(result, CallToolResult)
    return "\n".join(block.text for block in result.content if hasattr(block, "text"))


def _record_data_run(
    monkeypatch: pytest.MonkeyPatch, completed: ChildExecutionResult
) -> list[dict[str, Any]]:
    """Patch subprocess.run to capture the inline-data file contents at call time.

    The runtime deletes its temp dir on return, so the ``data.dzn`` written from
    an inline ``data`` argument must be read inside the fake, before cleanup.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        cmd = args[0]
        data_contents: str | None = None
        data_path = next((Path(arg) for arg in cmd if str(arg).endswith(".dzn")), None)
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        calls.append({"data_contents": data_contents})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_list_available_solvers_surfaces_capabilities(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Happy path (the other server test only covers the failure path): the richer
    # SolverInfo flows through to structured content with no per-field wiring.
    payload = json.dumps(
        [
            {
                "id": "org.gecode.gecode",
                "name": "Gecode",
                "version": "6.3.0",
                "stdFlags": ["-a", "-f", "-n", "-p", "-r"],
            },
            {
                "id": "com.google.or-tools.cpsat",
                "name": "OR-Tools CP-SAT",
                "version": "9.10",
                "stdFlags": ["-a", "-i", "-f", "-p", "-r"],
            },
        ]
    )

    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(stdout=payload, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool("list_available_solvers", {})

    structured = _structured(result)
    by_id = {solver["id"]: solver for solver in structured["solvers"]}
    gecode_caps = by_id["org.gecode.gecode"]["capabilities"]
    cpsat_caps = by_id["com.google.or-tools.cpsat"]["capabilities"]

    # gecode declares -n and is allowlisted; or-tools declares -a/-f/-p/-r but not
    # -n and is not allowlisted, so the conservative gate holds across the boundary.
    assert gecode_caps["supports_num_solutions"] is True
    assert gecode_caps["std_flags"] == ["-a", "-f", "-n", "-p", "-r"]
    assert cpsat_caps["supports_num_solutions"] is False
    assert cpsat_caps["supports_parallel"] is True
    assert "Detailed solver capabilities" in structured["capability_note"]


# A distinctive `--cp-profiler` flag in gecode's stdFlags lets a test prove the
# default text view does NOT dump raw std_flags values (only the field name).
_LIST_SOLVERS_PAYLOAD = json.dumps(
    [
        {
            "id": "org.gecode.gecode",
            "name": "Gecode",
            "version": "6.3.0",
            "stdFlags": ["-a", "-f", "-n", "-p", "-r", "--cp-profiler"],
        },
        {
            "id": "cp-sat",
            "name": "OR Tools CP-SAT",
            "version": "9.15",
            "stdFlags": ["-a", "-i", "-f", "-p", "-r"],
        },
    ]
)


def _patch_list_solvers_run(
    monkeypatch: pytest.MonkeyPatch, payload: str = _LIST_SOLVERS_PAYLOAD
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(stdout=payload, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)


@pytest.mark.asyncio
async def test_list_available_solvers_text_lists_every_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model-visible text must carry a complete row per solver, so a client
    # cannot silently drop entries it would otherwise summarize away.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "| id | name | version |" in text
    assert "capability hint" not in text.lower()
    for token in (
        "org.gecode.gecode",
        "Gecode",
        "6.3.0",
        "cp-sat",
        "OR Tools CP-SAT",
        "9.15",
    ):
        assert token in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_requires_complete_inventory(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The presentation requirement is what steers clients away from "additional
    # solvers" elisions; assert the binding instruction is present.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "copy the solver inventory table below" in text
    assert "Do not omit rows" in text
    assert "convert it to bullets" in text
    assert "summarize, group" in text
    assert "additional solvers" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_notes_num_solutions_support(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The routing note names exactly the two gated solvers and flags the default
    # cp-sat as unsupported.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "is supported only by" in text
    assert "org.chuffed.chuffed" in text
    assert "org.gecode.gecode" in text
    assert "cp-sat` solver does not support it" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_cautions_external_mip_setup(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A declared MIP solver entry is not a guarantee it can run; the caution must
    # name the commercial/external solvers and the separate-setup requirement.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "runtime configuration" in text
    for solver in ("CPLEX", "Gurobi", "Xpress", "SCIP", "COIN-BC"):
        assert solver in text
    assert "separate installed binaries" in text


@pytest.mark.asyncio
async def test_list_available_solvers_text_points_to_structured_capabilities_without_dumping_flags(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The text advertises where the rich capability metadata lives (structured, on
    # request) but must NOT dump raw std_flags values into the default view — the
    # distinctive `--cp-profiler` flag from the payload proves no flag dump.
    _patch_list_solvers_run(monkeypatch)
    mcp = create_mcp_server()

    text = _content_text(await mcp.call_tool("list_available_solvers", {}))

    assert "Detailed solver capabilities are available on request" in text
    assert "structured result includes" in text
    assert "capabilities.supports_all_solutions" in text
    assert "for each solver" in text
    assert "std_flags" in text
    assert "--cp-profiler" not in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_minizinc_model" in names

    tool = next(t for t in tools if t.name == "solve_minizinc_model")
    properties = tool.input_schema.get("properties", {})
    assert {
        "model",
        "data",
        "solver",
        "timeout_ms",
        "free_search",
        "parallel",
        "random_seed",
        "all_solutions",
        "num_solutions",
        "checker",
    } <= set(properties.keys())


@pytest.mark.asyncio
async def test_solve_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(
                solution_obj("x = 3;\n", {"x": 3}),
                {"type": "statistics", "statistics": {"solveTime": 0.01}},
            ),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    # A single `satisfy` that finds a solution emits no status object; the
    # return-code fallback resolves a clean exit with a solution to "satisfied".
    assert structured["status"] == "satisfied"
    assert structured["solver"] == "cp-sat"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    # The structured solution fields are reconstructed from the stream's
    # output.json payload; a satisfaction model carries no _objective.
    assert structured["solution"] == {"x": 3}
    assert structured["solutions"] == [{"x": 3}]
    assert structured["objective"] is None
    assert structured["checker"] is None
    # Parsed statistics objects flow through MCPServer structured content.
    assert structured["statistics"] == {"solveTime": "0.01"}

    # The model-visible default content also highlights non-empty statistics,
    # instead of relying on clients to notice them inside raw JSON.
    text = _content_text(result)
    lower_text = text.lower()
    assert "Final answer requirement" in text
    assert "entire Statistics section" in text
    assert "do not omit" in lower_text
    assert "summarize" in lower_text
    assert "selected fields" in lower_text
    assert "Statistics:" in text
    assert "- solveTime: 0.01" in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_output_cap_truncates_and_surfaces_diagnostic(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An output-cap tree-kill flows all the way to the MCP structured result:
    # partial solutions are kept, return_code is null, status is the stream verdict
    # (satisfied), and the diagnostic is output_truncated.
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})),
            stderr="",
            returncode=-15,
            truncated=True,
            truncation_killed=True,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["truncated"] is True
    assert structured["status"] == "satisfied"
    assert structured["return_code"] is None
    assert structured["timed_out"] is False
    assert structured["solution"] == {"x": 3}
    assert structured["diagnostic"]["category"] == "output_truncated"


@pytest.mark.asyncio
async def test_solve_minizinc_model_accepts_inline_checker(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *args, **kwargs: child_result(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;",
            "checker": _CHECKER_SRC,
        },
    )

    structured = _structured(result)
    assert structured["status"] == "satisfied"
    assert structured["checker"]["status"] == "completed"
    assert structured["checker"]["checks"] == [{"violation": False, "output": "CORRECT\n"}]
    assert '"type": "checker"' in structured["checker"]["transcript"]
    text = _content_text(result)
    assert text.startswith("Checker status: completed")
    assert "not interpreted" in text or "NOT interpreted" in text
    assert '"statistics"' not in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_shows_solution_when_model_has_no_output_item(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A model with no explicit `output` item streams only the json section. The
    # MCP-visible text must still carry a human solution block (not just status and
    # statistics), so the solution never disappears from the model-visible content.
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(solution_obj_json_only({"x": 5, "_objective": 5})),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve maximize x;"},
    )

    assert _structured(result)["solution"] == {"x": 5}
    text = _content_text(result)
    assert "Stdout:" in text
    assert "x = 5" in text


@pytest.mark.asyncio
async def test_solve_minizinc_model_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;", "data": "n = 3;"},
    )

    assert calls[0]["data_contents"] == "n = 3;"
    assert _structured(result)["status"] == "satisfied"


@pytest.mark.asyncio
async def test_solve_minizinc_model_omits_visible_statistics_when_empty(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    assert _structured(result)["statistics"] == {}
    assert "Final answer requirement" not in _content_text(result)
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["solve_minizinc_model", "check_minizinc_model", "inspect_minizinc_model", "find_unsat_core"],
)
async def test_runtime_missing_surfaces_actionable_error(
    tool_name: str,
    fake_runtime_dir: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(tool_name, {"model": "solve satisfy;"})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    ["solve_minizinc_model", "check_minizinc_model", "inspect_minizinc_model", "find_unsat_core"],
)
async def test_empty_model_surfaces_actionable_error(
    tool_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(tool_name, {"model": ""})
    assert "empty" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "model"),
    [
        ("solve_minizinc_model", "solve satisfy;"),
        ("find_unsat_core", "constraint false;\nsolve satisfy;"),
    ],
)
async def test_non_positive_timeout_surfaces_actionable_error(
    tool_name: str,
    model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            tool_name,
            {"model": model, "timeout_ms": 0},
        )
    assert "positive" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_compile_error_returns_structured_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout="",
            stderr="MiniZinc: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


@pytest.mark.asyncio
async def test_solve_minizinc_model_forwards_search_flags_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_full_capabilities(monkeypatch, "cp-sat")
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..5: x;\nsolve satisfy;",
            "free_search": True,
            "parallel": 2,
            "random_seed": 7,
            "all_solutions": True,
        },
    )

    cmd = calls[0]["cmd"]
    assert "-f" in cmd and "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == "7"


@pytest.mark.asyncio
async def test_solve_minizinc_model_invalid_parallel_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for bad parallel")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "parallel": 0},
        )
    assert "parallel" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_model_unsupported_control_surfaces_mcp_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An unsupported gated control surfaces as an MCP error carrying the core
    # message (solver, control, flag); the solve subprocess never runs.
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("solve subprocess must not run for an unsupported control")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "free_search": True},
        )
    message = str(exc_info.value)
    assert "free_search" in message
    assert "cp-sat" in message
    assert "-f" in message


@pytest.mark.asyncio
async def test_submit_solve_job_rejects_unsupported_control(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # submit_solve_job rejects an unsupported control at admission, surfaced as an
    # MCP error, and no job is created.
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "submit_solve_job",
            {"model": "solve satisfy;", "free_search": True},
        )
    assert "free_search" in str(exc_info.value)

    listed = await mcp.call_tool("list_solve_jobs", {})
    assert _structured(listed)["result"] == []


@pytest.mark.asyncio
async def test_solve_minizinc_model_num_solutions_forwards_n_flag_for_supported_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_model",
        {
            "model": "var 1..5: x;\nsolve satisfy;",
            "solver": "org.chuffed.chuffed",
            "num_solutions": 2,
        },
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-n") + 1] == "2"


@pytest.mark.asyncio
async def test_solve_minizinc_model_num_solutions_rejected_for_default_solver(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_model",
            {"model": "solve satisfy;", "num_solutions": 2},
        )
    message = str(exc_info.value)
    assert "num_solutions" in message
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.asyncio
async def test_check_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "check_minizinc_model" in names

    tool = next(t for t in tools if t.name == "check_minizinc_model")
    properties = tool.input_schema.get("properties", {})
    assert {"model", "data", "solver", "timeout_ms"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_check_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(stdout="", stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["solver"] == "cp-sat"


@pytest.mark.asyncio
async def test_check_minizinc_model_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "int: n;\nvar 1..n: x;\nsolve satisfy;", "data": "n = 3;"},
    )

    assert calls[0]["data_contents"] == "n = 3;"
    assert _structured(result)["status"] == "ok"


@pytest.mark.asyncio
async def test_check_minizinc_model_compile_error_returns_structured_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "check_minizinc_model",
        {"model": "var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;"},
    )

    structured = _structured(result)
    assert structured["status"] == "error"
    assert "type error" in structured["stderr"]


_SOLVE_ONLY_CONTROLS = {"free_search", "parallel", "random_seed", "all_solutions", "num_solutions"}

# A single-line interface object as the managed binary emits under
# `--model-interface-only`: one required array param and one output var.
_INSPECT_STDOUT = (
    '{"type": "interface", "input": {"weight": {"type": "int", "dim": 1}}, '
    '"output": {"take": {"type": "bool", "dim": 1}}, "method": "max", '
    '"has_output_item": false, "included_files": [], "globals": []}'
)


@pytest.mark.asyncio
async def test_inspect_minizinc_model_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "inspect_minizinc_model" in names

    tool = next(t for t in tools if t.name == "inspect_minizinc_model")
    properties = tool.input_schema.get("properties", {})
    assert {"model", "data", "solver", "timeout_ms"} <= set(properties.keys())
    # Decision 8: inspection exposes no solve-only search controls.
    assert not (_SOLVE_ONLY_CONTROLS & set(properties.keys()))


@pytest.mark.asyncio
async def test_inspect_minizinc_files_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "inspect_minizinc_files" in names

    tool = next(t for t in tools if t.name == "inspect_minizinc_files")
    properties = tool.input_schema.get("properties", {})
    assert {"model_path", "data_path", "solver", "timeout_ms"} <= set(properties.keys())
    assert not (_SOLVE_ONLY_CONTROLS & set(properties.keys()))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["inspect_minizinc_model", "inspect_minizinc_files"])
async def test_inspect_tools_output_schema_advertises_field_names(tool_name: str) -> None:
    # Concern 3: with no Pydantic aliases the advertised outputSchema must list the
    # public field names base_type/is_set/is_optional for InterfaceType — never
    # MiniZinc's raw type/set/optional — so a client validating structured output
    # against it does not fail.
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    tool = next(t for t in tools if t.name == tool_name)
    assert tool.output_schema is not None
    type_props = tool.output_schema["$defs"]["InterfaceType"]["properties"]
    assert set(type_props) == {"base_type", "dim", "is_set", "is_optional"}
    assert "type" not in type_props
    assert "set" not in type_props


@pytest.mark.asyncio
async def test_inspect_minizinc_model_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(stdout=_INSPECT_STDOUT, stderr="", returncode=0)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "inspect_minizinc_model",
        {
            "model": "array[1..3] of int: weight;\narray[1..3] of var bool: take;\n"
            "solve maximize sum(i in 1..3)(weight[i] * take[i]);"
        },
    )

    structured = _structured(result)
    assert structured["status"] == "ok"
    assert structured["interface"]["method"] == "max"
    # The structured payload carries the public field names, matching the
    # advertised outputSchema (Concern 3).
    weight = structured["interface"]["required_parameters"]["weight"]
    assert set(weight) == {"base_type", "dim", "is_set", "is_optional"}
    assert weight["base_type"] == "int"
    assert weight["dim"] == 1


@pytest.mark.asyncio
async def test_find_unsat_core_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "find_unsat_core" in names

    tool = next(t for t in tools if t.name == "find_unsat_core")
    properties = tool.input_schema.get("properties", {})
    assert {"model", "data", "timeout_ms"} <= set(properties.keys())
    assert "solver" not in properties


@pytest.mark.asyncio
async def test_find_unsat_core_happy_path(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=UNSAT_CORE_STDOUT,
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    mcp = create_mcp_server()
    result = await mcp.call_tool("find_unsat_core", {"model": UNSAT_CORE_MODEL})

    structured = _structured(result)
    assert structured["status"] == "mus_found"
    assert len(structured["core"]) == 2


@pytest.mark.asyncio
async def test_find_unsat_core_threads_inline_data_to_runtime(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_data_run(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "find_unsat_core",
        {"model": UNSAT_CORE_MODEL, "data": "lo = 5;"},
    )

    assert calls[0]["data_contents"] == "lo = 5;"
    assert _structured(result)["status"] == "mus_found"


# --- path-based file tools -------------------------------------------------

_FILE_MODEL_SRC = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"


def _record_run_capturing_cwd(
    monkeypatch: pytest.MonkeyPatch, completed: ChildExecutionResult
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        calls.append({"cmd": list(args[0]), "cwd": str(args[1])})
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    return calls


@pytest.mark.asyncio
async def test_file_tools_are_listed_with_expected_properties() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    for name in ("solve_minizinc_files", "check_minizinc_files", "find_unsat_core_files"):
        assert name in by_name
    assert "solve_and_check_minizinc_files" not in by_name

    for name in ("solve_minizinc_files", "check_minizinc_files"):
        properties = by_name[name].input_schema.get("properties", {})
        assert {"model_path", "data_path", "solver", "timeout_ms"} <= set(properties.keys())
        # The two-mode flag was removed: file tools always run CLI-style.
        assert "allow_local_includes" not in properties

    # Search-control flags are solve-only at the MCP surface: present on the
    # solve file tool, absent from the compile-check file tool.
    _search_flags = {"free_search", "parallel", "random_seed", "all_solutions", "num_solutions"}
    solve_files_props = by_name["solve_minizinc_files"].input_schema.get("properties", {})
    assert _search_flags <= set(solve_files_props.keys())
    assert "checker_path" in solve_files_props
    check_files_props = by_name["check_minizinc_files"].input_schema.get("properties", {})
    assert not (_search_flags & set(check_files_props.keys()))
    assert "checker_path" not in check_files_props

    findmus_props = by_name["find_unsat_core_files"].input_schema.get("properties", {})
    assert {"model_path", "data_path", "timeout_ms"} <= set(findmus_props.keys())
    assert "solver" not in findmus_props
    assert "allow_local_includes" not in findmus_props


@pytest.mark.asyncio
async def test_solve_minizinc_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    structured = _structured(result)
    assert structured["status"] == "satisfied"
    assert structured["return_code"] == 0
    assert structured["timed_out"] is False
    assert structured["solution"] == {"x": 3}
    assert structured["solutions"] == [{"x": 3}]
    assert structured["objective"] is None
    assert structured["checker"] is None
    # The statistics field is exposed even when no statistics object was emitted.
    assert structured["statistics"] == {}
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
async def test_solve_minizinc_files_visible_content_includes_statistics(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                solution_obj("x = 3;\n", {"x": 3}),
                {"type": "statistics", "statistics": {"failures": 2}},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    assert _structured(result)["statistics"] == {"failures": "2"}
    text = _content_text(result)
    lower_text = text.lower()
    assert "Final answer requirement" in text
    assert "entire Statistics section" in text
    assert "do not omit" in lower_text
    assert "summarize" in lower_text
    assert "selected fields" in lower_text
    assert "Statistics:" in text
    assert "- failures: 2" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_forwards_search_flags_to_runtime(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _patch_full_capabilities(monkeypatch, "cp-sat")
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "parallel": 3, "all_solutions": True},
    )

    cmd = calls[0]["cmd"]
    assert "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "3"


@pytest.mark.asyncio
async def test_solve_minizinc_files_num_solutions_forwards_n_flag_for_supported_solver(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "solver": "org.gecode.gecode", "num_solutions": 2},
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-n") + 1] == "2"


@pytest.mark.asyncio
async def test_solve_minizinc_files_num_solutions_rejected_for_default_solver(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An EXISTING model file is required: path validation runs before the gate.
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "num_solutions": 2},
        )
    message = str(exc_info.value)
    assert "num_solutions" in message
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.asyncio
async def test_solve_minizinc_files_unsupported_control_surfaces_mcp_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The path solve tool surfaces the same capability rejection as the inline one.
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("solve subprocess must not run for an unsupported control")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "parallel": 2},
        )
    message = str(exc_info.value)
    assert "parallel" in message
    assert "-p" in message


@pytest.mark.asyncio
async def test_check_minizinc_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    _record_run_capturing_cwd(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    mcp = create_mcp_server()
    result = await mcp.call_tool("check_minizinc_files", {"model_path": str(model_path)})

    assert _structured(result)["status"] == "ok"


@pytest.mark.asyncio
async def test_find_unsat_core_files_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(UNSAT_CORE_MODEL)
    _record_run_capturing_cwd(
        monkeypatch, child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0)
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool("find_unsat_core_files", {"model_path": str(model_path)})

    structured = _structured(result)
    assert structured["status"] == "mus_found"
    assert len(structured["core"]) == 2


@pytest.mark.asyncio
async def test_solve_minizinc_files_runs_from_model_parent(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0),
    )

    mcp = create_mcp_server()
    await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    assert calls[0]["cwd"] == str(model_path.resolve().parent)


@pytest.mark.asyncio
async def test_solve_minizinc_files_path_not_found_surfaces_actionable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a missing path")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    missing = tmp_path / "nope.mzn"

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_files", {"model_path": str(missing)})

    message = str(exc_info.value)
    assert "does not exist" in message
    assert "nope.mzn" in message


@pytest.mark.asyncio
async def test_solve_minizinc_files_runtime_missing_surfaces_actionable_error(
    tmp_path: Path,
    fake_runtime_dir: Path,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_FILE_MODEL_SRC)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("solve_minizinc_files", {"model_path": str(model_path)})

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


# --- solution checker on solve_minizinc_files -------------------------------

_CHECKER_MODEL_SRC = "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;\n"
_CHECKER_SRC = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)


def _write_solve_and_check_inputs(tmp_path: Path) -> tuple[Path, Path]:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_CHECKER_MODEL_SRC)
    checker_path = tmp_path / "model.mzc.mzn"
    checker_path.write_text(_CHECKER_SRC)
    return model_path, checker_path


@pytest.mark.asyncio
async def test_solution_checker_is_folded_into_solve_minizinc_files() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    names = {tool.name for tool in tools}
    assert "solve_and_check_minizinc_files" not in names

    tool = next(t for t in tools if t.name == "solve_minizinc_files")
    properties = tool.input_schema.get("properties", {})
    assert {"model_path", "checker_path", "data_path", "solver", "timeout_ms"} <= set(properties)
    assert _SOLVE_ONLY_CONTROLS <= set(properties)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_happy_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    structured = _structured(result)
    assert structured["checker"]["status"] == "completed"
    assert structured["status"] == "satisfied"
    assert structured["checker"]["checks"] == [{"violation": False, "output": "CORRECT\n"}]
    # The raw transcript (checker objects intact) is the authoritative record.
    assert '"type": "checker"' in structured["checker"]["transcript"]
    # The model-visible text leads with the checker verdict and the solve status.
    text = _content_text(result)
    assert "completed" in text
    assert "satisfied" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_text_notes_author_text_not_adjudicated(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The model-visible text must warn that author CORRECT/INCORRECT text is NOT
    # interpreted by the server (only a constraint-style rejection is a violation),
    # so a client never treats the verbatim verdict as a server judgement.
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_pass("INCORRECT\n"),
                solution_obj("x = 3; y = 1;\n", {"x": 3, "y": 1}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    text = _content_text(result).lower()
    assert "not interpreted" in text or "not adjudicated" in text
    assert "checks" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_visible_content_includes_statistics(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The solve portion reuses format_solve_result_content, so the Statistics
    # section and its copy-entire-section requirement appear verbatim whenever the
    # nested solve carries statistics.
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "statistics", "statistics": {"failures": 2}},
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    assert _structured(result)["statistics"] == {"failures": "2"}
    text = _content_text(result)
    assert "entire Statistics section" in text
    assert "Statistics:" in text
    assert "- failures: 2" in text


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_omits_statistics_when_empty(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_pass("CORRECT\n"),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    assert _structured(result)["statistics"] == {}
    assert "Statistics:" not in _content_text(result)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_violation_surfaces_in_structured(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_violation(),
                solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2}),
                {"type": "status", "status": "SATISFIED"},
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    structured = _structured(result)
    assert structured["checker"]["status"] == "violation"
    assert structured["checker"]["checks"][0]["violation"] is True
    # The rejected solution stays in solutions (fact 5).
    assert structured["solutions"] == [{"x": 1, "y": 2}]


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_runs_from_model_parent_with_checker_flag(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)
    calls = _record_run_capturing_cwd(
        monkeypatch,
        child_result(
            stdout=stream(
                checker_pass("CORRECT\n"), solution_obj("x = 1; y = 2;\n", {"x": 1, "y": 2})
            ),
            stderr="",
            returncode=0,
        ),
    )

    mcp = create_mcp_server()
    await mcp.call_tool(
        "solve_minizinc_files",
        {"model_path": str(model_path), "checker_path": str(checker_path)},
    )

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--solution-checker") + 1] == str(checker_path.resolve())
    assert calls[0]["cwd"] == str(model_path.resolve().parent)


@pytest.mark.asyncio
async def test_solve_minizinc_files_bad_checker_suffix_surfaces_actionable_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_CHECKER_MODEL_SRC)
    bad_checker = tmp_path / "checker.mzn"  # wrong suffix
    bad_checker.write_text(_CHECKER_SRC)

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a bad checker suffix")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "checker_path": str(bad_checker)},
        )
    assert "mzc" in str(exc_info.value)


@pytest.mark.asyncio
async def test_solve_minizinc_files_with_checker_runtime_missing_surfaces_actionable_error(
    tmp_path: Path,
    fake_runtime_dir: Path,
) -> None:
    model_path, checker_path = _write_solve_and_check_inputs(tmp_path)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "solve_minizinc_files",
            {"model_path": str(model_path), "checker_path": str(checker_path)},
        )

    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


# --- _report_status: dual-channel (progress + log) milestone helper ---------


class _FakeStatusContext:
    """Records report_progress/info calls; optionally raises from both."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.progress_calls: list[tuple[float, float | None, str | None]] = []
        self.info_messages: list[str] = []
        self._fail_with = fail_with

    async def report_progress(
        self, progress: float, total: float | None = None, message: str | None = None
    ) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.progress_calls.append((progress, total, message))

    async def info(self, message: str, **extra: object) -> None:
        if self._fail_with is not None:
            raise self._fail_with
        self.info_messages.append(message)


@pytest.mark.asyncio
async def test_report_status_noops_without_context() -> None:
    await _report_status(None, 1, "validating request")


@pytest.mark.asyncio
async def test_report_status_sends_progress_without_total_by_default() -> None:
    ctx = _FakeStatusContext()

    await _report_status(ctx, 2, "solve is running")  # type: ignore[arg-type]

    assert ctx.progress_calls == [(2, None, "solve is running")]


@pytest.mark.asyncio
async def test_report_status_sends_info_log_with_same_message() -> None:
    ctx = _FakeStatusContext()

    await _report_status(ctx, 2, "solve is running")  # type: ignore[arg-type]

    assert ctx.info_messages == ["solve is running"]


@pytest.mark.asyncio
async def test_report_status_noops_when_request_context_unavailable() -> None:
    # MCPServer raises this exact ValueError for direct calls outside a real
    # JSON-RPC request (the mode every mcp.call_tool test in this file uses).
    ctx = _FakeStatusContext(fail_with=ValueError(_CONTEXT_UNAVAILABLE_MESSAGE))

    await _report_status(ctx, 1, "validating request")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_report_status_does_not_swallow_unrelated_value_error() -> None:
    boom = ValueError("unexpected internal error")
    ctx = _FakeStatusContext(fail_with=boom)

    with pytest.raises(ValueError) as exc_info:
        await _report_status(ctx, 1, "validating request")  # type: ignore[arg-type]

    assert exc_info.value is boom


# --- milestone progress: schema stability and per-family stage schedules ----


def _tool_fn(mcp: Any, name: str) -> Any:
    """Return a registered tool's underlying (decorated) function for direct calls."""
    return mcp._tool_manager.get_tool(name).fn


def _assert_dual_channel_schedule(ctx: _FakeStatusContext, final_message: str) -> None:
    """Assert the standard four-stage schedule went out on both channels."""
    assert [call[0] for call in ctx.progress_calls] == [1, 2, 3, 4]
    assert all(call[1] is None for call in ctx.progress_calls)
    assert ctx.progress_calls[-1][2] == final_message
    assert ctx.info_messages == [call[2] for call in ctx.progress_calls]


@pytest.mark.asyncio
async def test_tool_schemas_do_not_expose_context_or_progress_arguments() -> None:
    # The ctx parameter is protocol plumbing: MCPServer must keep it (and any
    # progress-token argument) out of every advertised tool input schema.
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    for tool in tools:
        properties = set(tool.input_schema.get("properties", {}).keys())
        assert not ({"ctx", "context", "progress_token", "progressToken"} & properties), tool.name


@pytest.mark.asyncio
async def test_solve_minizinc_model_reports_stage_schedule(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(solution_obj("x = 3;\n", {"x": 3})), stderr="", returncode=0
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "solve_minizinc_model")(
        model="var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", ctx=ctx
    )

    _assert_dual_channel_schedule(ctx, "Solve complete")
    assert ctx.progress_calls[1][2] == "MiniZinc solve is running"


@pytest.mark.asyncio
async def test_solve_minizinc_model_with_checker_reports_checker_stage_messages(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> ChildExecutionResult:
        return child_result(
            stdout=stream(checker_pass("CORRECT\n"), solution_obj("x = 3;\n", {"x": 3})),
            stderr="",
            returncode=0,
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "solve_minizinc_model")(
        model="var 1..5: x;\nconstraint x > 2;\nsolve satisfy;",
        checker='output ["ok"];',
        ctx=ctx,
    )

    assert ctx.progress_calls[1][2] == "MiniZinc solve with solution checker is running"
    assert ctx.progress_calls[2][2] == "MiniZinc finished; parsing solve and checker streams"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "fake_stdout", "model", "final_message"),
    [
        ("check_minizinc_model", "", "solve satisfy;", "Check complete"),
        ("inspect_minizinc_model", _INSPECT_STDOUT, "solve satisfy;", "Inspection complete"),
        ("find_unsat_core", UNSAT_CORE_STDOUT, UNSAT_CORE_MODEL, "Unsat-core analysis complete"),
    ],
    ids=["check_minizinc_model", "inspect_minizinc_model", "find_unsat_core"],
)
async def test_reports_stage_schedule(
    tool_name: str,
    fake_stdout: str,
    model: str,
    final_message: str,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *a, **k: child_result(stdout=fake_stdout, stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, tool_name)(model=model, ctx=ctx)

    _assert_dual_channel_schedule(ctx, final_message)


@pytest.mark.asyncio
async def test_check_minizinc_files_reports_stage_schedule(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text("solve satisfy;")
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *a, **k: child_result(stdout="", stderr="", returncode=0),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    await _tool_fn(mcp, "check_minizinc_files")(model_path=str(model_path), ctx=ctx)

    _assert_dual_channel_schedule(ctx, "Check complete")


@pytest.mark.asyncio
async def test_check_minizinc_files_missing_path_still_raises_after_early_stages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Path validation behavior is unchanged: the domain ValueError still
    # surfaces through the decorated tool as its translated RuntimeError; only
    # the pre-call stages were emitted before it fired.
    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError("subprocess.run must not be invoked for a missing path")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    with pytest.raises(RuntimeError) as exc_info:
        await _tool_fn(mcp, "check_minizinc_files")(model_path=str(tmp_path / "nope.mzn"), ctx=ctx)

    assert "does not exist" in str(exc_info.value)
    assert [call[0] for call in ctx.progress_calls] == [1, 2]


@pytest.mark.asyncio
async def test_check_minizinc_model_compile_error_still_reports_final_stage(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A compile error is a normal structured result, so the client still gets
    # the closing milestone before the response.
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *a, **k: child_result(
            stdout="", stderr="Error: type error: undefined identifier 'xz'\n", returncode=1
        ),
    )
    mcp = create_mcp_server()
    ctx = _FakeStatusContext()

    result = await _tool_fn(mcp, "check_minizinc_model")(
        model="var 1..5: x;\nconstraint xz > 2;\nsolve satisfy;", ctx=ctx
    )

    assert result.status == "error"
    assert ctx.progress_calls[-1] == (4, None, "Check complete")


@pytest.mark.asyncio
async def test_run_blocking_executes_off_the_event_loop_thread() -> None:
    # The blocking MiniZinc call must leave the event loop free, or queued
    # status notifications sit unwritten until the solve ends (PR #34 review).
    loop_thread = threading.get_ident()
    seen: list[int] = []

    def _blocking() -> str:
        seen.append(threading.get_ident())
        return "done"

    result = await _run_blocking(_blocking)

    assert result == "done"
    assert seen and seen[0] != loop_thread


# --- save_verified_minizinc_model -------------------------------------------


_SAVE_TOOL_MODEL = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"


def _fake_save_subprocess(
    monkeypatch: pytest.MonkeyPatch,
    *,
    check: ChildExecutionResult,
    solve: ChildExecutionResult,
) -> None:
    """Route the save tool's two managed runs: `-c` → ``check``, else ``solve``."""

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        cmd = [str(part) for part in args[0]]
        return check if "-c" in cmd else solve

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)


@pytest.mark.asyncio
async def test_save_verified_minizinc_model_is_listed_with_expected_schema() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    tool = next(t for t in tools if t.name == "save_verified_minizinc_model")
    properties = set(tool.input_schema["properties"])
    assert {
        "model",
        "target_dir",
        "data",
        "checker",
        "problem",
        "solver",
        "timeout_ms",
        "free_search",
        "parallel",
        "random_seed",
        "all_solutions",
        "num_solutions",
        "overwrite",
        "portfolio_result",
    } <= properties
    # The trailing Context parameter is server plumbing, never client schema.
    assert "ctx" not in properties
    assert set(tool.input_schema.get("required", [])) == {"model", "target_dir"}


@pytest.mark.asyncio
async def test_save_verified_minizinc_model_happy_path_saves(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_save_subprocess(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=stream(solution_obj("x=3\n", {"x": 3})), stderr="", returncode=0),
    )
    target = tmp_path / "saved-project"

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "save_verified_minizinc_model",
        {"model": _SAVE_TOOL_MODEL, "target_dir": str(target)},
    )

    structured = _structured(result)
    assert structured["status"] == "saved"
    assert structured["check"]["status"] == "ok"
    assert structured["solve"]["status"] == "satisfied"
    assert [entry["role"] for entry in structured["files"]] == [
        "model",
        "solve_result",
        "manifest",
    ]
    text = _content_text(result)
    assert str(target) in text
    assert "saved" in text
    assert (target / "model.mzn").read_text() == _SAVE_TOOL_MODEL


@pytest.mark.asyncio
async def test_save_verified_minizinc_model_not_verified_is_normal_result(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A failed verification gate is a structured outcome the client reasons
    # about — a normal tool result, not an MCP exception.
    _fake_save_subprocess(
        monkeypatch,
        check=child_result(
            stdout="", stderr="Error: type error: undefined identifier 'xz'\n", returncode=1
        ),
        solve=child_result(stdout="", stderr="", returncode=0),
    )
    target = tmp_path / "saved-project"

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "save_verified_minizinc_model",
        {"model": _SAVE_TOOL_MODEL, "target_dir": str(target)},
    )

    structured = _structured(result)
    assert structured["status"] == "not_verified"
    assert structured["check"]["status"] == "error"
    assert structured["solve"] is None
    assert structured["files"] == []
    assert "nothing was written" in _content_text(result)
    assert not target.exists()


@pytest.mark.asyncio
async def test_save_verified_minizinc_model_with_portfolio_result_writes_experiment_log(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A self-consistent `portfolio_result` reaches the core save and is persisted."""
    from openconstraint_mcp.schemas.portfolio import (
        PortfolioAttempt,
        PortfolioSolveControls,
        PortfolioSolveResult,
    )

    _fake_save_subprocess(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=stream(solution_obj("x=3\n", {"x": 3})), stderr="", returncode=0),
    )
    target = tmp_path / "saved-project-portfolio"
    winner_solve = SolveResult(
        status="satisfied",
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x = 3;\n",
        stderr="",
        elapsed_ms=5,
        solution={"x": 3},
        solutions=[{"x": 3}],
        objective=None,
    )
    portfolio_result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=winner_solve,
        attempts=[
            PortfolioAttempt(
                index=0,
                model_index=0,
                solver="cp-sat",
                seed=None,
                timeout_ms=5000,
                state="succeeded",
                job_id="job-0",
                job_state="succeeded",
                result_status="satisfied",
                objective=None,
                elapsed_ms=5,
            )
        ],
        elapsed_ms=10,
        selection_policy="first-decisive-result",
        models_sha256=[text_sha256(_SAVE_TOOL_MODEL)],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=PortfolioSolveControls(
            free_search=False, parallel=None, all_solutions=False, num_solutions=None
        ),
    )

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "save_verified_minizinc_model",
        {
            "model": _SAVE_TOOL_MODEL,
            "target_dir": str(target),
            "portfolio_result": portfolio_result.model_dump(mode="json"),
        },
    )

    structured = _structured(result)
    assert structured["status"] == "saved"
    assert "experiment_log" in {entry["role"] for entry in structured["files"]}
    assert (target / "experiment-log.json").is_file()


# --- background solve jobs (submit / get / cancel / list) -------------------

_JOB_TERMINAL_STATES = {"succeeded", "failed", "timeout", "cancelled"}


def _job_solve_result(status: str = "satisfied") -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="x = 1;\n",
        stderr="",
        elapsed_ms=3,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=None,
    )


class _JobFakeProc:
    """A process-handle stand-in; ``poll`` reports it as already exited."""

    def poll(self) -> int:
        return 0


def _patch_job_solve(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", fake)


async def _poll_job_status(
    mcp: Any, job_id: str, timeout: float = 3.0, get_tool: str = "get_solve_job"
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _structured(await mcp.call_tool(get_tool, {"job_id": job_id}))
        if status["state"] in _JOB_TERMINAL_STATES:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


@pytest.mark.asyncio
async def test_job_tools_are_listed_with_expected_properties() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    by_name = {tool.name: tool for tool in tools}

    for name in ("submit_solve_job", "get_solve_job", "cancel_solve_job", "list_solve_jobs"):
        assert name in by_name

    # submit mirrors solve_minizinc_model's inline surface and search controls.
    submit_props = by_name["submit_solve_job"].input_schema.get("properties", {})
    assert {
        "model",
        "data",
        "checker",
        "solver",
        "timeout_ms",
        "free_search",
        "parallel",
        "random_seed",
        "all_solutions",
        "num_solutions",
    } <= set(submit_props)

    for name in ("get_solve_job", "cancel_solve_job"):
        assert "job_id" in by_name[name].input_schema.get("properties", {})
    assert by_name["list_solve_jobs"].input_schema.get("properties", {}) == {}


@pytest.mark.asyncio
async def test_get_solve_job_description_mandates_surfacing_statistics() -> None:
    # A completed job's `result` is the only place solve statistics surface, so the
    # tool description must tell the client to present the full `Statistics:`
    # section on completion — the same mandate the synchronous solve tools carry —
    # rather than letting a finished job's statistics be silently dropped.
    mcp = create_mcp_server()
    by_name = {tool.name: tool for tool in await mcp.list_tools()}

    description = by_name["get_solve_job"].description or ""
    assert "Statistics:" in description


@pytest.mark.asyncio
async def test_get_solve_job_description_guides_polling_cadence() -> None:
    # A `running` job exposes no partial data, so a client that polls in a tight
    # loop (or guesses a fixed "sleep N") just burns calls. The description must
    # tell the client to pace its polling against the job's own `timeout_ms`
    # budget so the cadence is derived, not invented.
    mcp = create_mcp_server()
    by_name = {tool.name: tool for tool in await mcp.list_tools()}

    description = by_name["get_solve_job"].description or ""
    assert "timeout_ms" in description and "pace" in description.lower()


@pytest.mark.asyncio
async def test_submit_returns_running_or_queued_then_get_reaches_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_job_solve(monkeypatch, lambda model, *, on_start, **kw: _job_solve_result("optimal"))

    mcp = create_mcp_server()
    submitted = _structured(
        await mcp.call_tool("submit_solve_job", {"model": "var 1..5: x;\nsolve maximize x;"})
    )
    assert submitted["state"] in {"queued", "running", "succeeded"}
    assert submitted["solver"] == "cp-sat"
    job_id = submitted["job_id"]

    final = await _poll_job_status(mcp, job_id)
    assert final["state"] == "succeeded"
    assert final["result"]["status"] == "optimal"


@pytest.mark.asyncio
async def test_cancel_solve_job_terminates_running_job(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_JobFakeProc())
        started.set()
        release.wait(timeout=5)
        return _job_solve_result()

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()  # the "process" dying unblocks the worker

    _patch_job_solve(monkeypatch, _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    mcp = create_mcp_server()
    try:
        job_id = _structured(await mcp.call_tool("submit_solve_job", {"model": "solve satisfy;"}))[
            "job_id"
        ]
        assert started.wait(timeout=3)
        await mcp.call_tool("cancel_solve_job", {"job_id": job_id})

        final = await _poll_job_status(mcp, job_id)
        assert final["state"] == "cancelled"
        assert final["result"] is None
        assert terminated  # the running child's process tree was signalled
    finally:
        release.set()


@pytest.mark.asyncio
async def test_get_solve_job_unknown_id_surfaces_actionable_error() -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("get_solve_job", {"job_id": "does-not-exist"})
    assert "unknown" in str(exc_info.value)


@pytest.mark.asyncio
async def test_submit_solve_job_queue_full_surfaces_actionable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(self: Any, **kwargs: Any) -> str:
        raise JobRejectedError("Job queue is full (4 running + 16 queued).")

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.JobRegistry.submit", _raise)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("submit_solve_job", {"model": "solve satisfy;"})
    assert "queue is full" in str(exc_info.value)


@pytest.mark.asyncio
async def test_submit_solve_job_invalid_num_solutions_surfaces_error_before_any_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The cp-sat + num_solutions gate runs synchronously in submit, before a job
    # exists; no worker/solve is created.
    def _fail_if_called(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("solve must not run when submit validation rejects the controls")

    _patch_job_solve(monkeypatch, _fail_if_called)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "submit_solve_job",
            {"model": "solve satisfy;", "num_solutions": 2},
        )
    message = str(exc_info.value)
    assert "num_solutions" in message
    assert _structured(await mcp.call_tool("list_solve_jobs", {}))["result"] == []


@pytest.mark.asyncio
async def test_list_solve_jobs_returns_one_entry_per_submitted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_job_solve(monkeypatch, lambda model, *, on_start, **kw: _job_solve_result())

    mcp = create_mcp_server()
    ids: set[str] = set()
    for _ in range(2):
        job_id = _structured(await mcp.call_tool("submit_solve_job", {"model": "solve satisfy;"}))[
            "job_id"
        ]
        ids.add(job_id)
        await _poll_job_status(mcp, job_id)

    listed = _structured(await mcp.call_tool("list_solve_jobs", {}))["result"]
    assert {entry["job_id"] for entry in listed} == ids


# --- portfolio tool test helpers -------------------------------------------


class _PortfolioFakeProc:
    """An opaque process-handle stand-in; ``poll`` reports it as already exited.

    It has no ``pid`` and backs no real process, so the real (group-aware)
    ``_terminate_process_tree`` must never see it. Any test racing MORE THAN ONE
    attempt must therefore request ``_never_terminate_for_real`` below (or patch
    a recorder of its own): winner selection cancels the still-running losers,
    and whether a loser is still mid-flight when the winner is polled is a
    timing race the test cannot control — it loses locally and wins under CI
    load. A single-attempt race never cancels, so it needs no guard.
    """

    def poll(self) -> int:
        return 0


@pytest.fixture
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Disarm real process-tree termination for tests using ``_PortfolioFakeProc``."""
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry._terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _portfolio_solve_result(status: str, solver: str) -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver=solver,
        return_code=0,
        timed_out=False,
        stdout=f"{solver} result\n",
        stderr="",
        elapsed_ms=2,
        solution={"x": 1},
        solutions=[{"x": 1}],
        objective=22 if status == "optimal" else None,
    )


# --- registry bounds via env vars ------------------------------------------


_REGISTRY_BOUND_ENV_VARS = (
    "OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS",
    "OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS",
    "OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL",
)


def _spy_registry_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Capture the kwargs ``create_mcp_server`` passes to ``JobRegistry``."""
    captured: dict[str, Any] = {}
    real_init = JobRegistry.__init__

    def _spy(self: JobRegistry, **kwargs: Any) -> None:
        captured.update(kwargs)
        real_init(self, **kwargs)

    monkeypatch.setattr("openconstraint_mcp.server.JobRegistry.__init__", _spy)
    return captured


def test_registry_bounds_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _REGISTRY_BOUND_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    captured = _spy_registry_kwargs(monkeypatch)
    create_mcp_server()
    assert captured == {
        "max_running_jobs": 4,
        "max_queued_jobs": 16,
        "max_retained_terminal": 64,
    }


def test_registry_bounds_read_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", "8")
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS", "2")
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_RETAINED_TERMINAL", "10")
    captured = _spy_registry_kwargs(monkeypatch)
    create_mcp_server()
    assert captured == {
        "max_running_jobs": 8,
        "max_queued_jobs": 2,
        "max_retained_terminal": 10,
    }


def test_registry_bounds_reject_non_integer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", "eight")
    with pytest.raises(ValueError, match="OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS must be an integer"):
        create_mcp_server()


def test_registry_bounds_reject_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    # Names the env var, NOT the bare constructor "max_running_jobs must be >= 1".
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", "0")
    with pytest.raises(ValueError, match=r"OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS must be >= 1"):
        create_mcp_server()


# --- background portfolio jobs (submit/get/cancel/list) --------------------


async def _poll_portfolio_status(mcp: Any, job_id: str, timeout: float = 5.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = _structured(await mcp.call_tool("get_portfolio_job", {"job_id": job_id}))
        if status["state"] in {"succeeded", "cancelled"}:
            return status
        await asyncio.sleep(0.01)
    raise AssertionError(f"portfolio job {job_id} did not finish within {timeout}s")


@pytest.mark.asyncio
async def test_portfolio_job_tools_are_listed_with_expected_properties() -> None:
    mcp = create_mcp_server()
    by_name = {tool.name: tool for tool in await mcp.list_tools()}

    for name in (
        "submit_portfolio_job",
        "get_portfolio_job",
        "cancel_portfolio_job",
        "list_portfolio_jobs",
    ):
        assert name in by_name

    submit_props = by_name["submit_portfolio_job"].input_schema.get("properties", {})
    assert {
        "models",
        "solvers",
        "data",
        "checker",
        "seed_count",
        "seeds",
        "per_attempt_timeout_ms",
        "free_search",
        "parallel",
        "all_solutions",
        "num_solutions",
    } <= set(submit_props)

    for name in ("get_portfolio_job", "cancel_portfolio_job"):
        assert "job_id" in by_name[name].input_schema.get("properties", {})
    assert by_name["list_portfolio_jobs"].input_schema.get("properties", {}) == {}


@pytest.mark.asyncio
async def test_submit_portfolio_job_returns_running_then_get_reaches_succeeded(
    monkeypatch: pytest.MonkeyPatch,
    _never_terminate_for_real: None,
) -> None:
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_PortfolioFakeProc())
        return _portfolio_solve_result("optimal", solver)

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _fake_solve)

    mcp = create_mcp_server()
    submitted = _structured(
        await mcp.call_tool(
            "submit_portfolio_job",
            {"models": ["solve satisfy;"], "solvers": ["cp-sat", "org.gecode.gecode"]},
        )
    )
    # The first poll inside submit may already have selected a winner on a fast solve.
    assert submitted["state"] in {"running", "succeeded"}
    job_id = submitted["job_id"]

    final = await _poll_portfolio_status(mcp, job_id)
    assert final["state"] == "succeeded"
    assert final["result"]["status"] == "winner"
    assert final["result"]["winner"]["status"] == "optimal"


@pytest.mark.asyncio
async def test_background_portfolio_provenance_threads_to_save(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    model = _SAVE_TOOL_MODEL
    data = "n = 3;\n"
    checker = "% portfolio checker\n"
    _patch_full_capabilities(monkeypatch, "cp-sat")

    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_PortfolioFakeProc())
        return _portfolio_solve_result("satisfied", solver)

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _fake_solve)

    mcp = create_mcp_server()
    submitted = _structured(
        await mcp.call_tool(
            "submit_portfolio_job",
            {
                "models": [model],
                "solvers": ["cp-sat"],
                "data": data,
                "checker": checker,
                "seeds": [42],
                "free_search": True,
                "parallel": 2,
            },
        )
    )
    final = (
        submitted
        if submitted["state"] == "succeeded"
        else await _poll_portfolio_status(mcp, submitted["job_id"])
    )
    portfolio_result = final["result"]

    assert portfolio_result["models_sha256"] == [text_sha256(model)]
    assert portfolio_result["data_sha256"] == text_sha256(data)
    assert portfolio_result["checker_sha256"] == text_sha256(checker)
    assert portfolio_result["solve_controls"] == {
        "free_search": True,
        "parallel": 2,
        "all_solutions": False,
        "num_solutions": None,
    }

    _fake_save_subprocess(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=stream(solution_obj("x=3\n", {"x": 3})), stderr="", returncode=0),
    )
    target = tmp_path / "portfolio-provenance-save"
    save_result = _structured(
        await mcp.call_tool(
            "save_verified_minizinc_model",
            {
                "model": model,
                "data": data,
                "solver": "cp-sat",
                "random_seed": 42,
                "free_search": True,
                "parallel": 2,
                "target_dir": str(target),
                "portfolio_result": portfolio_result,
            },
        )
    )

    assert save_result["status"] == "saved"
    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert log["models_sha256"] == [text_sha256(model)]
    assert log["data_sha256"] == text_sha256(data)
    assert log["checker_sha256"] == text_sha256(checker)
    assert log["solve_controls"] == {
        "free_search": True,
        "parallel": 2,
        "all_solutions": False,
        "num_solutions": None,
    }


@pytest.mark.asyncio
async def test_submit_portfolio_job_unsupported_control_surfaces_mcp_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})

    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run for an unsupported control")

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _fail)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "submit_portfolio_job",
            {"models": ["solve satisfy;"], "solvers": ["cp-sat"], "free_search": True},
        )
    assert "free_search" in str(exc_info.value)
    # No portfolio job and no attempt job were created (admission rejected).
    assert _structured(await mcp.call_tool("list_portfolio_jobs", {}))["result"] == []
    assert _structured(await mcp.call_tool("list_solve_jobs", {}))["result"] == []


@pytest.mark.asyncio
async def test_submit_portfolio_job_rejects_plan_exceeding_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_RUNNING_JOBS", "1")
    monkeypatch.setenv("OPENCONSTRAINT_MCP_MAX_QUEUED_JOBS", "0")

    def _fail(model: str, *, on_start: Any, **kw: Any) -> SolveResult:
        raise AssertionError("no solve should run when the batch exceeds capacity")

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _fail)

    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool(
            "submit_portfolio_job",
            {"models": ["solve satisfy;"], "solvers": ["cp-sat", "org.gecode.gecode"]},
        )
    assert "capacity" in str(exc_info.value)
    assert _structured(await mcp.call_tool("list_portfolio_jobs", {}))["result"] == []


@pytest.mark.asyncio
async def test_cancel_portfolio_job_stops_running_race(monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    release = threading.Event()
    terminated: list[Any] = []

    def _blocking_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_PortfolioFakeProc())
        started.set()
        release.wait(timeout=5)
        return _portfolio_solve_result("satisfied", solver)

    def _fake_terminate(proc: Any, **kwargs: Any) -> None:
        terminated.append(proc)
        release.set()

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _blocking_solve)
    monkeypatch.setattr("openconstraint_mcp.jobs.registry._terminate_process_tree", _fake_terminate)

    mcp = create_mcp_server()
    try:
        job_id = _structured(
            await mcp.call_tool(
                "submit_portfolio_job",
                {"models": ["solve satisfy;"], "solvers": ["cp-sat"]},
            )
        )["job_id"]
        assert started.wait(timeout=3)
        await mcp.call_tool("cancel_portfolio_job", {"job_id": job_id})

        final = await _poll_portfolio_status(mcp, job_id)
        assert final["state"] == "cancelled"
        assert final["result"] is None
        assert terminated  # the attempt's process tree was signalled
    finally:
        release.set()


@pytest.mark.asyncio
async def test_list_portfolio_jobs_returns_one_entry_per_submitted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_solve(model: str, *, solver: str, on_start: Any, **kw: Any) -> SolveResult:
        on_start(_PortfolioFakeProc())
        return _portfolio_solve_result("optimal", solver)

    monkeypatch.setattr("openconstraint_mcp.jobs.registry.solve_model_cancellable", _fake_solve)

    mcp = create_mcp_server()
    ids: set[str] = set()
    for _ in range(2):
        job_id = _structured(
            await mcp.call_tool(
                "submit_portfolio_job",
                {"models": ["solve satisfy;"], "solvers": ["cp-sat"]},
            )
        )["job_id"]
        ids.add(job_id)
        await _poll_portfolio_status(mcp, job_id)

    listed = _structured(await mcp.call_tool("list_portfolio_jobs", {}))["result"]
    assert {entry["job_id"] for entry in listed} == ids


@pytest.mark.asyncio
async def test_get_portfolio_job_unknown_id_surfaces_actionable_error() -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception) as exc_info:
        await mcp.call_tool("get_portfolio_job", {"job_id": "does-not-exist"})
    assert "unknown" in str(exc_info.value)


# --- run_cpsat_python ---------------------------------------------------------


@pytest.mark.asyncio
async def test_run_cpsat_python_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "run_cpsat_python" in names


@pytest.mark.asyncio
async def test_run_cpsat_python_routes_to_cpsat_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = CpsatPythonResult(
        status="optimal",
        solution={"x": 3},
        objective=3,
        stdout='{"status":"optimal","objective":3,"solution":{"x":3}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    monkeypatch.setattr(
        "openconstraint_mcp.server.run_cpsat_python",
        lambda source, **kw: fake_result,
    )

    mcp = create_mcp_server()
    result = _structured(await mcp.call_tool("run_cpsat_python", {"source": "print('hi')"}))
    assert result["status"] == "optimal"
    assert result["solution"] == {"x": 3}


@pytest.mark.asyncio
async def test_run_cpsat_python_surfaces_the_violated_envelope_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # End-to-end through the real executor with only the child mocked: the
    # field-specific contract error must survive tool serialization, since the
    # diagnostic is its only client-visible transport.
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.execute_child",
        lambda *a, **kw: ChildExecutionResult(
            stdout='{"status": "optimal", "solution": {"x": 1}}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=5,
        ),
    )

    mcp = create_mcp_server()
    result = _structured(await mcp.call_tool("run_cpsat_python", {"source": "print('hi')"}))
    assert result["status"] == "error"
    assert result["diagnostic"]["details"]["field"] == "objective"


@pytest.mark.asyncio
async def test_run_cpsat_python_surfaces_a_nested_non_finite_key_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The nested-finiteness gate has to survive tool serialization too, and the
    # key path is the whole repair signal — naming `solution` alone would leave a
    # client hunting through its own payload for the offending value.
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.core.execute_child",
        lambda *a, **kw: ChildExecutionResult(
            stdout='{"status": "optimal", "objective": 1, "solution": {"t": [{"s": NaN}]}}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=5,
        ),
    )

    mcp = create_mcp_server()
    result = _structured(await mcp.call_tool("run_cpsat_python", {"source": "print('hi')"}))
    assert result["status"] == "error"
    assert result["diagnostic"]["details"]["field"] == 'solution["t"][0]["s"]'


@pytest.mark.asyncio
async def test_run_cpsat_python_clears_both_cpsat_protocol_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This inline tool has no MCP-facing seed/config: it must always clear both
    # protocol env vars for the child rather than let a value the server process
    # happens to have inherited from its own launch environment leak in.
    seen: dict[str, object] = {}

    def _fake(source: str, **kw: object) -> Any:
        seen["env"] = kw.get("env")
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python", _fake)
    mcp = create_mcp_server()
    await mcp.call_tool("run_cpsat_python", {"source": "print('hi')"})
    assert seen["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


# --- run_cpsat_python_experiment ---------------------------------------------


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "run_cpsat_python_experiment" in names


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_routes_to_experiment_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = CpsatPythonResult(
        status="optimal",
        solution={"x": 4},
        objective=4,
        stdout='{"status":"optimal","objective":4,"solution":{"x":4}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    fake_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=winner,
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="baseline",
                seed=7,
                config_sha256=None,
                source_sha256="abc123",
                script_timeout_ms=1000,
                status="optimal",
                objective=4,
                accepted=True,
                timed_out=False,
                truncated=False,
                duration_ms=42,
            )
        ],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["abc123"],
    )
    seen: dict[str, object] = {}

    def _fake(
        attempts: list[CpsatPythonExperimentAttempt],
        **kw: object,
    ) -> CpsatPythonExperimentResult:
        seen["attempts"] = attempts
        seen.update(kw)
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_experiment", _fake)

    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "run_cpsat_python_experiment",
            {
                "attempts": [{"name": "baseline", "source": "print('hi')", "seed": 7}],
                "objective_sense": "maximize",
            },
        )
    )

    assert result["status"] == "winner"
    assert result["winner_name"] == "baseline"
    assert result["winner"]["solution"] == {"x": 4}
    assert seen["objective_sense"] == "maximize"
    assert seen["attempts"] == [
        CpsatPythonExperimentAttempt(name="baseline", source="print('hi')", seed=7)
    ]
    assert "Warnings:" not in _content_text(
        await mcp.call_tool(
            "run_cpsat_python_experiment",
            {
                "attempts": [{"name": "baseline", "source": "print('hi')", "seed": 7}],
                "objective_sense": "maximize",
            },
        )
    )


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_forwards_script_path_attempt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A script_path/args attempt survives the MCP schema boundary intact."""
    script = tmp_path / "model.py"
    script.write_text("print('hi')\n", encoding="utf-8")
    fake_result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[],
        elapsed_ms=1,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
        source_sha256=[],
    )
    seen: dict[str, object] = {}

    def _fake(
        attempts: list[CpsatPythonExperimentAttempt], **kw: object
    ) -> CpsatPythonExperimentResult:
        seen["attempts"] = attempts
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_experiment", _fake)

    mcp = create_mcp_server()
    await mcp.call_tool(
        "run_cpsat_python_experiment",
        {
            "attempts": [
                {"name": "from_file", "script_path": str(script), "args": ["data_ft10.json"]}
            ]
        },
    )

    assert seen["attempts"] == [
        CpsatPythonExperimentAttempt(
            name="from_file", script_path=str(script), args=["data_ft10.json"]
        )
    ]


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_forwards_include_winner_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = CpsatPythonResult(
        status="optimal",
        solution={"x": 4},
        objective=4,
        stdout='{"status":"optimal","objective":4,"solution":{"x":4}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    fake_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=winner,
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="baseline",
                seed=7,
                config_sha256=None,
                source_sha256="abc123",
                script_timeout_ms=1000,
                status="optimal",
                objective=4,
                accepted=True,
                timed_out=False,
                truncated=False,
                duration_ms=42,
            )
        ],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["abc123"],
    )
    seen: dict[str, object] = {}

    def _fake(
        attempts: list[CpsatPythonExperimentAttempt],
        **kw: object,
    ) -> CpsatPythonExperimentResult:
        seen["attempts"] = attempts
        seen.update(kw)
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_experiment", _fake)

    mcp = create_mcp_server()
    await mcp.call_tool(
        "run_cpsat_python_experiment",
        {
            "attempts": [{"name": "baseline", "source": "print('hi')", "seed": 7}],
            "objective_sense": "maximize",
            "include_winner_stdout": False,
        },
    )

    assert seen["include_winner_stdout"] is False


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_prints_warnings_section(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    winner = CpsatPythonResult(
        status="optimal",
        solution={"x": 4},
        objective=4,
        stdout='{"status":"optimal","objective":4,"solution":{"x":4}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    warning = (
        "max_parallel_attempts=2 combined with attempt(s) 'a' (num_workers=8) may "
        "request up to 16 CP-SAT workers, exceeding this machine's cpu_count=4; "
        "consider lowering num_workers or max_parallel_attempts."
    )
    fake_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=winner,
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="baseline",
                seed=7,
                config_sha256=None,
                source_sha256="abc123",
                script_timeout_ms=1000,
                status="optimal",
                objective=4,
                accepted=True,
                timed_out=False,
                truncated=False,
                duration_ms=42,
            )
        ],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["abc123"],
        warnings=[warning],
    )

    def _fake(
        attempts: list[CpsatPythonExperimentAttempt],
        **kw: object,
    ) -> CpsatPythonExperimentResult:
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_experiment", _fake)

    mcp = create_mcp_server()
    text = _content_text(
        await mcp.call_tool(
            "run_cpsat_python_experiment",
            {
                "attempts": [{"name": "baseline", "source": "print('hi')", "seed": 7}],
                "objective_sense": "maximize",
            },
        )
    )

    assert "Warnings:" in text
    assert warning in text


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_attempt_line_shows_best_objective_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unknown/rejected attempt's best_objective_bound must be visible in the
    plain-text attempt table too, not just structuredContent — otherwise a
    text-only client loses the diagnostic this field exists for."""
    fake_result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="baseline",
                seed=None,
                config_sha256=None,
                source_sha256="abc123",
                script_timeout_ms=1000,
                status="unknown",
                objective=None,
                best_objective_bound=7,
                accepted=False,
                message="status='unknown'",
                timed_out=False,
                truncated=False,
                duration_ms=42,
            )
        ],
        elapsed_ms=42,
        objective_sense="maximize",
        selection_policy="best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order",
        source_sha256=["abc123"],
    )

    def _fake(
        attempts: list[CpsatPythonExperimentAttempt],
        **kw: object,
    ) -> CpsatPythonExperimentResult:
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_experiment", _fake)

    mcp = create_mcp_server()
    text = _content_text(
        await mcp.call_tool(
            "run_cpsat_python_experiment",
            {
                "attempts": [{"name": "baseline", "source": "print('hi')"}],
                "objective_sense": "maximize",
            },
        )
    )

    assert "best_bound=7" in text


# --- run_cpsat_python_file ----------------------------------------------------


@pytest.mark.asyncio
async def test_run_cpsat_python_file_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "run_cpsat_python_file" in names


@pytest.mark.asyncio
async def test_run_cpsat_python_file_routes_path_to_cpsat_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_result = CpsatPythonResult(
        status="optimal",
        solution={"x": 3},
        objective=3,
        stdout='{"status":"optimal","objective":3,"solution":{"x":3}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=42,
    )
    seen: dict[str, object] = {}

    def _fake(script_path: Path, **kw: object) -> CpsatPythonResult:
        seen["script_path"] = script_path
        return fake_result

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file", _fake)

    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool("run_cpsat_python_file", {"script_path": "/tmp/model.py"})
    )
    assert result["status"] == "optimal"
    assert result["solution"] == {"x": 3}
    # The string path is wrapped in a Path before reaching the executor.
    assert seen["script_path"] == Path("/tmp/model.py")


@pytest.mark.asyncio
async def test_run_cpsat_python_file_tool_schema_includes_seed_and_config() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "run_cpsat_python_file")
    props = set(tool.input_schema["properties"])
    assert {"seed", "config"} <= props


@pytest.mark.asyncio
async def test_run_cpsat_python_file_seed_replay_passes_seed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake(script_path: Path, **kw: object) -> Any:
        seen["env"] = kw.get("env")
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file", _fake)
    mcp = create_mcp_server()
    await mcp.call_tool("run_cpsat_python_file", {"script_path": "/tmp/model.py", "seed": 7})
    assert seen["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


@pytest.mark.asyncio
async def test_run_cpsat_python_file_forwards_args_to_the_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake(script_path: Path, **kw: object) -> Any:
        seen["args"] = kw.get("args")
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file", _fake)
    mcp = create_mcp_server()
    await mcp.call_tool(
        "run_cpsat_python_file",
        {"script_path": "/tmp/model.py", "args": ["data_ft10.json"]},
    )
    assert seen["args"] == ["data_ft10.json"]


@pytest.mark.asyncio
async def test_run_cpsat_python_file_config_replay_passes_config_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def _fake(script_path: Path, **kw: object) -> Any:
        env = kw.get("env")
        assert env is not None
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])  # type: ignore[index]
        captured["seed_env"] = env["OPENCONSTRAINT_MCP_CPSAT_SEED"]  # type: ignore[index]
        captured["config_contents"] = json.loads(config_path.read_text())
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file", _fake)
    mcp = create_mcp_server()
    await mcp.call_tool(
        "run_cpsat_python_file",
        {"script_path": "/tmp/model.py", "config": {"num_workers": 2}},
    )
    assert captured["seed_env"] is None
    assert captured["config_contents"] == {"num_workers": 2}


@pytest.mark.asyncio
async def test_run_cpsat_python_file_without_seed_or_config_clears_both_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def _fake(script_path: Path, **kw: object) -> Any:
        seen["env"] = kw.get("env")
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file", _fake)
    mcp = create_mcp_server()
    await mcp.call_tool("run_cpsat_python_file", {"script_path": "/tmp/model.py"})
    assert seen["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


# --- run_cpsat_python_file_checked --------------------------------------------


def _fake_checked_result() -> CpsatPythonCheckedResult:
    return CpsatPythonCheckedResult(
        status="optimal",
        solution={"x": 3},
        objective=3,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=7,
        checker=_fake_checker_report(),
        checker_skipped_reason=None,
        checker_timeout_ms=30_000,
    )


def _patch_checked_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    """Stub the server-level checked runner; return the recorded call."""
    seen: dict[str, Any] = {}

    def _fake(script_path: Path, checker_path: Path, **kw: Any) -> CpsatPythonCheckedResult:
        seen["script_path"] = script_path
        seen["checker_path"] = checker_path
        seen.update(kw)
        return _fake_checked_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file_checked", _fake)
    return seen


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_tool_is_listed() -> None:
    mcp = create_mcp_server("full")
    tools = await mcp.list_tools()
    names = {tool.name for tool in tools}
    assert "run_cpsat_python_file_checked" in names


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_routes_result_to_the_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    result = _structured(
        await mcp.call_tool(
            "run_cpsat_python_file_checked",
            {"script_path": "/tmp/model.py", "checker_path": "/tmp/checker.py"},
        )
    )

    assert result["status"] == "optimal"


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_keeps_script_and_checker_paths_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A swap of the two positional paths would run the checker as the model."""
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {"script_path": "/tmp/model.py", "checker_path": "/tmp/checker.py"},
    )

    assert (seen["script_path"], seen["checker_path"]) == (
        Path("/tmp/model.py"),
        Path("/tmp/checker.py"),
    )


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_forwards_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": "/tmp/model.py",
            "checker_path": "/tmp/checker.py",
            "args": ["data_ft20.json"],
        },
    )

    assert seen["args"] == ["data_ft20.json"]


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_forwards_problem_and_checker_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": "/tmp/model.py",
            "checker_path": "/tmp/checker.py",
            "problem": "20 jobs, 5 machines",
            "checker_timeout_ms": 5_000,
        },
    )

    assert (seen["problem"], seen["checker_timeout_ms"]) == ("20 jobs, 5 machines", 5_000)


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_forwards_test_checker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dropping `test_checker=` on the inner call would silently return a null
    # `checker_test` for a caller who asked for the checker self-test.
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": "/tmp/model.py",
            "checker_path": "/tmp/checker.py",
            "test_checker": True,
        },
    )

    assert seen["test_checker"] is True


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_defaults_test_checker_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {"script_path": "/tmp/model.py", "checker_path": "/tmp/checker.py"},
    )

    assert seen["test_checker"] is False


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_seed_replay_passes_seed_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {"script_path": "/tmp/model.py", "checker_path": "/tmp/checker.py", "seed": 7},
    )

    assert seen["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_config_replay_passes_config_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake(script_path: Path, checker_path: Path, **kw: Any) -> CpsatPythonCheckedResult:
        env = kw["env"]
        captured["seed_env"] = env["OPENCONSTRAINT_MCP_CPSAT_SEED"]
        captured["config_contents"] = json.loads(
            Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"]).read_text()
        )
        return _fake_checked_result()

    monkeypatch.setattr("openconstraint_mcp.server.run_cpsat_python_file_checked", _fake)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": "/tmp/model.py",
            "checker_path": "/tmp/checker.py",
            "config": {"num_workers": 2},
        },
    )

    assert captured["seed_env"] is None
    assert captured["config_contents"] == {"num_workers": 2}


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_without_seed_or_config_clears_both_env_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _patch_checked_runner(monkeypatch)

    mcp = create_mcp_server("full")
    await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {"script_path": "/tmp/model.py", "checker_path": "/tmp/checker.py"},
    )

    assert seen["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


# --- CP-SAT background job tool smoke tests ---------------------------------


def _fake_cpsat_result(status: str = "optimal") -> Any:
    return CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution={"x": 1},
        objective=None,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=10,
    )


def _fake_checker_report() -> Any:
    from openconstraint_mcp.schemas.cpsat import CpsatCheckerReport

    return CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


@pytest.mark.asyncio
async def test_submit_cpsat_python_job_returns_job_id_and_queued_or_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python",
        lambda source, *, on_start, **kw: _fake_cpsat_result(),
    )
    mcp = create_mcp_server()
    result = _structured(await mcp.call_tool("submit_cpsat_python_job", {"source": "x=1"}))
    assert "job_id" in result
    # A very fast job may already be terminal by the time submit reads status,
    # matching the MiniZinc submit-job test convention.
    assert result["state"] in {"queued", "running", "succeeded"}


@pytest.mark.asyncio
async def test_submit_cpsat_python_file_job_returns_job_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python_file",
        lambda path, *, on_start, **kw: _fake_cpsat_result(),
    )
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool("submit_cpsat_python_file_job", {"script_path": str(script)})
    )
    assert "job_id" in result
    # A very fast job may already be terminal by the time submit reads status,
    # matching the MiniZinc submit-job test convention.
    assert result["state"] in {"queued", "running", "succeeded"}


@pytest.mark.asyncio
async def test_submit_cpsat_python_file_job_forwards_args_to_the_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    seen: dict[str, object] = {}

    def _fake(path: Path, *, on_start: Any, **kw: object) -> Any:
        seen["args"] = kw.get("args")
        return _fake_cpsat_result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_cpsat_python_file", _fake)
    mcp = create_mcp_server()
    submitted = _structured(
        await mcp.call_tool(
            "submit_cpsat_python_file_job",
            {"script_path": str(script), "args": ["data_ft10.json"]},
        )
    )
    # The run happens on a worker thread, so the kwarg is only settled once the
    # job is terminal — asserting straight off the submit response would race it.
    await _poll_job_status(mcp, submitted["job_id"], get_tool="get_cpsat_python_job")
    assert seen["args"] == ["data_ft10.json"]


@pytest.mark.asyncio
async def test_get_cpsat_python_job_reflects_submitted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python",
        lambda source, *, on_start, **kw: _fake_cpsat_result(),
    )
    mcp = create_mcp_server()
    submit_result = _structured(await mcp.call_tool("submit_cpsat_python_job", {"source": "x=1"}))
    job_id = submit_result["job_id"]

    get_result = _structured(await mcp.call_tool("get_cpsat_python_job", {"job_id": job_id}))
    assert get_result["job_id"] == job_id


@pytest.mark.asyncio
async def test_list_cpsat_python_jobs_reflects_submitted_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python",
        lambda source, *, on_start, **kw: _fake_cpsat_result(),
    )
    mcp = create_mcp_server()
    submit_result = _structured(await mcp.call_tool("submit_cpsat_python_job", {"source": "x=1"}))
    job_id = submit_result["job_id"]

    list_data = _structured(await mcp.call_tool("list_cpsat_python_jobs", {}))["result"]
    job_ids = [j["job_id"] for j in list_data]
    assert job_id in job_ids


@pytest.mark.asyncio
async def test_get_cpsat_python_job_unknown_id_surfaces_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="unknown job_id"):
        await mcp.call_tool("get_cpsat_python_job", {"job_id": "no-such-id"})


@pytest.mark.asyncio
async def test_submit_cpsat_python_job_accepts_checker_inputs_and_echoes_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python",
        lambda source, *, on_start, **kw: _fake_cpsat_result(),
    )
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_checker",
        lambda checker, result, **kw: _fake_checker_report(),
    )
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "submit_cpsat_python_job",
            {
                "source": "x=1",
                "problem": "toy",
                "checker": 'print(\'{"status":"accepted","errors":[]}\')',
                "checker_timeout_ms": 7000,
            },
        )
    )
    assert "job_id" in result
    assert result["checker_timeout_ms"] == 7000


@pytest.mark.asyncio
async def test_submit_cpsat_python_job_serializes_json_object_problem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `problem` sent as a JSON object reaches the checker as serialized text.

    An MCP call is one JSON document, so a caller with a machine-readable
    instance may spell `problem` as an object rather than as a string. The tool
    boundary serializes it instead of rejecting the call.
    """
    seen: dict[str, Any] = {}
    checked = threading.Event()

    def _capture_checker(checker: str, result: Any, **kwargs: Any) -> Any:
        seen["problem"] = kwargs["problem"]
        checked.set()
        return _fake_checker_report()

    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python",
        lambda source, *, on_start, **kw: _fake_cpsat_result(),
    )
    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_checker", _capture_checker)
    mcp = create_mcp_server()
    instance = {"request": "schedule ft06", "num_machines": 6, "jobs": [[[2, 1], [0, 3]]]}
    await mcp.call_tool(
        "submit_cpsat_python_job",
        {
            "source": "x=1",
            "problem": instance,
            "checker": 'print(\'{"status":"accepted","errors":[]}\')',
        },
    )

    assert checked.wait(3.0), "checker never ran"
    assert json.loads(seen["problem"]) == instance


@pytest.mark.asyncio
async def test_submit_cpsat_python_job_rejects_bad_checker_args_before_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="without checker"):
        await mcp.call_tool(
            "submit_cpsat_python_job",
            {"source": "x=1", "checker_timeout_ms": 5000},
        )
    list_data = _structured(await mcp.call_tool("list_cpsat_python_jobs", {}))["result"]
    assert list_data == []


@pytest.mark.asyncio
async def test_submit_cpsat_python_file_job_accepts_checker_inputs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python_file",
        lambda path, *, on_start, **kw: _fake_cpsat_result(),
    )
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_checker",
        lambda checker, result, **kw: _fake_checker_report(),
    )
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "submit_cpsat_python_file_job",
            {
                "script_path": str(script),
                "checker": 'print(\'{"status":"accepted","errors":[]}\')',
            },
        )
    )
    assert "job_id" in result
    # Default fallback: a checker without checker_timeout_ms echoes timeout_ms.
    assert result["checker_timeout_ms"] == result["script_timeout_ms"]


@pytest.mark.asyncio
async def test_submit_cpsat_python_file_job_forwards_checker_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text('print(\'{"status":"accepted","errors":[]}\')', encoding="utf-8")
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs.run_cpsat_python_file",
        lambda path, *, on_start, **kw: _fake_cpsat_result(),
    )

    def _spy(checker_path: Path, result: Any, **kw: object) -> Any:
        seen["checker_path"] = checker_path
        return _fake_checker_report()

    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_checker_file", _spy)
    mcp = create_mcp_server()
    submitted = _structured(
        await mcp.call_tool(
            "submit_cpsat_python_file_job",
            {"script_path": str(script), "checker_path": str(checker)},
        )
    )
    await _poll_job_status(mcp, submitted["job_id"], get_tool="get_cpsat_python_job")
    assert seen["checker_path"] == checker.resolve()


@pytest.mark.asyncio
async def test_submit_cpsat_python_file_job_rejects_both_checker_forms(tmp_path: Path) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text('print(\'{"status":"accepted","errors":[]}\')', encoding="utf-8")
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="mutually exclusive"):
        await mcp.call_tool(
            "submit_cpsat_python_file_job",
            {
                "script_path": str(script),
                "checker": 'print(\'{"status":"accepted","errors":[]}\')',
                "checker_path": str(checker),
            },
        )
    list_data = _structured(await mcp.call_tool("list_cpsat_python_jobs", {}))["result"]
    assert list_data == []


# --- save_verified_cpsat_python tool -----------------------------------------


def _fake_cpsat_run_result(
    *,
    status: str = "optimal",
    solution: dict | None = None,
    objective: float | None = 3.0,
    stdout: str = '{"status":"optimal","objective":3,"solution":{"x":3}}',
    duration_ms: int = 10,
) -> Any:
    return CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution if solution is not None else {"x": 3},
        objective=objective,
        stdout=stdout,
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=duration_ms,
    )


def _fake_cpsat_save_result(
    *,
    status: str = "optimal",
    saved: bool = True,
    verification_level: str = "reported",
) -> Any:
    from openconstraint_mcp.schemas.cpsat import SaveVerifiedPythonResult

    return SaveVerifiedPythonResult(
        status=status,  # type: ignore[arg-type]
        target_dir="/tmp/s" if saved else None,
        reason=None if saved else "status=infeasible",
        solution={"x": 3} if saved else None,
        objective=3.0 if saved else None,
        stdout="",
        stderr="",
        timed_out=False,
        truncated=False,
        duration_ms=10,
        verification_level=verification_level,  # type: ignore[arg-type]
        reported_passed=saved,
        expectation=None,
        expectation_passed=None,
        checker=None,
    )


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_is_listed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert "save_verified_cpsat_python" in names


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_schema_includes_new_params() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "save_verified_cpsat_python")
    props = set(tool.input_schema["properties"])
    assert {
        "source",
        "target_dir",
        "expectation",
        "checker",
        "checker_timeout_ms",
        "verify_only",
    } <= props
    assert "ctx" not in props
    # `target_dir` is required only for a real save; verify_only=true omits it,
    # a condition the generated JSON schema cannot express (enforced at runtime).
    assert set(tool.input_schema.get("required", [])) == {"source"}


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_routes_to_save(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: _fake_cpsat_run_result(),
    )
    target = tmp_path / "save_target"
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {"source": "print('x')", "target_dir": str(target)},
        )
    )
    assert result["saved"] is True
    assert result["verification_level"] == "reported"


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_passes_verify_only_and_no_target_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: dict[str, Any] = {}

    def _spy(source: str, **kw: Any) -> Any:
        received.update(kw)
        return _fake_cpsat_save_result()

    monkeypatch.setattr("openconstraint_mcp.server.save_verified_cpsat_python", _spy)

    mcp = create_mcp_server()
    await mcp.call_tool(
        "save_verified_cpsat_python",
        {"source": "print('x')", "verify_only": True},
    )

    assert received["verify_only"] is True
    assert received["target_dir"] is None


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_missing_target_dir() -> None:
    # The published schema cannot express "required unless verify_only=true", so
    # the normal-mode requirement is enforced at the tool boundary instead.
    mcp = create_mcp_server()
    with pytest.raises(ToolError, match="target_dir is required unless verify_only"):
        await mcp.call_tool("save_verified_cpsat_python", {"source": "print('x')"})


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_verify_only_reports_no_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: _fake_cpsat_run_result(),
    )
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {"source": "print('x')", "verify_only": True},
        )
    )
    assert result["saved"] is False
    assert result["target_dir"] is None
    assert result["reason"] is None
    assert result["verification_level"] == "reported"


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_accepts_nested_expectation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """MCPServer coerces nested `expectation` dict to CpsatExpectation."""
    received: dict[str, Any] = {}
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: _fake_cpsat_run_result(objective=10.0),
    )

    original_save = __import__(
        "openconstraint_mcp.pyexec.save", fromlist=["save_verified_cpsat_python"]
    ).save_verified_cpsat_python

    def _spy(source: str, *, target_dir: Path, expectation: Any = None, **kw: Any) -> Any:
        received["expectation"] = expectation
        return original_save(source, target_dir=target_dir, expectation=expectation, **kw)

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.save_verified_cpsat_python", _spy)
    monkeypatch.setattr("openconstraint_mcp.server.save_verified_cpsat_python", _spy)

    target = tmp_path / "save_t"
    mcp = create_mcp_server()
    result = _structured(
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {
                "source": "print('x')",
                "target_dir": str(target),
                "expectation": {"objective_sense": "maximize", "objective_threshold": 5.0},
            },
        )
    )
    exp = received.get("expectation")
    assert exp is not None
    assert exp.objective_sense == "maximize"
    assert exp.objective_threshold == 5.0
    assert result["expectation_passed"] is True


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_invalid_nested_threshold(
    tmp_path: Path,
) -> None:
    """An invalid nested expectation (NaN threshold) is rejected by MCPServer at the tool
    boundary."""
    mcp = create_mcp_server()
    import math

    with pytest.raises(Exception, match="finite"):
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {
                "source": "print('x')",
                "target_dir": str(tmp_path / "x"),
                "expectation": {"objective_sense": "maximize", "objective_threshold": math.nan},
            },
        )


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_checker_timeout_without_checker(
    tmp_path: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="checker_timeout_ms"):
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {
                "source": "print('x')",
                "target_dir": str(tmp_path / "x"),
                "checker_timeout_ms": 5000,
            },
        )


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_empty_checker(
    tmp_path: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="non-empty"):
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {
                "source": "print('x')",
                "target_dir": str(tmp_path / "x"),
                "checker": "",
            },
        )


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_whitespace_only_checker(
    tmp_path: Path,
) -> None:
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="non-empty"):
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {
                "source": "print('x')",
                "target_dir": str(tmp_path / "x"),
                "checker": "  \n",
            },
        )


# --- save_verified_cpsat_python seed passthrough -----------------------------


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_schema_includes_seed() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()
    tool = next(t for t in tools if t.name == "save_verified_cpsat_python")
    assert "seed" in set(tool.input_schema["properties"])


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_passes_seed_through(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def _spy(source: str, **kw: Any) -> Any:
        seen.update(kw)
        return _fake_cpsat_save_result()

    monkeypatch.setattr("openconstraint_mcp.server.save_verified_cpsat_python", _spy)
    mcp = create_mcp_server()
    await mcp.call_tool(
        "save_verified_cpsat_python",
        {"source": "print('x')", "target_dir": str(tmp_path / "s"), "seed": 7},
    )
    assert seen["seed"] == 7


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_tool_rejects_bool_seed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # `seed: StrictInt | None` makes MCPServer/pydantic reject a JSON boolean at the
    # tool boundary instead of silently coercing `true` to int 1, so the MCP surface
    # enforces the documented "non-bool integer" contract rather than relying on the
    # function-level guard in save.py (which only protects a direct Python call; see
    # tests/pyexec/test_save.py::test_save_with_bool_seed_is_rejected).
    called = False

    def _spy(source: str, **kw: Any) -> Any:
        nonlocal called
        called = True
        return _fake_cpsat_save_result()

    monkeypatch.setattr("openconstraint_mcp.server.save_verified_cpsat_python", _spy)
    mcp = create_mcp_server()
    with pytest.raises(Exception, match="valid integer"):
        await mcp.call_tool(
            "save_verified_cpsat_python",
            {"source": "print('x')", "target_dir": str(tmp_path / "s"), "seed": True},
        )
    assert not called


# --- tabular I/O tools ---------------------------------------------------------


@pytest.mark.asyncio
async def test_load_tabular_data_tool_is_listed_with_its_pagination_inputs() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    tool = next(t for t in tools if t.name == "load_tabular_data")
    properties = tool.input_schema.get("properties", {})
    assert {"path", "sheet", "has_header", "row_offset", "max_rows"} <= set(properties.keys())


@pytest.mark.asyncio
async def test_write_tabular_result_tool_is_listed_with_its_inputs() -> None:
    mcp = create_mcp_server()
    tools = await mcp.list_tools()

    tool = next(t for t in tools if t.name == "write_tabular_result")
    properties = tool.input_schema.get("properties", {})
    assert {"headers", "rows", "target_path", "overwrite", "style", "gantt", "charts"} <= set(
        properties.keys()
    )


@pytest.mark.asyncio
async def test_load_tabular_data_returns_a_paginated_page(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("name,qty\na,1\nb,2\nc,3\n", encoding="utf-8")

    mcp = create_mcp_server()
    result = await mcp.call_tool("load_tabular_data", {"path": str(source), "max_rows": 2})

    structured = _structured(result)
    assert structured["headers"] == ["name", "qty"]
    assert structured["rows"] == [["a", "1"], ["b", "2"]]
    assert structured["next_row_offset"] == 2
    assert structured["truncation_reason"] == "max_rows"
    assert structured["total_rows"] == 3


@pytest.mark.asyncio
async def test_load_tabular_data_text_content_does_not_duplicate_the_rows(
    tmp_path: Path,
) -> None:
    # The low-level MCP SDK's default handling of a plain dict/model return
    # would put the full page in content a second time (as indent=2 JSON) on
    # top of structuredContent, doubling an already MAX_TABULAR_RESPONSE_BYTES
    # -sized page on the wire. Row values must appear only in structuredContent.
    source = tmp_path / "data.csv"
    source.write_text("name,qty\nwidget,3\ngadget,10\n", encoding="utf-8")

    mcp = create_mcp_server()
    result = await mcp.call_tool("load_tabular_data", {"path": str(source)})

    assert "widget" not in _content_text(result)
    assert "gadget" not in _content_text(result)
    assert _structured(result)["rows"] == [["widget", "3"], ["gadget", "10"]]


@pytest.mark.asyncio
async def test_load_tabular_data_next_row_offset_reaches_the_rest(tmp_path: Path) -> None:
    source = tmp_path / "data.csv"
    source.write_text("name,qty\na,1\nb,2\nc,3\n", encoding="utf-8")

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "load_tabular_data", {"path": str(source), "row_offset": 2, "max_rows": 2}
    )

    structured = _structured(result)
    assert structured["rows"] == [["c", "3"]]
    assert structured["truncated"] is False
    assert structured["next_row_offset"] is None


@pytest.mark.asyncio
async def test_write_tabular_result_writes_a_csv(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "write_tabular_result",
        {"headers": ["task", "start"], "rows": [["a", 0], ["b", 3]], "target_path": str(target)},
    )

    structured = _structured(result)
    assert structured["status"] == "written"
    assert structured["format"] == "csv"
    assert structured["rows_written"] == 2
    assert target.read_text(encoding="utf-8").splitlines() == ["task,start", "a,0", "b,3"]


@pytest.mark.asyncio
async def test_write_tabular_result_writes_an_xlsx(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "write_tabular_result",
        {"headers": ["task", "start"], "rows": [["a", 0]], "target_path": str(target)},
    )

    structured = _structured(result)
    assert structured["format"] == "xlsx"
    assert target.is_file()

    # Round-trips through the reader with its scalar types intact.
    page = await mcp.call_tool("load_tabular_data", {"path": str(target)})
    assert _structured(page)["rows"] == [["a", 0]]


@pytest.mark.asyncio
async def test_write_tabular_result_renders_a_gantt_sheet(tmp_path: Path) -> None:
    target = tmp_path / "schedule.xlsx"

    mcp = create_mcp_server()
    result = await mcp.call_tool(
        "write_tabular_result",
        {
            "headers": ["task", "start", "duration"],
            "rows": [["cut", 0, 3], ["polish", 3, 2]],
            "target_path": str(target),
            "gantt": {
                "task_column": "task",
                "start_column": "start",
                "duration_column": "duration",
            },
        },
    )

    structured = _structured(result)
    assert structured["sheets_written"] == ["Sheet1", "Gantt"]
    assert structured["diagrams_written"] == ["gantt"]


@pytest.mark.asyncio
async def test_write_tabular_result_rejects_a_gantt_on_a_csv_target(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"

    mcp = create_mcp_server()
    with pytest.raises(Exception, match="Write .xlsx instead"):  # noqa: B017 - wrapped by MCPServer
        await mcp.call_tool(
            "write_tabular_result",
            {
                "headers": ["task", "start", "duration"],
                "rows": [["cut", 0, 3]],
                "target_path": str(target),
                "gantt": {
                    "task_column": "task",
                    "start_column": "start",
                    "duration_column": "duration",
                },
            },
        )

    assert not target.exists()


@pytest.mark.asyncio
async def test_write_tabular_result_rejects_a_nested_cell_before_touching_the_disk(
    tmp_path: Path,
) -> None:
    # The scalar-only cell contract is enforced by the tool's own input schema,
    # so a nested array never reaches the writer.
    target = tmp_path / "out.csv"

    mcp = create_mcp_server()
    with pytest.raises(Exception):  # noqa: B017 - MCPServer wraps schema failures in its own type
        await mcp.call_tool(
            "write_tabular_result",
            {"headers": ["a"], "rows": [[["nested"]]], "target_path": str(target)},
        )

    assert not target.exists()
