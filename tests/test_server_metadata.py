from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer
from pydantic import ValidationError

from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.protocol_text.descriptions import (
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS_CORE,
)
from openconstraint_mcp.pyexec.jobs import CpsatJobRegistry
from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerTestReport,
    CpsatPythonCheckedResult,
    CpsatPythonResult,
)
from openconstraint_mcp.server import (
    _homepage_url,
    _make_lifespan,
    _server_version,
    create_mcp_server,
    run_stdio,
)

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.shared.childproc import ChildProcessTracker
from openconstraint_mcp.shared.proc import popen_process_group


def _boot_lifespan() -> Callable[[MCPServer[Any]], AbstractAsyncContextManager[None]]:
    """A wired lifespan over fresh server-owned registries (boot tests)."""
    return _make_lifespan(JobRegistry(), CpsatJobRegistry(), ChildProcessTracker())


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_teardown_terminates_in_flight_sync_child(
    fake_runtime_dir: Path,
) -> None:
    # The synchronous tools register their live child with the tracker; the
    # lifespan must terminate whatever is still in flight on teardown so it is
    # not orphaned, the same coverage background-job children already get.
    tracker = ChildProcessTracker()
    child = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker.register(child)
    lifespan = _make_lifespan(JobRegistry(), CpsatJobRegistry(), tracker)

    async with lifespan(create_mcp_server()):
        assert child.poll() is None  # still running within the server's lifetime

    assert child.wait(timeout=5) is not None  # terminated on teardown


@pytest.mark.integration
@pytest.mark.asyncio
async def test_lifespan_teardown_reaps_sync_child_even_if_registry_shutdown_raises(
    fake_runtime_dir: Path,
) -> None:
    # The two teardown steps cover disjoint child sets and are independently
    # guarded: a failure tearing down the background-job registry must not skip
    # terminating the in-flight synchronous children, or they would be orphaned.
    class _BoomRegistry:
        def shutdown(self) -> None:
            raise RuntimeError("registry boom")

    tracker = ChildProcessTracker()
    child = popen_process_group([sys.executable, "-c", "import time; time.sleep(60)"])
    tracker.register(child)
    lifespan = _make_lifespan(_BoomRegistry(), CpsatJobRegistry(), tracker)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="registry boom"):
        async with lifespan(create_mcp_server()):
            assert child.poll() is None  # still running within the server's lifetime

    assert child.wait(timeout=5) is not None  # reaped despite the registry failure


# --- website_url metadata --------------------------------------------------


def _expected_homepage_from_metadata() -> str | None:
    """Parse the ``Homepage`` Project-URL the same way the server should.

    Derived from live ``importlib.metadata`` so the test does not hardcode the
    URL literal: when the dedicated homepage launches, only ``pyproject.toml``
    changes and this expectation tracks it automatically.
    """
    for entry in metadata.metadata("openconstraint-mcp").get_all("Project-URL") or []:
        label, _, url = entry.partition(",")
        if label.strip().lower() == "homepage":
            return url.strip()
    return None


def test_homepage_url_returns_declared_homepage() -> None:
    url = _homepage_url()

    assert url is not None
    # Load-bearing: the comma-split leaves a leading space (' https://…'); this
    # assertion fails if the parse forgets to strip, catching a shared bug.
    assert url.startswith("https://")
    assert url == _expected_homepage_from_metadata()


def test_server_advertises_homepage_as_website_url() -> None:
    assert create_mcp_server().website_url == _homepage_url()


def test_homepage_url_none_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> object:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.metadata", _raise)

    assert _homepage_url() is None


def test_server_version_unknown_when_metadata_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(_name: str) -> str:
        raise metadata.PackageNotFoundError("openconstraint-mcp")

    monkeypatch.setattr("openconstraint_mcp.server.metadata.version", _raise)

    assert _server_version() == "unknown"


# --- lifespan boot diagnostic ----------------------------------------------


@pytest.mark.asyncio
async def test_boot_diagnostic_warns_when_runtime_missing(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _boot_lifespan()(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert _server_version() in err
    assert str(fake_runtime_dir) in err
    assert "NOT installed" in err
    assert "install-runtime" in err


@pytest.mark.asyncio
async def test_boot_diagnostic_reports_installed_runtime(
    fake_minizinc_binary: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async with _boot_lifespan()(create_mcp_server()):
        pass

    err = capsys.readouterr().err
    assert "installed" in err
    assert str(fake_minizinc_binary) in err


@pytest.mark.asyncio
async def test_boot_diagnostic_writes_nothing_to_stdout(
    fake_runtime_dir: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Over stdio, stdout is the JSON-RPC channel; the banner must never land
    # there or it corrupts the protocol.
    async with _boot_lifespan()(create_mcp_server()):
        pass

    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_lifespan_teardown_shuts_down_the_server_registry(
    fake_runtime_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The server's own lifespan must terminate its job registry on exit (orphan
    # handling). Driving the wired lifespan and spying the class-level shutdown
    # proves create_mcp_server() bound the teardown to its registry.
    calls: list[bool] = []
    monkeypatch.setattr(
        "openconstraint_mcp.jobs.registry.JobRegistry.shutdown",
        lambda self: calls.append(True),
    )
    server = create_mcp_server()
    lifespan = server.settings.lifespan
    assert lifespan is not None

    async with lifespan(server):
        assert calls == []
    assert calls == [True]


# --- toolset profiles ------------------------------------------------------

# The exact eight-tool core inventory. Pinned as a literal (not derived) so a
# schema reduction cannot hide accidental tool exposure and a new tool cannot
# silently join core.
CORE_TOOL_NAMES = {
    "check_runtime",
    "list_available_solvers",
    "check_minizinc_model",
    "solve_minizinc_model",
    "check_minizinc_files",
    "solve_minizinc_files",
    "run_cpsat_python",
    "run_cpsat_python_file",
}

# The complete current full-profile tool surface, pinned exactly.
FULL_TOOL_NAMES = CORE_TOOL_NAMES | {
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

# The exact core prompt inventory: one compact backend-neutral workflow prompt,
# usable for either backend and naming core tools only.
CORE_PROMPT_NAMES = {"solve_constraint_problem"}

FULL_PROMPT_NAMES = CORE_PROMPT_NAMES | {
    "minizinc_solution_workflow",
    "cpsat_python_solution_workflow",
    "auto_tune_constraint_problem",
}

# A schema-change budget failure is a REVIEW trigger, not a cap to silently
# raise: reduce descriptions or reconsider the core inventory instead.
CORE_METADATA_BUDGET_BYTES = 40_000

# The same rule for the full profile, which core-only budgets never covered.
# Measured at 300 168 bytes across the 31 full tools. The rise from 300 000
# accommodates the styled-xlsx-export feature: four new schema models
# (ColumnStyle, TableStyle, GanttSpec, ChartSpec) plus `write_tabular_result`'s
# three new parameters (style, gantt, charts). The cap carries deliberate
# headroom for one or two more tools' worth of schema, not unbounded growth. A
# failure here is a REVIEW trigger, not a cap to silently raise.
FULL_METADATA_BUDGET_BYTES = 320_000

SOLVE_TOOL_NAMES = (
    "solve_minizinc_model",
    "solve_minizinc_files",
    "run_cpsat_python",
    "run_cpsat_python_file",
)

# The stable plain-language vocabulary each solve tool advertises for itself,
# independently of the server `instructions` that carry the same words.
SOLVE_DOMAIN_CUES = (
    "scheduling",
    "rostering",
    "assignment",
    "routing",
    "bin-packing",
    "knapsack",
    "allocation",
)

# The plain-language domain cue and the input precondition must LEAD each solve
# tool's description, not sit in its tail. A failure here is a REVIEW trigger,
# not a cap to silently raise: widening the window lets the cue drift out of the
# opening, which voids the "lead with" contract these assertions exist to hold.
SOLVE_CUE_PREFIX_CHARS = 340

# A budget failure here is a REVIEW trigger, not a cap to silently raise: the
# routing/safety paragraphs must survive client-side instructions truncation,
# so growth should shrink other paragraphs or be reconsidered, not just raise
# this number.
CORE_INSTRUCTIONS_BUDGET_BYTES = 2_048

# A budget failure here is a REVIEW trigger, not a cap to silently raise:
# Codex's documented guidance is that only a bounded prefix of `instructions`
# is guaranteed to reach the model self-contained before truncation may cut
# it off, so the routing paragraph plus the paragraph after it must fit this
# head. Measured in UTF-8 bytes, the conservative axis — see
# CORE_INSTRUCTIONS_BUDGET_BYTES's rationale above.
TRUNCATION_HEAD_BUDGET_BYTES = 512


def _serialize_tools(tools: list[Any]) -> str:
    """Deterministic compact serialization of a complete advertised tool list.

    One ``json.dumps`` over the whole list — ``model_dump(mode="json",
    by_alias=True, exclude_none=True)`` per tool, sorted keys, compact
    separators — so the measured bytes include the list framing a client
    actually receives. ``by_alias=True`` is required: the mcp SDK's protocol
    models use snake_case Python field names with camelCase wire aliases, and
    the wire bytes (what a client actually receives) are camelCase. Reused by
    the budget and reference-safety tests so they scan the same payload.
    """
    return json.dumps(
        [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools],
        sort_keys=True,
        separators=(",", ":"),
    )


async def _tools_by_name(toolset: str) -> dict[str, Any]:
    tools = await create_mcp_server(toolset).list_tools()
    return {tool.name: tool for tool in tools}


@pytest.mark.asyncio
async def test_core_profile_exposes_exactly_the_eight_core_tools() -> None:
    tools = await _tools_by_name("core")
    assert set(tools) == CORE_TOOL_NAMES


@pytest.mark.asyncio
async def test_full_profile_retains_the_current_thirty_one_tool_set() -> None:
    tools = await _tools_by_name("full")
    assert set(tools) == FULL_TOOL_NAMES
    assert len(FULL_TOOL_NAMES) == 31


def _tools_declaring_problem() -> list[Any]:
    """Full-profile tools exposing a `problem` parameter.

    Reads the tool manager rather than the public `list_tools()`: protocol tools
    carry the published schema but not the pydantic arg model, and one of these
    guards has to validate arguments against it.
    """
    mcp = create_mcp_server("full")
    return [
        tool
        for tool in mcp._tool_manager.list_tools()
        if "problem" in tool.parameters.get("properties", {})
    ]


def test_every_problem_parameter_publishes_string_or_null() -> None:
    # Text is the canonical form callers should send; accepting the object
    # spelling is runtime leniency, not an advertised second shape.
    tools = _tools_declaring_problem()
    assert tools, "no tool declares `problem` — this guard would pass vacuously"
    for tool in tools:
        assert tool.parameters["properties"]["problem"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ], tool.name


def test_every_problem_parameter_accepts_a_json_object() -> None:
    # EVERY tool taking `problem` accepts the object spelling, so this asserts the
    # coercion and not the schema: a tool added with a plain `str | None` publishes
    # a byte-identical schema and fails only at call time. Validating arguments
    # must therefore raise nothing AT `problem` — the other parameters are absent
    # here and are expected to error.
    tools = _tools_declaring_problem()
    assert tools, "no tool declares `problem` — this guard would pass vacuously"
    for tool in tools:
        try:
            tool.fn_metadata.arg_model.model_validate({"problem": {"num_machines": 6}})
        except ValidationError as exc:
            rejected = [error for error in exc.errors() if error["loc"] == ("problem",)]
            assert not rejected, f"{tool.name} rejects an object `problem`: {rejected}"


@pytest.mark.asyncio
async def test_cpsat_file_tools_advertise_an_args_parameter() -> None:
    # Both file-based CP-SAT surfaces must publish `args`, otherwise a client
    # cannot pass a script its data file without editing the script's source.
    tools = await _tools_by_name("full")
    for name in ("run_cpsat_python_file", "submit_cpsat_python_file_job"):
        assert "args" in tools[name].input_schema.get("properties", {}), (
            f"{name} does not advertise `args`"
        )


@pytest.mark.asyncio
async def test_cpsat_tools_document_the_args_rejection_limits() -> None:
    # Every tool that forwards `args` to a child enforces the NUL/size preflight,
    # so every one of them must say so: a client that learns the rule only from a
    # rejection has already wasted a call.
    tools = await _tools_by_name("full")
    for name in (
        "run_cpsat_python_file",
        "run_cpsat_python_file_checked",
        "run_cpsat_python_experiment",
        "submit_cpsat_python_file_job",
    ):
        description = tools[name].description or ""
        assert "contains a NUL" in description or "containing a NUL" in description, (
            f"{name} does not document the NUL rejection"
        )
        assert "32 KiB" in description, f"{name} does not document the argv size bound"


@pytest.mark.asyncio
async def test_run_cpsat_python_file_core_profile_documents_the_args_limits() -> None:
    # The core profile publishes its own trimmed description; the limits are a
    # rejection rule, not a full-profile extra, so they must survive the trim.
    tools = await _tools_by_name("core")
    description = tools["run_cpsat_python_file"].description or ""
    assert "32 KiB" in description


async def _core_solve_description_openings() -> dict[str, str]:
    """Each core solve tool's advertised description, cut to its leading window.

    Read from the live core server, not the description constants, so these
    assertions cover what a client is actually sent.
    """
    tools = await _tools_by_name("core")
    return {
        name: (tools[name].description or "")[:SOLVE_CUE_PREFIX_CHARS] for name in SOLVE_TOOL_NAMES
    }


@pytest.mark.asyncio
async def test_solve_tools_lead_with_the_plain_language_domain_cue() -> None:
    for name, opening in (await _core_solve_description_openings()).items():
        for cue in SOLVE_DOMAIN_CUES:
            assert cue in opening, f"{name} does not lead with the {cue!r} domain cue"


@pytest.mark.asyncio
async def test_inline_solve_tools_lead_with_a_complete_artifact_precondition() -> None:
    # An inline tool takes a drafted model/script, never the user's raw prose.
    openings = await _core_solve_description_openings()
    for name in ("solve_minizinc_model", "run_cpsat_python"):
        assert "COMPLETE" in openings[name], name
        assert "prose" in openings[name], name


@pytest.mark.asyncio
async def test_file_solve_tools_lead_with_an_existing_on_disk_artifact() -> None:
    openings = await _core_solve_description_openings()
    for name in ("solve_minizinc_files", "run_cpsat_python_file"):
        assert "already has on disk" in openings[name], name


@pytest.mark.asyncio
async def test_core_profile_registers_only_the_backend_neutral_prompt() -> None:
    prompts = await create_mcp_server("core").list_prompts()
    assert {prompt.name for prompt in prompts} == CORE_PROMPT_NAMES


@pytest.mark.asyncio
async def test_full_profile_retains_the_four_prompts() -> None:
    prompts = await create_mcp_server("full").list_prompts()
    assert {prompt.name for prompt in prompts} == FULL_PROMPT_NAMES


def test_create_mcp_server_rejects_unknown_toolset() -> None:
    # The factory boundary rejects a bad value before any server is built,
    # independently of Typer's CLI-level validation, and names the accepted set.
    with pytest.raises(ValueError) as excinfo:
        create_mcp_server(toolset="typo")
    message = str(excinfo.value)
    assert "core" in message
    assert "full" in message


@pytest.mark.asyncio
async def test_full_profile_descriptions_advertise_full_only_cross_references() -> None:
    # Read from the actual full-profile registration (not the description
    # constants) so this verifies the wiring, not just the strings: the
    # conditional guidance the core variants drop must still be advertised here.
    tools = await _tools_by_name("full")
    assert "submit_portfolio_job" in tools["solve_minizinc_model"].description
    run_cpsat_python_desc = tools["run_cpsat_python"].description
    assert "cpsat_python_solution_workflow" in run_cpsat_python_desc
    assert "submit_portfolio_job" in run_cpsat_python_desc
    run_cpsat_python_file_desc = tools["run_cpsat_python_file"].description
    assert "save_verified_cpsat_python" in run_cpsat_python_file_desc
    # The checked-replay pointer names the gate-only mode, not a scratch target:
    # `verify_only=true` ignores a supplied `target_dir`, so persisting a replay
    # must be advertised as `verify_only=false`, never as a throwaway target.
    assert "verify_only=true" in run_cpsat_python_file_desc
    assert "scratch" not in run_cpsat_python_file_desc
    assert "`verify_only=false`" in run_cpsat_python_file_desc


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_description_states_verify_only_result_shape() -> None:
    # A passing verify-only run is a `saved=false` SUCCESS, so the advertised
    # description must say so rather than letting a client read `saved` as the verdict.
    tools = await _tools_by_name("full")
    description = tools["save_verified_cpsat_python"].description
    assert "verify_only=true" in description
    assert "`reason=null` with `saved=false`" in description


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_description_excludes_script_path_attempt_provenance() -> (
    None
):
    # The advertised contract used to promise "any matching accepted attempt";
    # a script_path attempt is now excluded, and a client has no other way to
    # learn that before its save is rejected.
    tools = await _tools_by_name("full")
    description = tools["save_verified_cpsat_python"].description
    assert "`used_script_path: true`" in description
    assert "At least one matching attempt must be an inline-`source` one" in description


@pytest.mark.asyncio
async def test_run_cpsat_python_experiment_description_documents_script_path_attempts() -> None:
    tools = await _tools_by_name("full")
    description = tools["run_cpsat_python_experiment"].description
    assert "`script_path`" in description
    assert "EXACTLY ONE of the two, never both and never neither" in description
    # `args` is rejected, not ignored, when paired with an inline source.
    assert "rejected when supplied alongside `source`" in description
    # Only attempts gained a path option; checker/problem stay inline.
    assert "this tool has no `checker_path`" in description


@pytest.mark.asyncio
async def test_save_verified_cpsat_python_output_schema_does_not_read_saved_as_verdict() -> None:
    # The SaveVerifiedPythonResult docstring is published verbatim as this tool's
    # outputSchema.description, so the corrected two-field rule must reach clients.
    tools = await _tools_by_name("full")
    output_schema = tools["save_verified_cpsat_python"].output_schema
    assert output_schema is not None
    description = output_schema["description"]
    assert "combine with ``saved``" not in description
    assert "PERSISTENCE only, never the verdict" in description


@pytest.mark.asyncio
@pytest.mark.parametrize("toolset", ["core", "full"])
async def test_run_cpsat_python_advertises_the_required_envelope_vocabulary(toolset: str) -> None:
    # The required-key/type contract is what a client's script is graded against,
    # so it must survive the core profile's trimmed description too.
    tools = await _tools_by_name(toolset)
    description = tools["run_cpsat_python"].description or ""

    assert "all three REQUIRED keys" in description
    assert "`status` (str)" in description
    assert "`objective` (number|null)" in description
    assert "`solution` (object; `{}` with no incumbent)" in description
    assert "a missing or invalid required key is rejected" in description


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["run_cpsat_python", "run_cpsat_python_file"])
async def test_full_cpsat_tool_description_states_the_no_stdin_rule(tool_name: str) -> None:
    # The missing stdin is a property of the child these very tools create, and
    # `tools/list` is the payload no client truncates at 512 bytes — so the one
    # rule whose loss is a failed run rather than an unlovely script gets a
    # second, durable home beside the server instructions' copy.
    tools = await _tools_by_name("full")
    description = tools[tool_name].description or ""

    assert "NO stdin" in description
    assert "`input()`" in description


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["run_cpsat_python", "run_cpsat_python_file"])
async def test_core_cpsat_tool_description_omits_the_no_stdin_rule(tool_name: str) -> None:
    # Pins the profile split deliberately: core metadata has ~260 bytes of
    # headroom, which two copies of this sentence would consume exactly. Moving
    # it into a shared description fragment must fail HERE, loudly, rather than
    # silently eating core's last bytes.
    tools = await _tools_by_name("core")
    description = tools[tool_name].description or ""

    assert "stdin" not in description


@pytest.mark.asyncio
async def test_core_metadata_is_within_budget() -> None:
    tools = await create_mcp_server("core").list_tools()
    total = len(_serialize_tools(tools).encode("utf-8"))
    assert total <= CORE_METADATA_BUDGET_BYTES, (
        f"core metadata is {total} bytes, over the {CORE_METADATA_BUDGET_BYTES} budget"
    )


@pytest.mark.asyncio
async def test_full_metadata_is_within_budget() -> None:
    tools = await create_mcp_server("full").list_tools()
    total = len(_serialize_tools(tools).encode("utf-8"))
    assert total <= FULL_METADATA_BUDGET_BYTES, (
        f"full metadata is {total} bytes, over the {FULL_METADATA_BUDGET_BYTES} budget"
    )


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_is_full_only() -> None:
    # Advertising CpsatPythonCheckedResult costs ~1.8 kB of outputSchema alone,
    # which core (1 084 bytes of headroom) cannot absorb — hence a full-only tool.
    assert "run_cpsat_python_file_checked" in await _tools_by_name("full")
    assert "run_cpsat_python_file_checked" not in await _tools_by_name("core")


# --- checked-result shape, documented in three registers -------------------

# The three sites that enumerate CpsatPythonCheckedResult's extra fields BY HAND:
# `run_cpsat_python_file`'s terse cross-reference to its checked sibling, the
# checked tool's own per-field gloss, and the CP-SAT reference doc. They
# deliberately differ in register, so they are not collapsed into one shared
# string — instead the SCHEMA is the source of truth and this gate fails when a
# new field fails to reach any of them.
_CPSAT_DOC = "docs/cpsat-python.md"
_CHECKED_SHAPE_SITES = ("run_cpsat_python_file", "run_cpsat_python_file_checked", _CPSAT_DOC)
_CPSAT_DOC_PATH = Path(__file__).parent.parent / "docs" / "cpsat-python.md"


async def _checked_shape_site_text(site: str) -> str:
    """The prose of one site that documents the checked result's extra fields."""
    if site == _CPSAT_DOC:
        return _CPSAT_DOC_PATH.read_text(encoding="utf-8")
    return (await _tools_by_name("full"))[site].description or ""


@pytest.mark.asyncio
@pytest.mark.parametrize("site", _CHECKED_SHAPE_SITES)
async def test_every_checked_only_field_is_documented(site: str) -> None:
    extra_fields = set(CpsatPythonCheckedResult.model_fields) - set(CpsatPythonResult.model_fields)
    text = await _checked_shape_site_text(site)
    missing = sorted(name for name in extra_fields if f"`{name}`" not in text)
    assert not missing, (
        f"{site} does not document CpsatPythonCheckedResult field(s): {', '.join(missing)}"
    )


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_advertises_the_checker_fields() -> None:
    tools = await _tools_by_name("full")
    output_schema = tools["run_cpsat_python_file_checked"].output_schema
    assert output_schema is not None
    properties = output_schema["properties"]
    for field in ("status", "solution", "checker", "checker_skipped_reason", "checker_timeout_ms"):
        assert field in properties, field


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_output_schema_has_no_result_envelope() -> None:
    # The annotation is the concrete return type, so MCPServer publishes the model
    # itself — not a `{"result": ...}` wrapper it would add for a non-model return.
    tools = await _tools_by_name("full")
    output_schema = tools["run_cpsat_python_file_checked"].output_schema
    assert output_schema is not None
    assert "result" not in output_schema["properties"]


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_requires_both_paths() -> None:
    tools = await _tools_by_name("full")
    required = tools["run_cpsat_python_file_checked"].input_schema.get("required", [])
    assert set(required) == {"script_path", "checker_path"}


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_advertises_the_test_checker_parameter() -> None:
    tools = await _tools_by_name("full")
    properties = tools["run_cpsat_python_file_checked"].input_schema["properties"]
    assert "test_checker" in properties


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_test_checker_defaults_off() -> None:
    # The probe costs one extra checker child per applied mutation, so a client
    # must have to ask for it.
    tools = await _tools_by_name("full")
    properties = tools["run_cpsat_python_file_checked"].input_schema["properties"]
    assert properties["test_checker"]["default"] is False


@pytest.mark.asyncio
async def test_run_cpsat_python_file_checked_advertises_the_checker_test_field() -> None:
    tools = await _tools_by_name("full")
    output_schema = tools["run_cpsat_python_file_checked"].output_schema
    assert output_schema is not None
    assert "checker_test" in output_schema["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["rejected_count", "accepted_count"])
async def test_the_checker_test_counts_reach_the_advertised_output_schema(field: str) -> None:
    # Both counts are derived from `mutations`, but they must stay REAL fields:
    # the MCP SDK builds output schemas in pydantic's validation mode, which
    # omits `computed_field`s, so deriving them that way would hide from every
    # client the two numbers it reads first.
    tools = await _tools_by_name("full")
    output_schema = tools["run_cpsat_python_file_checked"].output_schema
    assert output_schema is not None
    report = output_schema["$defs"]["CpsatCheckerTestReport"]
    assert field in report["properties"]


# Derived from the SCHEMA, not from a phrase: `accepted_count` is only readable
# next to the other two (a client told just the rejection count would infer
# toleration by subtraction and mis-score a checker that choked), so the gate is
# that every field of the report reaches both audiences — and it survives any
# rewording of the prose that carries them.
@pytest.mark.asyncio
@pytest.mark.parametrize("site", ("run_cpsat_python_file_checked", _CPSAT_DOC))
async def test_every_checker_test_field_is_documented(site: str) -> None:
    text = await _checked_shape_site_text(site)
    missing = sorted(
        name for name in CpsatCheckerTestReport.model_fields if f"`{name}`" not in text
    )
    assert not missing, f"{site} does not document CpsatCheckerTestReport field(s): {missing}"


def test_core_instructions_toolset_hint_is_paragraph_two_within_head_budget() -> None:
    # The `--toolset full` hint must be paragraph two, immediately after
    # routing, so it survives truncation alongside the routing paragraph.
    # (The routing paragraph itself is covered by
    # test_both_instruction_variants_open_with_the_routing_paragraph in
    # test_server_prompts.py — not duplicated here.)
    paragraphs = MCP_SERVER_INSTRUCTIONS_CORE.split("\n\n")
    assert paragraphs[1].startswith("This is the default core toolset.")
    first_two = "\n\n".join(paragraphs[:2])
    head_bytes = len(first_two.encode("utf-8"))
    assert head_bytes <= TRUNCATION_HEAD_BUDGET_BYTES, (
        f"core instructions head is {head_bytes} bytes, over the "
        f"{TRUNCATION_HEAD_BUDGET_BYTES} truncation-head budget"
    )


def test_core_instructions_are_within_budget() -> None:
    total = len(MCP_SERVER_INSTRUCTIONS_CORE.encode("utf-8"))
    assert total <= CORE_INSTRUCTIONS_BUDGET_BYTES, (
        f"core instructions are {total} bytes, over the {CORE_INSTRUCTIONS_BUDGET_BYTES} budget"
    )


def test_core_instructions_carry_the_no_stdin_rule() -> None:
    # `_CPSAT_NO_STDIN_FULL` is deliberately full-only on the tool descriptions
    # (it fans out to two tools, and core metadata has no headroom), so these
    # instructions are the ONLY channel that reaches a core client which does
    # not use prompts. A script that reads stdin fails the entire run.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS_CORE.split())

    assert "NO stdin" in normalized
    assert "never call `input()` or read `sys.stdin`" in normalized


def test_core_instructions_state_what_the_solution_object_must_carry() -> None:
    # `_CPSAT_JSON_CONTRACT` carries the TYPE contract to both profiles and
    # delegates the SEMANTIC requirement to the prompts and these instructions
    # — so core must state it, or a prompt-less core client is told the shape
    # of `solution` and never what belongs in it.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS_CORE.split())

    assert "must carry the COMPLETE answer" in normalized
    assert "never prose or a file path" in normalized


def test_core_instructions_route_a_missing_runtime_to_install_or_cpsat() -> None:
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS_CORE.split())

    assert "check_runtime" in normalized
    assert "`openconstraint-mcp install-runtime`" in normalized
    assert "use CP-SAT, which needs none" in normalized


def test_full_instructions_lead_with_routing_then_posture() -> None:
    # POSTURE (the safety disclosure) must be paragraph two in the full
    # profile, so it survives truncation alongside the routing paragraph.
    paragraphs = MCP_SERVER_INSTRUCTIONS.split("\n\n")
    assert paragraphs[1].startswith("POSTURE")
    first_two = "\n\n".join(paragraphs[:2])
    head_bytes = len(first_two.encode("utf-8"))
    assert head_bytes <= TRUNCATION_HEAD_BUDGET_BYTES, (
        f"full instructions head is {head_bytes} bytes, over the "
        f"{TRUNCATION_HEAD_BUDGET_BYTES} truncation-head budget"
    )


def _full_instructions_cpsat_bullet() -> str:
    """The `WITHOUT PROMPTS:` CP-SAT bullet of the FULL server instructions.

    The script-layout rules are asserted against this bullet rather than the
    whole string, so none of them can be satisfied by unrelated prose elsewhere
    in the instructions.
    """
    (bullet,) = [
        line for line in MCP_SERVER_INSTRUCTIONS.splitlines() if line.startswith("- CP-SAT:")
    ]
    return bullet


def test_full_instructions_cpsat_bullet_forbids_reading_stdin() -> None:
    # `read_input` is a name that INVITES reading stdin, while the child is
    # launched with stdin=DEVNULL: a script that calls `input()` gets an
    # immediate EOF, prints no envelope, and the run comes back as an error. The
    # bullet must also never offer an input source its named tool cannot supply:
    # naming the file tool is not enough, because run_cpsat_python_file only
    # supplies a data file when `args` carries it, and the inline tool only when
    # the instance is embedded in the script.
    bullet = _full_instructions_cpsat_bullet()

    assert "stdin" in bullet
    assert "never call `input()`" in bullet
    if "sys.argv" in bullet or "data file" in bullet:
        assert "run_cpsat_python_file" in bullet
        assert "args=[" in bullet
        assert "embed the instance in the script" in bullet


def test_full_instructions_cpsat_bullet_names_its_verification_surface() -> None:
    # The MiniZinc bullet has always named its checker surface; the CP-SAT
    # bullet must name its own, or "LLM proposes, server verifies" reaches the
    # client for one backend only — and the experiment tool is otherwise
    # reachable only through a prompt, which the WITHOUT PROMPTS path lacks.
    bullet = _full_instructions_cpsat_bullet()

    assert "run_cpsat_python_file_checked" in bullet
    assert "run_cpsat_python_experiment" in bullet


def test_full_instructions_cpsat_bullet_states_the_typed_solve_boundary() -> None:
    # The spine names alone are satisfied by a pass-through pipeline of untyped
    # dicts, which is not the convention: a typed record has to cross `solve` in
    # both directions.
    bullet = _full_instructions_cpsat_bullet()

    assert "`parse_input` hands `solve` a typed instance record" in bullet
    assert "`solve` hands `serialize_solution` a typed solution record" in bullet


def test_full_instructions_cpsat_bullet_names_the_spine_in_order() -> None:
    # The layout is an ORDER, not a set of names, so assert ascending positions
    # rather than membership.
    bullet = _full_instructions_cpsat_bullet()

    positions = [
        bullet.index(f"`{name}`")
        for name in (
            "read_input",
            "parse_input",
            "solve",
            "serialize_solution",
            "write_output",
            "main",
        )
    ]
    assert positions == sorted(positions)


async def _forbidden_full_only_names() -> set[str]:
    """Every full-only tool name plus every full-only prompt name.

    Derived from the live servers so it tracks reality: both halves are the
    ``full - core`` difference, so a prompt the core profile also exposes is
    NOT classified as forbidden — core text may name it.
    """
    full = create_mcp_server("full")
    core = create_mcp_server("core")
    full_tools = {tool.name for tool in await full.list_tools()}
    full_prompts = {prompt.name for prompt in await full.list_prompts()}
    core_tools = {tool.name for tool in await core.list_tools()}
    core_prompts = {prompt.name for prompt in await core.list_prompts()}
    return (full_tools - core_tools) | (full_prompts - core_prompts)


@pytest.mark.asyncio
async def test_core_tool_payload_names_no_full_only_tool_or_prompt() -> None:
    # Scan the ENTIRE serialized core payload — description, inputSchema, and
    # outputSchema — so a full-only name reaching core through a Pydantic schema
    # docstring or field description is caught, not only a description mention.
    forbidden = await _forbidden_full_only_names()
    core_tools = await create_mcp_server("core").list_tools()
    payload = _serialize_tools(core_tools)
    leaked = sorted(name for name in forbidden if name in payload)
    assert leaked == []


@pytest.mark.asyncio
async def test_core_prompt_surface_names_no_full_only_tool_or_prompt() -> None:
    # The core prompt's advertised description AND its rendered body must stay
    # inside the core surface: a full-only tool or prompt named there points the
    # client's LLM at something the profile does not expose.
    forbidden = await _forbidden_full_only_names()
    mcp = create_mcp_server("core")
    descriptions = [prompt.description or "" for prompt in await mcp.list_prompts()]
    (core_prompt_name,) = CORE_PROMPT_NAMES
    rendered = await mcp.get_prompt(core_prompt_name, {"problem": "pack 5 boxes"})
    payload = "\n".join(
        descriptions
        + [
            message.content.text  # type: ignore[union-attr]
            for message in rendered.messages
        ]
    )
    leaked = sorted(name for name in forbidden if name in payload)
    assert leaked == []


@pytest.mark.asyncio
async def test_core_instructions_name_no_full_only_tool_or_prompt() -> None:
    forbidden = await _forbidden_full_only_names()
    instructions = create_mcp_server("core").instructions or ""
    leaked = sorted(name for name in forbidden if name in instructions)
    assert leaked == []


def test_run_stdio_defaults_to_core_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    # Closes the gap between the mocked CLI tests and the direct factory
    # inventory tests: run_stdio() must request the core profile and run stdio.
    calls: list[tuple[str, str]] = []

    class _FakeServer:
        def run(self, *, transport: str) -> None:
            calls.append(("run", transport))

    def _fake_create(toolset: str = "full") -> _FakeServer:
        calls.append(("create", toolset))
        return _FakeServer()

    monkeypatch.setattr("openconstraint_mcp.server.create_mcp_server", _fake_create)
    run_stdio()
    assert calls == [("create", "core"), ("run", "stdio")]


def test_run_stdio_forwards_full_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    class _FakeServer:
        def run(self, *, transport: str) -> None:
            calls.append(("run", transport))

    def _fake_create(toolset: str = "full") -> _FakeServer:
        calls.append(("create", toolset))
        return _FakeServer()

    monkeypatch.setattr("openconstraint_mcp.server.create_mcp_server", _fake_create)
    run_stdio(toolset="full")
    assert calls == [("create", "full"), ("run", "stdio")]
