from __future__ import annotations

import pytest
from mcp_types import GetPromptResult

from openconstraint_mcp.protocol_text.descriptions import (
    AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION,
    CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    LIST_AVAILABLE_SOLVERS_DESCRIPTION,
    MCP_SERVER_INSTRUCTIONS,
    MCP_SERVER_INSTRUCTIONS_CORE,
    MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION,
    RUN_CPSAT_PYTHON_DESCRIPTION,
    SOLVE_MINIZINC_FILES_DESCRIPTION,
    SOLVE_MINIZINC_MODEL_DESCRIPTION,
)
from openconstraint_mcp.protocol_text.prompts import (
    CPSAT_OUTPUT_CONTRACT_GUIDANCE,
    CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE,
    CPSAT_SCRIPT_INPUT_GUIDANCE,
    CPSAT_SCRIPT_SPINE_GUIDANCE,
    CPSAT_SCRIPT_STRUCTURE_GUIDANCE,
    MINIZINC_SOLUTION_WORKFLOW_PROMPT,
    SOLVE_CPSAT_PYTHON_PROMPT,
)

# Tests deliberately white-box server internals, which are private by design.
# noinspection PyProtectedMember
from openconstraint_mcp.server import (
    create_mcp_server,
)


def test_list_available_solvers_description_documents_capabilities() -> None:
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "capabilities" in text
    for field in (
        "supports_all_solutions",
        "supports_free_search",
        "supports_parallel",
        "supports_random_seed",
        "supports_num_solutions",
        "std_flags",
    ):
        assert field in text, f"description should name the capability field {field}"
    assert "advisory" in text.lower()


def test_list_available_solvers_description_calls_out_conservative_num_solutions_gate() -> None:
    # supports_num_solutions is the conservative gate: only the two supported
    # solvers, explicitly not the default cp-sat.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "org.gecode.gecode" in text
    assert "org.chuffed.chuffed" in text
    assert "cp-sat" in text


def test_list_available_solvers_description_frames_std_flags_as_non_passthrough() -> None:
    # std_flags reports declared flags; it is not a surface for sending flags back
    # into the solve tools.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "solve_minizinc_model" in text
    assert "solve_minizinc_files" in text
    assert "passthrough" in text.lower()


def test_list_available_solvers_description_distinguishes_no_control_from_divergence() -> None:
    # Two distinct cases must stay separate: standard flags with no named control
    # (-i/-s/-t/-v) vs. the gist/-n allowlist divergence.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "gist" in text.lower()
    assert any(flag in text for flag in ("-i", "-s", "-t", "-v")), (
        "description should give a no-named-control flag example"
    )


def test_list_available_solvers_description_notes_complete_inventory_presentation() -> None:
    # The description must advertise the complete-inventory text presentation and
    # that the full capability metadata is structured, not printed by default.
    text = LIST_AVAILABLE_SOLVERS_DESCRIPTION
    assert "inventory" in text.lower()
    assert "not printed by default" in text


def test_solve_minizinc_model_description_nudges_portfolio_for_hard_instances() -> None:
    assert "submit_portfolio_job" in SOLVE_MINIZINC_MODEL_DESCRIPTION


def test_run_cpsat_python_description_nudges_portfolio_for_hard_instances() -> None:
    assert "submit_portfolio_job" in RUN_CPSAT_PYTHON_DESCRIPTION


SAMPLE_PROBLEM = (
    "Schedule 5 nurses across 3 shifts over 7 days so each shift has at least "
    "one nurse and nobody works two shifts in a row."
)


def test_mcp_server_instructions_route_constraint_tasks() -> None:
    mcp = create_mcp_server()
    instructions = mcp.instructions or ""

    for substring in (
        "constraint programming",
        "optimization",
        "knapsack",
        "minizinc_solution_workflow",
        "check_minizinc_model",
        "solve_minizinc_model",
        "inspect_minizinc_model",
        "check_minizinc_files",
        "solve_minizinc_files",
        "managed local MiniZinc runtime",
        "bare PATH minizinc",
    ):
        assert substring in instructions


def test_mcp_server_instructions_present_solution_in_problem_terms() -> None:
    mcp = create_mcp_server()
    instructions = mcp.instructions or ""

    # The non-prompt fallback path must carry the same presentation contract as
    # the minizinc_solution_workflow prompt: state the solution in the terms of
    # the user's problem rather than dumping the raw JSON SolveResult, plus the
    # complete Statistics section when present.
    lower = instructions.lower()
    assert "terms of the user's problem" in lower
    assert "json" in lower
    assert "item table" in lower
    assert "statistics" in lower
    assert "complete" in lower
    assert "condense" in lower


async def _get_prompt_text(prompt_name: str, arguments: dict[str, str]) -> str:
    mcp = create_mcp_server()
    result = await mcp.get_prompt(prompt_name, arguments)
    assert isinstance(result, GetPromptResult)
    return "\n".join(
        message.content.text  # type: ignore[union-attr]
        for message in result.messages
    )


async def _get_core_prompt_text(prompt_name: str, arguments: dict[str, str]) -> str:
    """Render a prompt through the CORE profile — the user-facing stdio default."""
    mcp = create_mcp_server("core")
    result = await mcp.get_prompt(prompt_name, arguments)
    assert isinstance(result, GetPromptResult)
    return "\n".join(
        message.content.text  # type: ignore[union-attr]
        for message in result.messages
    )


# --- solve_constraint_problem ----------------------------------------------


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_substitutes_the_user_problem() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_passes_through_brace_input() -> None:
    # The body is `str.format`ted with the user's text, so a problem containing
    # braces must be substituted literally rather than read as a field.
    problem = 'Pack items {a, b} into bins of capacity {"max": 10}.'
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": problem})

    assert problem in text


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_requires_an_explicit_backend_choice() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    assert "Choose a backend by problem shape, and say which one you chose and why" in normalized
    assert "MiniZinc" in normalized
    assert "OR-Tools CP-SAT Python" in normalized


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_orders_minizinc_check_before_solve() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    assert normalized.index("check_minizinc_model") < normalized.index("solve_minizinc_model")
    assert 'never solve before it returns `"ok"`' in normalized


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_names_the_cpsat_execution_path() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    call = "`run_cpsat_python(source=<complete script>, script_timeout_ms=<milliseconds>)`"
    assert call in normalized


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_routes_existing_artifacts_to_file_tools() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    for tool in ("check_minizinc_files", "solve_minizinc_files", "run_cpsat_python_file"):
        assert tool in text, f"prompt does not route an existing artifact to {tool}"


@pytest.mark.asyncio
async def test_solve_constraint_problem_prompt_carries_the_cpsat_generation_rule() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    # Same mandatory rule as the cpsat_python_solution_workflow prompt: the
    # server cannot enforce it, so the client-facing prompt must state it.
    assert "no network access, no file writes or deletes" in lower
    assert "no subprocess spawning" in lower
    assert "unless the user explicitly requested it" in lower


# --- shared CP-SAT output-contract guidance ---------------------------------


@pytest.mark.asyncio
async def test_backend_neutral_prompt_carries_the_shared_output_contract_fragment() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE in text


@pytest.mark.asyncio
async def test_full_backend_neutral_prompt_carries_the_full_output_contract_fragment() -> None:
    # Full exposes the checker-capable CP-SAT tools, so its copy of the same
    # prompt keeps the wording core has to drop.
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_OUTPUT_CONTRACT_GUIDANCE in text


def test_core_output_contract_fragment_promises_no_checker_execution() -> None:
    # Core exposes only run_cpsat_python / run_cpsat_python_file, neither of
    # which takes a checker — so the fragment must not say the server runs one.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "the server only executes it." in normalized
    assert "runs the checker you supply" not in normalized


def test_full_output_contract_fragment_keeps_the_checker_execution_clause() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "the server only executes it and runs the checker you supply" in normalized


def test_output_contract_fragment_variants_differ_only_in_the_role_clause() -> None:
    # The variants share one head and one advisory tail, so every contract RULE
    # and the advisory framing stay byte-identical; only the execution-role
    # clause between them may differ. This is the no-drift property the single
    # constant used to give for free, now that there are two. Checker MANDATES
    # are deliberately not here — they live in the full-only CP-SAT workflow
    # prompt's step 7c, so neither variant carries them.
    def _shared_halves(fragment: str) -> tuple[str, str]:
        rules, _, rest = fragment.partition("- You generate and repair the script")
        _, _, advisory = rest.partition("This guidance is advisory")
        return rules, advisory

    assert _shared_halves(CPSAT_OUTPUT_CONTRACT_GUIDANCE) == _shared_halves(
        CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE
    )


def test_core_output_contract_fragment_is_brace_free_for_str_format() -> None:
    assert "{" not in CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE
    assert "}" not in CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE


@pytest.mark.asyncio
async def test_cpsat_prompt_carries_the_shared_output_contract_fragment() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_OUTPUT_CONTRACT_GUIDANCE in text


@pytest.mark.asyncio
async def test_auto_tune_prompt_carries_the_shared_output_contract_fragment() -> None:
    # The third CP-SAT generation route: it drafts candidates and rewrites them
    # per tier, so it needs the same stdout envelope contract as the other two.
    # Full variant, because this prompt registers in the full profile only.
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_OUTPUT_CONTRACT_GUIDANCE in text


def test_output_contract_fragment_is_brace_free_for_str_format() -> None:
    # Both host prompts are `str.format`ted with the user's problem text, so a brace
    # in the spliced fragment would surface as an unrelated-looking KeyError.
    assert "{" not in CPSAT_OUTPUT_CONTRACT_GUIDANCE
    assert "}" not in CPSAT_OUTPUT_CONTRACT_GUIDANCE


def test_output_contract_fragment_requires_a_complete_in_band_solution() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "`solution` must carry the COMPLETE, problem-specific answer" in normalized
    assert "never only a path to a result file the script wrote" in normalized


def test_output_contract_fragment_says_json_dumps_writes_no_file() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "`json.dumps` only serializes a Python object into a STRING" in normalized
    assert "It creates no file and saves nothing." in normalized


def test_output_contract_fragment_requires_one_shared_solution_schema() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "Variants of the SAME problem must share ONE `solution` schema" in normalized


def test_output_contract_fragment_permits_intermediate_objects() -> None:
    # The fragment used to demand "ONE JSON object" without exception, which
    # contradicts the improved-solution callback the detailed prompt asks for and
    # that the executor's timeout recovery reads. A core client never sees that
    # detailed prompt, so it needs the positive permission here.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "Emit a FINAL JSON object as the LAST stdout line" in normalized
    assert "Same-shaped intermediate objects ARE allowed" in normalized


def test_output_contract_fragment_ties_intermediate_objects_to_timeout_recovery() -> None:
    # Permission alone reads as trivia. The reason to spend the callback is that
    # it is the only way a timed-out run reports any answer at all.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "recover a partial answer when the run hits its timeout" in normalized


def test_output_contract_fragment_bounds_intermediate_output() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "Bound their cumulative bytes" in normalized
    assert "512 KiB" in normalized


def test_output_contract_fragment_scopes_the_error_status_to_a_clean_exit() -> None:
    # Unqualified, this promised an outcome the timeout path does not produce.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "On a CLEAN EXIT, a missing or invalid required key makes the whole run" in normalized
    assert '`status="error"`' in normalized


def test_output_contract_fragment_names_the_rejected_partial_keys_for_timeouts() -> None:
    # A client that cannot find `child_process_error` after a timeout otherwise has
    # no way to learn its partial was dropped on purpose, or why.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert 'On a TIMEOUT the status stays `"timeout"`' in normalized
    assert "`rejected_partial_field` / `rejected_partial_reason`" in normalized


def test_output_contract_fragment_requires_finite_numbers_at_any_depth() -> None:
    # The gate rejects non-finite floats anywhere in `solution`, which is a
    # breaking change for a script that emits them. It cannot ship undocumented.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "Every number anywhere in the payload must be FINITE" in normalized
    assert "at any depth inside `solution`" in normalized


def test_core_output_contract_fragment_promises_no_checker_the_core_toolset_lacks() -> None:
    # Core exposes only `run_cpsat_python` and `run_cpsat_python_file`, neither of
    # which takes a checker. The shared head reaches core verbatim, so it may
    # describe a checker as the STANDARD a solution must satisfy but must never
    # say the server runs or supplies one.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE_CORE.split())

    assert "the form any checker must be able to grade" in normalized
    for promise in ("a checker grades", "the checker can grade", "so a single checker grades"):
        assert promise not in normalized


def test_full_output_contract_fragment_still_describes_the_checker_backed_path() -> None:
    # Rewording the head must not cost the full profile its checker story; the
    # capability difference lives in the role clause, which full alone carries.
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "runs the checker you supply" in normalized


def test_output_contract_fragment_frames_prompt_text_as_advisory() -> None:
    normalized = " ".join(CPSAT_OUTPUT_CONTRACT_GUIDANCE.split())

    assert "You generate and repair the script; the server only executes it" in normalized
    assert "the deterministic guarantee starts only when an MCP execution tool" in normalized


def test_spine_fragment_names_the_spine_in_order() -> None:
    # The layout is an ORDER, not a set of names, so assert ascending positions
    # rather than membership.
    normalized = " ".join(CPSAT_SCRIPT_SPINE_GUIDANCE.split())

    positions = [
        normalized.index(f"`{name}()`")
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


def test_spine_fragment_keeps_status_logic_out_of_the_serializer() -> None:
    # "`solve()` owns the guards, the serializer only shapes" is a design split,
    # not a restatement of the guard bullet: a serializer-side guard would move
    # every OR-Tools read in front of its guard, and `objective_value` returns a
    # fabricated `0.0` rather than raising on an infeasible run.
    normalized = " ".join(CPSAT_SCRIPT_SPINE_GUIDANCE.split())

    assert "the `status` it prints is the one `solve()` already decided" in normalized


def test_spine_fragment_spells_out_the_entrypoint_guard() -> None:
    # "the standard module-name guard" is jargon a weaker model can miss; the
    # literal form is also what the workflow prompt's own example already ships,
    # so prose and example teach the same line. It carries no brace, so it is
    # still safe under the host prompt's `str.format`.
    normalized = " ".join(CPSAT_SCRIPT_SPINE_GUIDANCE.split())

    assert '`if __name__ == "__main__":` guard' in normalized


def test_spine_fragment_requires_a_typed_boundary() -> None:
    normalized = " ".join(CPSAT_SCRIPT_SPINE_GUIDANCE.split())

    assert "`parse_input()` hands `solve()` a typed instance record" in normalized
    assert "`solve()` hands `serialize_solution()` a typed solution record" in normalized


def test_input_fragment_forbids_reading_stdin() -> None:
    # A script that reads stdin gets an immediate EOF (the child runs with
    # stdin=DEVNULL), prints no envelope, and the whole run returns `error`.
    normalized = " ".join(CPSAT_SCRIPT_INPUT_GUIDANCE.split())

    assert "NEVER read stdin." in normalized
    assert "calls `input()` or reads `sys.stdin` gets an immediate EOF" in normalized


def test_input_fragment_binds_each_input_source_to_its_tool() -> None:
    # sys.argv and a relative open() are not available under the inline tool,
    # so the sources must never read as interchangeable.
    normalized = " ".join(CPSAT_SCRIPT_INPUT_GUIDANCE.split())

    assert "INLINE execution embeds the instance in the script itself" in normalized
    assert (
        "ARGV or RELATIVE-FILE input requires `run_cpsat_python_file` with "
        "`script_path` and `args`" in normalized
    )


def test_script_structure_halves_join_into_one_contiguous_bullet_list() -> None:
    # The spine and input halves are separate constants but ONE rendered block:
    # a blank line at the seam would split the prompt's bullet list in two, and
    # a missing newline would fuse the last spine bullet onto the stdin bullet.
    assert "\n\n" not in CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    assert CPSAT_SCRIPT_STRUCTURE_GUIDANCE.count("\n- NEVER read stdin.") == 1


def test_script_structure_fragment_is_brace_free_for_str_format() -> None:
    # Every host prompt is `str.format`ted with the user's problem text, so a
    # brace in the spliced fragment would surface as an unrelated-looking
    # KeyError.
    assert "{" not in CPSAT_SCRIPT_STRUCTURE_GUIDANCE
    assert "}" not in CPSAT_SCRIPT_STRUCTURE_GUIDANCE


def test_script_structure_fragment_names_no_full_only_tool() -> None:
    # The fragment is written to be spliced into routes the CORE profile also
    # serves, where the checked variant does not exist. The positive check
    # above cannot catch this: `in` is a substring match and
    # "run_cpsat_python_file" is a prefix of "run_cpsat_python_file_checked".
    assert "run_cpsat_python_file_checked" not in CPSAT_SCRIPT_STRUCTURE_GUIDANCE


@pytest.mark.asyncio
async def test_cpsat_prompt_carries_the_script_structure_fragment() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_SCRIPT_STRUCTURE_GUIDANCE in text


@pytest.mark.asyncio
async def test_backend_neutral_prompt_carries_the_script_structure_fragment() -> None:
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_SCRIPT_STRUCTURE_GUIDANCE in text


@pytest.mark.asyncio
async def test_full_backend_neutral_prompt_carries_the_script_structure_fragment() -> None:
    # One constant, both profiles: the fragment names no full-only tool, so
    # unlike the output-contract fragment it needs no profile-dependent variant.
    text = await _get_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_SCRIPT_STRUCTURE_GUIDANCE in text


@pytest.mark.asyncio
async def test_auto_tune_prompt_carries_the_script_structure_fragment() -> None:
    # Auto-tune drafts CP-SAT candidates and REWRITES each one at the tuning and
    # full-instance stages, so its rewrites must follow the same layout.
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})

    assert CPSAT_SCRIPT_STRUCTURE_GUIDANCE in text


def test_full_instructions_state_the_cpsat_output_contract_after_the_head() -> None:
    # The invariant is the 512-byte truncation head, not a paragraph ordinal:
    # everything preceding this paragraph must already fill the head, so the
    # routing + POSTURE lead survives truncation intact. This also proves the
    # requirement reached the instructions at all.
    paragraphs = MCP_SERVER_INSTRUCTIONS.split("\n\n")
    (index,) = [i for i, para in enumerate(paragraphs) if para.startswith("CP-SAT OUTPUT:")]

    preceding_bytes = len("\n\n".join(paragraphs[:index]).encode("utf-8"))
    assert preceding_bytes >= 512


def test_full_instructions_require_a_complete_in_band_solution() -> None:
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS.split())

    assert "`json.dumps` only builds a string" in normalized
    assert "`solution` must carry the COMPLETE, problem-specific answer" in normalized
    assert "This text is advisory" in normalized


# --- minizinc_solution_workflow --------------------------------------------


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_is_listed() -> None:
    mcp = create_mcp_server()
    prompts = await mcp.list_prompts()

    names = {prompt.name for prompt in prompts}
    assert "minizinc_solution_workflow" in names

    prompt = next(p for p in prompts if p.name == "minizinc_solution_workflow")
    argument_names = {arg.name for arg in (prompt.arguments or [])}
    assert "problem" in argument_names


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_echoes_user_problem() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_guides_minizinc_drafting() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    for substring in (
        "you",
        "draft",
        "MiniZinc",
        "check_minizinc_model",
        "solve_minizinc_model",
        "check-runtime",
        "install-runtime",
    ):
        assert substring in text, f"prompt missing required guidance: {substring!r}"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_steers_num_solutions_to_supported_solver() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The recommended flow defaults to cp-sat, which does not support num_solutions;
    # without explicit steering an "N solutions" request lands on the gated solver.
    assert "num_solutions" in text
    assert "org.gecode.gecode" in text
    assert "org.chuffed.chuffed" in text


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_guides_multiple_optimal_solutions() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    assert "multiple optimal solutions" in normalized
    assert "proven optimum" in normalized
    assert "solve satisfy" in normalized
    assert "num_solutions" in normalized


def test_backend_routing_presents_minizinc_and_cpsat_as_peers() -> None:
    # The two backends are peers with a when-to-use heuristic; no routing text
    # may reinstate a blanket "prefer X" default for natural-language problems.
    combined = (
        MCP_SERVER_INSTRUCTIONS
        + MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION
        + CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION
    )
    assert "prefer" not in combined.lower()

    # The server instructions route both backend prompts and both run paths.
    assert "minizinc_solution_workflow" in MCP_SERVER_INSTRUCTIONS
    assert "cpsat_python_solution_workflow" in MCP_SERVER_INSTRUCTIONS
    assert "run_cpsat_python" in MCP_SERVER_INSTRUCTIONS

    # Selection heuristic markers: CP-SAT Python (zero-install) vs MiniZinc
    # (rich globals, .dzn data, checker verification, portfolio racing).
    lower = MCP_SERVER_INSTRUCTIONS.lower()
    assert "zero-install" in lower
    assert "portfolio" in lower
    assert ".dzn" in MCP_SERVER_INSTRUCTIONS

    # Each prompt description names the other backend's prompt as its peer.
    assert "cpsat_python_solution_workflow" in MINIZINC_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION
    assert "minizinc_solution_workflow" in CPSAT_PYTHON_SOLUTION_WORKFLOW_PROMPT_DESCRIPTION


def test_both_instruction_variants_open_with_the_routing_paragraph() -> None:
    # The routing paragraph must survive client-side truncation, so it has to
    # be the very first thing in both variants, not merely present somewhere.
    routing_paragraph = (
        "For constraint programming or discrete optimization (scheduling, "
        "rostering, assignment, routing, knapsack, allocation, bin-packing, or "
        "model validation), use this MCP server before running solver code "
        "directly."
    )
    for instructions in (MCP_SERVER_INSTRUCTIONS, MCP_SERVER_INSTRUCTIONS_CORE):
        assert instructions.startswith(routing_paragraph)


def test_both_instruction_variants_retain_safety_disclosures() -> None:
    # These disclosures must reach the client even under truncation pressure,
    # so both the full and core profiles carry them, not just one.
    for instructions in (MCP_SERVER_INSTRUCTIONS, MCP_SERVER_INSTRUCTIONS_CORE):
        assert "managed local MiniZinc runtime" in instructions
        assert "bare PATH minizinc" in instructions
        assert "UNSANDBOXED" in instructions
        assert "server wrapper makes no network calls" in instructions
        assert "arbitrary code" in instructions


def test_mcp_server_instructions_route_num_solutions_and_multiple_optima() -> None:
    assert "num_solutions" in MCP_SERVER_INSTRUCTIONS
    assert "org.gecode.gecode" in MCP_SERVER_INSTRUCTIONS
    assert "org.chuffed.chuffed" in MCP_SERVER_INSTRUCTIONS
    assert "not the default `cp-sat`" in MCP_SERVER_INSTRUCTIONS
    assert "multiple optimal solutions" in MCP_SERVER_INSTRUCTIONS
    assert "objective fixed" in MCP_SERVER_INSTRUCTIONS


def test_full_instructions_route_long_runs_to_background_jobs() -> None:
    # Deciding whether to go async is a CROSS-tool call no single tool
    # description can make: the sync default is 30 s, and a client that just
    # raises `timeout_ms` hits its own tool-call limit instead, orphaning the
    # child. Every submit tool is named so no backend is left on the sync path.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS.split())

    assert "LONG RUNS:" in normalized
    for tool in (
        "submit_solve_job",
        "submit_portfolio_job",
        "submit_cpsat_python_job",
        "submit_cpsat_python_file_job",
    ):
        assert tool in normalized, f"instructions do not route long runs to {tool}"
    assert "poll the matching get_* tool" in normalized


def test_full_instructions_route_unsatisfiable_to_the_unsat_core_tools() -> None:
    # "unsatisfiable" is the most common now-what branch, and the untaught
    # reflex is to rewrite the model blind rather than compute a MUS first.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS.split())

    assert "find_unsat_core" in normalized
    assert "find_unsat_core_files" in normalized
    assert "before rewriting the model" in normalized


def test_full_instructions_route_a_missing_runtime_to_install_or_cpsat() -> None:
    # The core profile has always carried this recovery path; the full profile
    # must not offer LESS guidance on the most common first-run failure.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS.split())

    assert "check_runtime" in normalized
    assert "openconstraint-mcp install-runtime" in normalized
    assert "zero-install CP-SAT backend" in normalized


def test_full_instructions_route_spreadsheet_data_to_the_tabular_tools() -> None:
    # Without this the client reads the spreadsheet itself and retypes numbers
    # into the model, which is where transcription errors enter.
    normalized = " ".join(MCP_SERVER_INSTRUCTIONS.split())

    assert "load_tabular_data" in normalized
    assert "write_tabular_result" in normalized


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_does_not_recommend_bare_path_minizinc() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The managed-runtime invariant in AGENTS.md forbids recommending an
    # arbitrary `$PATH`-resolved `minizinc`. The fallback must route users
    # through the openconstraint-mcp CLI instead.
    assert "minizinc --solver cp-sat model.mzn" not in text, (
        "fallback must not recommend a bare PATH-based minizinc invocation"
    )


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_passes_through_brace_input() -> None:
    problem_with_braces = "Allocate workers across shifts {1..3} with budget constraints"

    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": problem_with_braces})

    assert problem_with_braces in text


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_preserves_local_first_boundary() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    for forbidden in (
        "the server will generate",
        "the server calls",
        "server-side LLM",
        "LangChain",
        "LangGraph",
    ):
        assert forbidden not in text, (
            f"prompt must not imply server-side LLM coupling: {forbidden!r}"
        )


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_orders_check_before_solve() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # Pin the order on the single recommended-loop line that names both tools,
    # not a whole-prompt first-index comparison: the execute step and CLI
    # walkthrough also mention solve_minizinc_model, so a global comparison
    # could pass or fail for the wrong reasons.
    loop_lines = [
        line
        for line in text.splitlines()
        if "check_minizinc_model" in line and "solve_minizinc_model" in line
    ]
    assert len(loop_lines) == 1, "expected one recommended-loop line naming both tools in order"
    loop_line = loop_lines[0]
    assert loop_line.index("check_minizinc_model") < loop_line.index("solve_minizinc_model")


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_notes_inline_data_for_check_and_solve() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The prompt as a whole still references inline data and names both tools.
    assert "data" in text
    assert "check_minizinc_model" in text
    assert "solve_minizinc_model" in text

    # There is a note that the same data flows to both the check and the solve.
    data_notes = [line for line in text.splitlines() if "data" in line and "both" in line]
    assert data_notes, "prompt should note passing the same data to both check and solve"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_routes_existing_files_to_file_tools() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # When the user already has MiniZinc files (.mzn + optional .dzn) on disk,
    # the prompt should route to the path-based tools and pass paths — not the
    # pasted file contents, which would break relative `include`s — validating
    # before solving.
    assert "check_minizinc_files" in text
    assert "solve_minizinc_files" in text
    assert "model_path" in text
    assert ".dzn" in text or "data_path" in text
    assert text.index("check_minizinc_files") < text.index("solve_minizinc_files"), (
        "the file branch should check before it solves"
    )


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_timeout_branch_does_not_auto_solve() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The timeout branch must not silently regress to "treat timeout as ok".
    # Anchor on stable keywords for the three options the LLM should offer the
    # user rather than exact prose: simplify, raise timeout_ms, or solve anyway.
    for keyword in ("timeout_ms", "simplify", "anyway"):
        assert keyword in text, f"timeout branch missing guidance: {keyword!r}"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_explains_result_fields() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The explain step must guide the LLM to read the new deterministic fields.
    # Keyword presence, not exact wording, to avoid brittleness. `timed_out` and
    # `return_code` are new to the prompt, so they pin the new caveat rather than
    # the pre-existing "timeout" mention in the validation branch.
    assert "statistics" in text
    assert "stdout" in text
    assert any(keyword in text for keyword in ("timed_out", "return_code")), (
        "explain step should note a timeout/return-code caveat"
    )


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_explains_structured_solution_fields() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The explain step must name the new structured SolveResult fields so the
    # client builds tables and comparisons from them rather than re-parsing
    # stdout. Backticked tokens pin the field references — plain "solution"
    # appears throughout the prose, so it would not prove the new fields.
    for field in ("`solution`", "`solutions`", "`objective`"):
        assert field in text, f"prompt should reference the structured field {field}"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_instructs_structured_result_presentation() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The explain step must frame the final answer as a structured summary the
    # client presents to the user, not just "interpret these fields". Pin the
    # framing on a single line that names both presenting and structure, so the
    # pre-existing "Present the complete MiniZinc model" line cannot satisfy it.
    presentation_lines = [
        line
        for line in text.splitlines()
        if "present" in line.lower() and "structured" in line.lower()
    ]
    assert presentation_lines, "explain step should instruct presenting a structured result summary"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_solution_block_is_status_conditioned() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The solution must be shown as a block read verbatim from stdout, never
    # paraphrased or inferred by the model.
    assert "verbatim" in text

    # Showing a solution is conditional on a solution-bearing status. The
    # unsatisfiable/error/timeout branch must say there is no solution to show
    # rather than fabricating one, so a line ties the two together.
    no_solution_lines = [
        line for line in text.splitlines() if "unsatisfiable" in line and "solution" in line
    ]
    assert no_solution_lines, (
        "explain step should note unsat/error/timeout have no solution block to show"
    )


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_leads_with_result_not_workflow_narration() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The user-facing answer must open with the result, not with MCP prompt,
    # workflow, or tool names. Pin the directive on a single "lead with" line.
    lead_lines = [line for line in text.splitlines() if "lead with" in line.lower()]
    assert lead_lines, "explain step should tell the client to lead with the result"

    lower = text.lower()
    # The "do not narrate internal names" instruction and its escape hatch.
    assert "narrat" in lower
    assert "workflow" in lower
    assert "implementation details" in lower


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_requires_statistics_when_present() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # Real clients dropped or compressed the statistics summary when the prompt
    # framed it as a soft, best-effort nicety. The directive must make the full
    # Statistics section non-optional whenever the `statistics` map is non-empty.
    stats_required_lines = [
        line
        for line in text.splitlines()
        if "statistics" in line.lower()
        and ("required" in line.lower() or "do not omit" in line.lower())
    ]
    assert stats_required_lines, (
        "explain step should require the Statistics section when the map is non-empty"
    )
    lower = text.lower()
    assert "copy the full section" in lower
    assert "selected fields" in lower


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_forbids_compressed_statistics() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    statistics_lines = [line.lower() for line in text.splitlines() if "statistics" in line.lower()]
    assert statistics_lines
    assert all("brief" not in line and "few" not in line for line in statistics_lines)
    assert "summarize it" in text.lower()


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_avoids_repeated_headings() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # A real client (Claude Code) emitted the "Solver statistics" heading twice.
    # The prompt must tell the client to use each heading at most once.
    heading_lines = [
        line
        for line in text.splitlines()
        if "heading" in line.lower() and ("once" in line.lower() or "repeat" in line.lower())
    ]
    assert heading_lines, "presentation guidance should forbid repeating section headings"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_avoids_speculative_commentary() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # A real client (Claude Code) padded the default answer with value-density
    # and greedy commentary. The prompt must discourage speculative algorithm
    # commentary by default while leaving an escape hatch when the user asks.
    commentary_lines = [
        line
        for line in text.splitlines()
        if "commentary" in line.lower() or "speculat" in line.lower()
    ]
    assert commentary_lines, (
        "presentation guidance should discourage speculative algorithm commentary"
    )
    assert "unless the user" in text.lower(), "the no-commentary default needs an escape hatch"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_requires_item_table_when_applicable() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # When the user problem supplies item-like data and the solution selects
    # among it, the client should render a compact table, not degrade to a
    # prose-only list. Small item sets should show all item rows.
    table_lines = [line for line in text.splitlines() if "table" in line.lower()]
    assert table_lines, "presentation guidance should require a table-style item summary"

    lower = text.lower()
    assert "item-like" in lower or "selected-item" in lower, (
        "the item-table guidance should be conditioned on item-like data"
    )
    assert "prose-only list" in lower
    assert "one row per item" in lower
    assert "selected/count" in lower


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_step6_broadens_hard_problem_exploration() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # Step 6 must frame exploration around the general "hard problem, best
    # approach not knowable in advance" case, not just "one solver too slow",
    # and must name the concrete portfolio knobs a client can vary.
    for needle in (
        "submit_portfolio_job",
        "get_portfolio_job",
        "symmetry-breaking",
        "seed_count",
        "free_search",
        "per_attempt_timeout_ms",
    ):
        assert needle in text, f"step 6 exploration guidance should mention {needle}"


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_step6_nudges_cross_backend() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # Step 6 should point at the CP-SAT Python path for an especially hard
    # instance, since neither backend dominates for every problem shape.
    assert "run_cpsat_python" in text


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_offers_save_only_on_user_request() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The save tool appears, but only as the optional post-success step gated
    # on the user's explicit ask — never as a required part of the solve loop.
    assert "save_verified_minizinc_model" in text
    normalized = " ".join(text.split())
    assert "asks" in normalized and "save" in normalized
    assert "only if" in normalized.lower()


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_save_step_follows_result_presentation() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The save mention lives after the result-presentation step, so it cannot
    # read as a pre-solve requirement.
    assert text.index("save_verified_minizinc_model") > text.index("Present the result")


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_save_step_mentions_portfolio_result() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The portfolio_result mention belongs in the save step, after the save
    # tool itself is introduced, not earlier as a pre-solve requirement.
    assert "portfolio_result" in text
    assert text.index("portfolio_result") > text.index("save_verified_minizinc_model")


@pytest.mark.asyncio
async def test_minizinc_solution_workflow_prompt_save_step_keeps_path_choice_client_side() -> None:
    text = await _get_prompt_text("minizinc_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The client obtains the explicit absolute directory from the user (or its
    # own picker); the server never opens a dialog — no OS UI is implied
    # server-side.
    save_block_lines = [
        line for line in text.splitlines() if "dialog" in line.lower() or "picker" in line.lower()
    ]
    assert save_block_lines, "save guidance should address who owns the path choice"
    assert "target_dir" in text
    assert "absolute" in text
    normalized = " ".join(text.split()).lower()
    assert "opens no file dialog" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_is_registered() -> None:
    mcp = create_mcp_server()
    prompts = await mcp.list_prompts()
    names = {p.name for p in prompts}
    assert "cpsat_python_solution_workflow" in names


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_substitutes_problem() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_mentions_run_cpsat_python() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    assert "run_cpsat_python" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_offers_script_path_attempts() -> None:
    # Step 6 must not hardcode a mandatory inline `source` any more: an attempt
    # may instead name an existing on-disk script, with `args`.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "`{name, source | script_path, args, seed, config, script_timeout_ms}`" in normalized
    assert "exactly one of `source`" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_warns_script_path_is_not_save_provenance() -> (
    None
):
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "cannot serve as `save_verified_cpsat_python` provenance" in normalized
    assert "accepted inline-`source` attempt matching this exact save" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_splits_deliver_all_from_select_one() -> None:
    # Two different multi-script intents: "deliver all" must inspect EVERY
    # attempt row and repair each failure, while "select one winner" keeps the
    # existing behavior of discarding rejected candidates.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "select one winner" in normalized
    assert "leave intentionally discarded candidates unrepaired" in normalized
    assert "deliver all of several requested scripts" in normalized
    assert "inspect every attempt row, not only `winner_index`" in normalized
    assert "never claim the deliverable is complete while any requested script's row" in normalized
    assert 'the server reports per-attempt acceptance and a winner, and has no "all attempts' in (
        normalized
    )


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_shares_a_checker_only_across_matches() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "the same problem, the same instance, and the same objective" in normalized
    assert "under the same objective sense, emitting one shared `solution` schema" in normalized
    assert "split mismatched scripts into separate calls" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_experiment_checker_is_a_predicate() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "standard-library predicate over the reported answer" in normalized
    assert "covers every element the instance requires" in normalized
    assert "the reported `objective` is consistent with the solution" in normalized
    assert "never `import ortools` and never re-solve" in normalized
    assert "not an independent proof that an `optimal` claim is globally optimal" in normalized


@pytest.mark.asyncio
async def test_auto_tune_prompt_still_selects_a_finalist_rather_than_passing_all() -> None:
    # The all-must-pass loop belongs to the CP-SAT workflow's "deliver all"
    # branch; auto_tune keeps selecting one finalist.
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "deliver all of several requested scripts" not in normalized
    assert "then present one full-instance result" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_teaches_seed_protocol() -> None:
    # The client-facing protocol must not drift from the env-var contract the
    # save replay relies on: read OPENCONSTRAINT_MCP_CPSAT_SEED, fall back to
    # 42, single worker by default. Keyed on a whole-prompt count (step 6's
    # mini-example plus both solve() examples) rather than the bare literal,
    # which is already present once in the unedited prompt and would measure
    # nothing.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in text
    assert "42" in text
    assert text.count('config.get("num_workers", 1)') == 3
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_defines_a_solver_config_helper() -> None:
    # Keyed on `_solver_config`, not on the env var name: the env var was
    # already read three times before this edit, so a guard phrased as
    # "the prompt mentions OPENCONSTRAINT_MCP_CPSAT_CONFIG" would be green
    # before any edit is made.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    assert "def _solver_config" in text
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" in text
    assert "return {}" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_both_examples_call_the_config_helper() -> None:
    # The half-edit guard: updating only the step-3 example (one definition,
    # one call) leaves this count at 1, not 2. Keyed on "= _solver_config()"
    # rather than the bare "_solver_config()" -- the bare token also matches
    # its own `def _solver_config() -> dict:` line and prose mentions naming
    # the helper (e.g. the streaming fence calling it out as a reused
    # dependency), so it isn't a count of call sites: it moves with unrelated
    # prose edits to those mentions, not just with calls being added or
    # dropped. Excluding the prose and the definition keeps the count pinned
    # to call sites only.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    assert text.count("= _solver_config()") == 2


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_time_limit_is_conditional() -> None:
    # solver_time_limit_seconds carries this test; num_workers defaulting to 1 is
    # confirmatory only, already covered by the seed-protocol test above.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "solver_time_limit_seconds" in text
    assert 'config.get("solver_time_limit_seconds")' in text
    assert "if solver_time_limit_seconds is not none" in normalized
    assert 'config.get("num_workers", 1)' in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_always_installs_the_streaming_callback() -> (
    None
):
    # The script cannot observe the executor's own deadline, so a CP-SAT search
    # limit never proves the solve returns before the kill. Gating the callback on
    # that limit discards every solution found whenever the limit does not fit
    # inside the executor budget, which nothing validates.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "callback = (" not in text
    assert "solver.solve(model, _best(names" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_still_teaches_direct_instance_values() -> None:
    # Config is for solver-run controls and explicit multi-attempt scenario
    # selection, not for a one-off problem instance's own values.
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "hardcode the actual parameter values" in normalized
    assert "config-driven instance selection is not the default modeling style" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_nudges_cross_backend() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The result-presentation step should point at the MiniZinc portfolio path
    # for an especially hard instance, since neither backend dominates for
    # every problem shape.
    assert "submit_portfolio_job" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_states_json_output_contract() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    # Must describe the required JSON output format
    assert '"status"' in text
    assert '"solution"' in text
    assert '"objective"' in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_forbids_network_and_file_mutation() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    assert "network" in lower
    assert "file" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_states_local_child_process_execution() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    assert "child process" in lower or "subprocess" in lower or "local" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_documents_save_gate_options() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    # All three gates must be named in the save step
    assert "reported" in text
    assert "expectation" in text.lower()
    assert "checker" in text.lower()
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_expectation_gate_no_optimality_proof() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    # The prompt must explicitly state the threshold is NOT a proof of global optimality.
    assert "does not prove" in lower or "not prove" in lower or "not an optimality proof" in lower
    # Must name both sense options
    assert "maximize" in lower
    assert "minimize" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_gate_payload_format() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    # Checker receives the payload path as sys.argv[1]
    assert "sys.argv[1]" in text
    # Payload keys that the checker must read
    assert "solver_status" in text
    assert "solution" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_gate_output_contract() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    # Checker must emit JSON with status/errors/details
    assert '"accepted"' in text or "accepted" in text
    assert '"rejected"' in text or "rejected" in text
    assert "errors" in text
    # Only accepted + empty errors is the passing verdict
    assert "empty" in text.lower()
    assert "passing" in text.lower() or "only" in text.lower()


@pytest.mark.asyncio
async def test_cpsat_workflow_step_three_permits_intermediate_objects() -> None:
    """The shared contract head is not the only place this rule is stated. Step 3 said
    "exactly ONE JSON object" while step 3's OWN callback snippet asks for
    intermediates — a contradiction inside one prompt, invisible to any assertion
    made against the head alone."""
    lower = await _cpsat_workflow_lower()

    assert "emit a final json object as the last line of stdout" in lower
    assert "same-shaped intermediate objects during search are allowed" in lower


@pytest.mark.asyncio
async def test_cpsat_workflow_step_three_requires_finite_numbers_at_any_depth() -> None:
    """The script-writing step is where the rule has to land to change what gets
    written; the contract head states it, but this is the step being followed."""
    lower = await _cpsat_workflow_lower()

    assert "every number in it must be finite at any depth" in lower
    assert "anywhere inside `solution` is rejected" in lower


@pytest.mark.asyncio
async def test_backend_neutral_prompt_does_not_forbid_intermediate_objects() -> None:
    """The backend-choosing prompt is served in the CORE profile and summarizes the
    CP-SAT artifact in one line. It said "prints one JSON object", contradicting the
    contract head spliced into the same prompt."""
    text = await _get_core_prompt_text("solve_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "prints a final json object as its last stdout line" in lower
    assert "prints one json object" not in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_splits_error_from_rejected() -> None:
    """`error` and `rejected` both fail the gate, so a client that reads them as one
    verdict is pointed at the wrong artifact: told "rejected" for a missing
    `solution` key, its plausible next move is to rewrite correct constraints."""
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "split the two failing verdicts your checker emits by what failed" in lower
    assert "`error` means the payload could not be graded at all" in lower
    assert (
        "`rejected` means a well-formed solution was graded against the "
        "instance and violates it" in lower
    )


# --- checker verdicts: two sections, asserted separately ---------------------
#
# The verdict explanation appears TWICE in this prompt — once in the experiment
# loop (step 6) and once in the checker-authoring rules (step 7c) — and both
# used to open with the same "`error` means the payload could not be graded at
# all" sentence. A whole-prompt substring assertion therefore passes on either
# one, which is how a stale copy survives an update. Each test below anchors on
# text unique to its section.


_EXPERIMENT_VERDICTS_ANCHOR = "read the three non-accepted verdicts differently"
_EXPERIMENT_RERUN_ANCHOR = "an attempt row does not carry the checker's own output"
_EXPERIMENT_SECTION_END = "persist only if the user asks"
_CHECKER_AUTHORING_ANCHOR = "reading the verdict back"


async def _cpsat_workflow_lower() -> str:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    return " ".join(text.split()).lower()


def _section(lower: str, start_anchor: str, end_anchor: str) -> str:
    """Slice the text between two anchors, so a later duplicate cannot satisfy a test.

    Slicing only from `start_anchor` to the end of the prompt would let step 7c's
    wording satisfy a step 6 assertion — the very collision these tests exist to
    prevent, since 7c comes after. Both anchors are asserted present, so this
    fails loudly rather than silently returning an empty section if either moves.
    """
    assert start_anchor in lower, f"missing section anchor: {start_anchor}"
    assert end_anchor in lower, f"missing section anchor: {end_anchor}"
    start = lower.index(start_anchor)
    end = lower.index(end_anchor, start)
    assert end > start, f"{end_anchor!r} must follow {start_anchor!r}"
    return lower[start:end]


@pytest.mark.asyncio
async def test_experiment_guidance_defines_error_as_no_valid_verdict() -> None:
    """`error` is also what the server reports when the checker itself failed to run,
    exited non-zero, or printed malformed output. Defining it as "the payload could
    not be graded" sends a client to repair a model that may be correct."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_VERDICTS_ANCHOR, _EXPERIMENT_RERUN_ANCHOR)

    assert "`error` means no valid verdict was produced" in section
    assert "the checker itself failed to run" in section
    assert "does not by itself accuse the model" in section


@pytest.mark.asyncio
async def test_experiment_guidance_keeps_rejected_pointing_at_the_model() -> None:
    """The `error` rewrite must not blur `rejected`, which is the one verdict that
    genuinely does indict the model's constraints."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_VERDICTS_ANCHOR, _EXPERIMENT_RERUN_ANCHOR)

    assert "`rejected` means a well-formed solution was graded" in section
    assert "points at the model's constraints" in section


@pytest.mark.asyncio
async def test_experiment_guidance_names_timeout_as_a_third_non_accepted_verdict() -> None:
    """`checker_status_is_failure` treats `timeout` as a failure too, so an attempt row
    can carry it today with no guidance at all."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_VERDICTS_ANCHOR, _EXPERIMENT_RERUN_ANCHOR)

    assert "`timeout` means the checker ran out of time" in section
    assert "not at the answer" in section


@pytest.mark.asyncio
async def test_experiment_guidance_admits_the_attempt_row_lacks_the_checker_report() -> None:
    """An attempt row carries `checker_status`, a short `message`, and a `diagnostic` —
    never the checker's own `errors`/`stdout`/`stderr`. Telling a client to read a
    report it cannot reach is worse than saying nothing."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_RERUN_ANCHOR, _EXPERIMENT_SECTION_END)

    assert "no `errors`, `stdout`, or `stderr`" in section


@pytest.mark.asyncio
async def test_experiment_guidance_escalates_to_a_seed_preserving_rerun() -> None:
    """A replay preserves every solve and checker input from the failed attempt."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_RERUN_ANCHOR, _EXPERIMENT_SECTION_END)

    assert "replaying its exact inputs" in section
    echoed = "the same `problem`, `seed`, `config`, `script_timeout_ms`, and `checker_timeout_ms`"
    assert echoed in section
    assert "save_verified_cpsat_python" in section
    assert "run_cpsat_python_file_checked" in section


@pytest.mark.asyncio
async def test_experiment_guidance_rules_out_the_checked_job_tools_for_the_rerun() -> None:
    """Neither `submit_cpsat_python_job` nor `submit_cpsat_python_file_job` accepts a
    `seed` or `config`, so for a configured attempt they silently grade a different
    run than the one being diagnosed."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _EXPERIMENT_RERUN_ANCHOR, _EXPERIMENT_SECTION_END)

    assert "do not use `submit_cpsat_python_job`" in section
    assert "they accept no `seed` or `config`" in section


@pytest.mark.asyncio
async def test_checker_authoring_guidance_reads_error_back_as_no_valid_verdict() -> None:
    """Step 7c tells the checker author which verdict to EMIT; reading one BACK is a
    different question, because the server normalizes infrastructure failures to
    `error` and adds `timeout` on its own. These tools DO return the full report."""
    lower = await _cpsat_workflow_lower()

    section = _section(lower, _CHECKER_AUTHORING_ANCHOR, "be a predicate, not a solver")

    assert "`error` means no valid verdict" in section
    assert "read the report's `errors`, `stdout`, and `stderr`" in section
    assert "`timeout`" in section


@pytest.mark.asyncio
async def test_cpsat_workflow_prompt_interprets_the_optional_checker_self_test() -> None:
    lower = await _cpsat_workflow_lower()

    assert "optional checker self-test" in lower
    assert "`rejected_count: 0, accepted_count: 0`" in lower
    assert "non-vacuity, not completeness" in lower
    assert "known-invalid payload" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_is_a_predicate_not_a_solver() -> None:
    """The checker child runs under `sys.executable` — the server's own venv, which
    ships `ortools` — so a checker CAN re-solve. One that does inherits the model's
    failure modes (timeout, memory, the same modeling bug) and stops being
    independent evidence exactly where the verdict matters."""
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "be a predicate, not a solver" in lower
    assert "never `import ortools` and never re-solve" in lower
    assert "inherits the failure modes it exists to catch" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_validates_cardinality() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "never accept vacuously" in lower
    assert "domain cardinalities disagree with the supplied data" in lower
    assert "a zero-cardinality domain is valid" in lower
    assert "check coverage" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_checker_gate_safety_boundary() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = text.lower()
    # The server executes the checker locally and does not sandbox it — this
    # must be documented so the client knows to generate safe validation code.
    assert "sandbox" in lower
    assert "network" in lower
    assert "local" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_combines_request_and_instance() -> None:
    """`problem` is both the checker payload's instance source and the value persisted
    as `problem.txt`, so a data-driven checker must not cost the user's original
    request — one flat JSON object carries both."""
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert (
        "pass as `problem` a single json object holding the machine-readable "
        "instance and the user's original request verbatim under its own key" in lower
    )
    assert "never drop the original request to make room for the instance" in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_accepts_object_or_text_problem() -> None:
    """The `problem` tool parameter serializes a JSON object for the caller, so the
    prompt must not tell the client that an object fails validation — that stale
    warning would push it into hand-serializing, the spelling that produced
    double-encoded text the checker cannot parse with one `json.loads`."""
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "send it either as that object or as its serialized text" in lower
    assert "fails tool validation" not in lower


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_discourages_replay_for_ordinary() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    # For a single problem instance, the prompt must steer toward a concrete,
    # self-contained script over a named scenario resolved via `config` — the
    # cooperative config protocol is reserved for explicit multi-attempt or
    # configured experiments, not the default modeling style for a one-off save.
    assert "single" in normalized and "hardcode" in normalized
    assert "not the default modeling style" in normalized
    assert "explicit multi-attempt" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_documents_file_replay_workflow() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # The manual replay workflow must route through the existing file tool
    # instead of promising a dedicated inspect/rerun tool, and must name the
    # checked-replay limitation plus its save-tool workaround.
    assert "run_cpsat_python_file" in text
    assert ".openconstraint-model.json" in text
    assert "replay-config.json" in text
    normalized = " ".join(text.split()).lower()
    assert "reported" in normalized and "level" in normalized
    assert "save_verified_cpsat_python" in text


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_replays_via_verify_only() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})

    # Full checked replay is a gate re-evaluation, so the prompt must point at
    # `verify_only=true` rather than telling the client to invent a scratch target.
    assert "verify_only=true" in text
    normalized = " ".join(text.split()).lower()
    assert "scratch `target_dir`, and" not in normalized
    # `verify_only=true` IGNORES a supplied target, so the prompt must never sell a
    # scratch target as the way to persist a replay — that needs `verify_only=false`.
    assert "scratch" not in normalized
    assert "ignores one if you pass it" in normalized
    assert "`verify_only=false`" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_replays_checker_inputs() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    assert "problem=…, checker=…, checker_timeout_ms=…, seed=…, config=…" in normalized
    assert "checker_path=…, problem=…, checker_timeout_ms=…, args=…" in normalized


@pytest.mark.asyncio
async def test_cpsat_python_solution_workflow_prompt_save_step_gated_on_user_request() -> None:
    text = await _get_prompt_text("cpsat_python_solution_workflow", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()
    # Save is optional — the user must ask
    assert "only if" in normalized or "if the user" in normalized
    # Save must not be framed as a required solve-loop step
    save_idx = text.index("save_verified_cpsat_python")
    run_idx = text.index("run_cpsat_python")
    assert save_idx > run_idx, "save step must appear after the run step"


def test_solve_descriptions_state_checker_suffix_and_nested_report() -> None:
    # The protocol descriptions must state plainly that checking is a solve option,
    # requires a `.mzc`/`.mzc.mzn` checker on the path side, and returns the nested
    # report fields clients need to inspect.
    combined = SOLVE_MINIZINC_MODEL_DESCRIPTION + SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "checker" in combined.lower()
    assert ".mzc" in SOLVE_MINIZINC_FILES_DESCRIPTION
    assert "CheckerReport" in combined
    assert "transcript" in combined


# --- auto_tune_constraint_problem ------------------------------------------


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_is_listed() -> None:
    mcp = create_mcp_server()
    prompts = await mcp.list_prompts()

    names = {prompt.name for prompt in prompts}
    assert "auto_tune_constraint_problem" in names

    prompt = next(p for p in prompts if p.name == "auto_tune_constraint_problem")
    argument_names = {arg.name for arg in (prompt.arguments or [])}
    assert "problem" in argument_names


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_echoes_user_problem() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    assert SAMPLE_PROBLEM in text


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_passes_through_brace_input() -> None:
    problem_with_braces = "Allocate workers across shifts {1..3} with budget constraints"

    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": problem_with_braces})

    assert problem_with_braces in text


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_smoke_precedes_tuning_race() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The tiny smoke check (step 5) must appear before either backend's
    # representative-tuning race (steps 9/10) — smoke never ranks or selects.
    smoke_idx = text.index("Create a tiny smoke instance")
    minizinc_race_idx = text.index("Select the PROVISIONAL MiniZinc candidate")
    cpsat_race_idx = text.index("Select the PROVISIONAL CP-SAT candidate")

    assert smoke_idx < minizinc_race_idx
    assert smoke_idx < cpsat_race_idx


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_minizinc_check_precedes_portfolio() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})

    # The smoke-stage MiniZinc check line (naming both inspect and check tools)
    # must precede the tuning-stage portfolio racing step.
    smoke_check_line = next(
        line
        for line in text.splitlines()
        if "check_minizinc_model" in line and "inspect_minizinc_model" in line
    )
    smoke_idx = text.index(smoke_check_line)
    portfolio_race_idx = text.index("Select the PROVISIONAL MiniZinc candidate")

    assert smoke_idx < portfolio_race_idx


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_smoke_offers_args_for_on_disk_script() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    # The on-disk smoke call shape must offer `args`, or the prompt steers the
    # client into whatever default instance the script hardcodes.
    file_call_idx = normalized.index("run_cpsat_python_file(script_path=")
    args_idx = normalized.index("add `args=[<data file>]`")
    assert file_call_idx < args_idx


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_submit_tools_name_matching_poll_tools() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())

    # Each background submit tool must be paired with its own matching getter.
    assert "submit_portfolio_job` polls with `get_portfolio_job`" in normalized
    assert "submit_solve_job` polls with `get_solve_job`" in normalized
    assert "submit_cpsat_python_job` polls with `get_cpsat_python_job`" in normalized


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_explicit_save_paths() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split()).lower()

    assert "target_dir" in text
    assert "absolute" in normalized
    assert "only when the user asks" in normalized


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_cpsat_default_safety() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    # Every drafted/rewritten CP-SAT candidate carries the same no-network,
    # no-file-mutation default as the single-backend cpsat_python_solution_workflow prompt.
    assert "no network access, no file writes or deletes" in lower
    assert "unless the user explicitly requested it" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_per_candidate_portfolio_jobs() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert (
        "submitting one `submit_portfolio_job` call per smoke-surviving minizinc candidate" in lower
    )
    assert (
        "never race multiple candidate formulations inside one `submit_portfolio_job` call" in lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_ranks_feasibility_without_objective() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # A pure `solve satisfy;` problem has no objective (SolveResult.objective
    # is null), so ranking tuning-stage MiniZinc candidates by "best objective"
    # only applies to optimization; feasibility candidates rank by status
    # instead.
    assert "for an optimization problem, rank by best `objective`, then elapsed time" in lower
    assert "for a pure feasibility (`solve satisfy;`) problem there is no `objective`" in lower
    assert "rank by `status` instead" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_states_portfolio_racing_reason() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    # The reason a single formulation per submit_portfolio_job call is required:
    # first-decisive-result treats unsatisfiable/unbounded as decisive, and the
    # checker verdict never gates that selection, so a buggy candidate could win.
    assert "first-decisive-result" in text
    assert "unsatisfiable" in lower
    assert "unbounded" in lower
    assert "decisive" in lower
    assert "observational" in lower
    assert "buggy formulation could otherwise" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_cpsat_racing_not_split_per_candidate() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Unlike MiniZinc portfolio racing, run_cpsat_python_experiment's own
    # acceptance gate (solution required; checker-accepted when supplied)
    # already excludes an incorrect formulation, so one call across candidates
    # is safe and per-candidate calls are not required.
    assert "one `run_cpsat_python_experiment` call across the smoke-surviving cp-sat" in lower
    assert "not required to split into per-candidate calls" in lower
    assert "present solution" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_offers_script_path_attempts() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "`{name, source | script_path, args, seed, config, script_timeout_ms}`" in lower
    assert "exactly one of a complete, independent inline `source` or a `script_path`" in lower
    # The tool gained a path option on attempts only — checker/problem stay inline.
    assert "`checker`/`problem` stay inline text for the whole call" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_inline_source_for_save_provenance() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "a `script_path` attempt is never accepted as save provenance" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_backend_local_winner_selection() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "choose winners within a backend only" in lower
    assert "never merge candidates from both backends into one race" in lower

    # Cross-backend comparison is gated on matching objective and sense, and
    # a mismatch defers to the user rather than an auto-picked winner.
    assert (
        "compare each backend's final, checker-validated result across "
        "backends only when both represent the same objective and "
        "objective sense" in lower
    )
    assert (
        "when the objectives or senses don't match, ask the user which "
        "backend/result to keep instead of picking one yourself" in lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_race_checker_must_discriminate() -> None:
    """The race attaches a checker precisely to keep an incorrect formulation from
    winning. A checker that re-solves or accepts malformed instance data passes every
    candidate alike — the race then ranks on speed with correctness unchecked."""
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert "each checker is a predicate that grades the emitted solution" in lower
    assert "re-solves the problem inherits the very failure it exists to catch" in lower
    assert "accepts missing or cardinality-inconsistent instance data" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_recheck_uses_bounded_solve_not_check() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # The full-instance re-check must use a bounded solve call, never the
    # compile-only check tools, quoting their own "compiles, not satisfiable"
    # disclaimer as the reason.
    assert "a bounded `solve_minizinc_model`/`solve_minizinc_files` call" in lower
    assert "never `check_minizinc_model`/`check_minizinc_files`" in lower
    assert "`ok` means it compiles, not that it is satisfiable" in normalized

    # CP-SAT's re-check must go through a CHECKED background job, since
    # run_cpsat_python (the inline tool) has no checker parameter at all.
    assert "submit_cpsat_python_job` with the checker" in normalized
    assert "poll `get_cpsat_python_job` until terminal" in lower
    assert "no `checker` parameter at all" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_recheck_pass_fail_inconclusive_gate() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Stop on unsatisfiable/error/checker-violation; proceed-but-flag on
    # timeout/unknown, since that is inconclusive rather than a hard failure.
    assert "stop and report the failure to the user instead of proceeding to the" in lower
    assert "inconclusive" in lower
    assert "proceed to the final solve, but flag that the" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_recheck_stop_gate_is_backend_specific() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # CP-SAT's status vocabulary has no "unsatisfiable" value (it uses
    # "infeasible" instead), so the stop gate must name each backend's own
    # failure status rather than checking one literal for both.
    assert "minizinc's `unsatisfiable`/`error`" in lower
    assert "cp-sat's `infeasible`/`error`" in lower
    assert "cp-sat's status vocabulary has no `unsatisfiable` value" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_recheck_requires_clean_checker_pass() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Once a solution was produced, only a clean "completed"/"accepted"
    # checker outcome counts as verified; a genuine checker error/timeout or
    # an explicit violation/rejection must stop the re-check.
    assert 'checker.status == "completed"' in lower
    assert 'checker.status == "accepted"' in lower
    assert "clean pass to count as verified" in lower
    assert "has no inconclusive middle ground" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_recheck_exempts_no_incumbent_checker() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # A timeout/unknown re-check with NO incumbent has nothing for the
    # checker to check (MiniZinc: checker.status "no_solution"; CP-SAT: a
    # skipped checker) — that is the EXPECTED outcome in this case, not a
    # separate failure, so it must not trip the checker clean-pass gate and
    # contradict the status gate's own "proceed but flag" instruction.
    assert "no incumbent solution is inconclusive" in lower
    assert "a `checker.status` of `no_solution`" in lower
    assert "sets `checker_skipped_reason` instead of running `checker`" in lower
    assert "do not apply the checker gate below to it" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_gates_final_presentation_on_checker() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # submit_portfolio_job/submit_solve_job/submit_cpsat_python_job all treat
    # their checker verdict as observational — none refuses a checker-violated
    # result — so the prompt itself must gate presentation on that verdict
    # rather than trusting the tools to have already done so.
    assert "checker_status` is observational" in lower
    assert "`solveresult.checker` and" in lower
    assert "stop and report the violation to the user instead of presenting the result" in lower
    assert "automatically satisfied whenever that path was used" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_final_presentation_requires_clean_checker() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Once a solution was produced, a checker "error"/"timeout" or an
    # explicit violation/rejection must also fail this gate, not just a
    # nominal "not a clean pass" reading of any outcome.
    assert "clean pass to count as verified" in lower
    assert '`checker.status` of exactly `"completed"`' in lower
    assert '`checker.status` of exactly `"accepted"`' in lower
    assert "correctness was not confirmed" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_final_presentation_exempts_no_incumbent() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # The same no-incumbent exemption applies to the final terminal result:
    # a timeout/unknown result with no solution has nothing for the checker
    # to check, so that outcome must not block presenting it (flagged as
    # unproven) — the checker requirement only binds once a solution exists.
    assert "nothing for the checker to check" in lower
    assert "reports `checker.status` of `no_solution`" in lower
    assert "sets `checker_skipped_reason` instead of `checker`" in lower
    assert "present that result (flagged as unproven)" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_smoke_reject_only_tuning_separate() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "use it only to reject structurally broken candidates" in lower
    assert "this step never ranks or selects a winner among the candidates that pass" in lower
    assert "create a separate, larger representative tuning instance" in lower
    assert "never rank or select using the smoke instance's results" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_smoke_tuning_not_used_as_provenance() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert (
        "only the full-instance final run's result is ever presented to the user "
        "or used as save-tool provenance" in lower
    )
    assert (
        "do not present a provisional candidate as the answer, and do not use its "
        "result as save-tool provenance" in lower
    )
    assert "never a smoke or representative-tuning result" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_checker_for_multi_candidate() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "draft a checker whenever more than one candidate is being compared, not" in lower
    assert (
        "a checker is what stops an incorrect formulation from winning the "
        "tuning-stage race" in lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_two_checkers_cross_backend() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "draft two backend-specific checkers that enforce the same problem constraints" in lower
    assert "not interchangeable source" in lower
    assert "inline minizinc solution-checker source" in lower
    assert "reads a payload json path from `sys.argv[1]`" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_rebuilds_problem_payload_per_tier() -> None:
    """Auto-tune solves a DIFFERENT instance per tier, so a data-driven CP-SAT checker's
    combined request+instance payload has to be rebuilt for each tier. Handing a
    tuning-stage run the full instance would make the checker reject a correct solution."""
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    lower = " ".join(text.split()).lower()

    assert 'a cp-sat checker that reads its instance from `payload["problem"]`' in lower
    assert "pass as `problem` one flat json object" in lower
    assert (
        "carrying both the user's original request verbatim and "
        "the machine-readable instance that run solves" in lower
    )
    assert "each tier solves a different instance, so rebuild that value per tier" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_ties_provenance_fields_to_specific_tools() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "its `solveresult` carries no `portfolio_result` field" in lower
    assert "a final run made through `submit_solve_job` has no `portfolio_result` to pass" in lower
    assert (
        "a final run made through `submit_cpsat_python_job` has no `experiment_result` to pass"
        in lower
    )
    assert "the synchronous `run_cpsat_python_experiment`" in normalized


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_save_provenance_conditional_on_finalist() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "plus `portfolio_result` only when the final run used `submit_portfolio_job`" in lower
    assert (
        "plus `experiment_result` only when the final run used the synchronous "
        "`run_cpsat_python_experiment`" in lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_save_replays_checker_not_just_provenance() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Provenance (`portfolio_result`/`experiment_result`) never re-runs or gates on a
    # checker; only a `checker` argument passed directly to the save call does.
    assert "`portfolio_result`/`experiment_result` are provenance only" in lower
    assert (
        "when the same `checker` you attached to the finalist run is passed directly "
        "to the save call itself" in lower
    )
    assert "dropping `checker` from the save call silently saves at a weaker" in lower
    # Both backend save bullets must carry the finalist's checker and problem forward,
    # not only their provenance object. The backends differ in what `problem` may hold:
    # a MiniZinc checker never reads it (it reads the `.dzn` interface), so that bullet
    # keeps the original text, while CP-SAT's must replay the finalist run's value —
    # which is the combined request+instance JSON when the checker parses it.
    assert lower.count("the same `checker` (when one was drafted for the finalist run)") == 2
    assert "the original problem text as `problem`" in lower
    assert "as `problem` the same value the full-instance finalist run used" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_names_search_space_reduction_techniques() -> (
    None
):
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Candidates must vary along an axis that actually changes search-space
    # size, not just cosmetic structure.
    assert (
        "vary something that actually changes the search space, not just cosmetic "
        "structure" in lower
    )
    assert "symmetry breaking" in lower
    assert "draft one candidate with symmetry breaking and one without" in lower
    assert "implied/redundant constraints" in lower
    assert "global vs. decomposed constraints" in lower
    assert "variable domain tightening" in lower
    assert (
        "do not draft candidates that differ only in variable naming, constraint "
        "ordering, or code style" in lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_names_search_strategy_techniques() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    # Search strategy (exploration order) is distinct from search-space size.
    assert (
        "search strategy is a second, complementary axis, distinct from search space size" in lower
    )
    # MiniZinc: restart annotations paired with seed racing, solver-gated.
    assert "restart_luby" in lower
    assert "restart_geometric" in lower
    assert "only gecode/chuffed honor restart annotations" in lower
    assert (
        "cp-sat ignores them and runs its own restarts, so pair a restart-annotated "
        "candidate with a restart-aware solver in `solvers`, not with `org.cp-sat`" in lower
    )
    # CP-SAT: num_workers enables automatic LNS/restarts; no hand-rolled LNS.
    assert "solver.parameters.num_workers` above 1" in lower
    assert "already includes automatic lns and restarts" in lower
    assert "do not draft a custom fix-and-reoptimize lns loop" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_shared_dzn_interface() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "fix one shared `.dzn` parameter interface" in lower
    assert "the parameter interface itself stays fixed" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_requires_cpsat_rewrite_each_stage() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "it will be rewritten, not reused verbatim, at the representative" in lower
    assert "rewritten with the representative tuning instance's values hardcoded" in lower
    assert "rewrite the provisional approach with the full instance's" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_includes_existing_model_as_candidate() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "review it" in lower
    assert "include it as one candidate formulation in the drafted set" in lower
    assert "do not ignore it, and do not treat it as the only candidate" in lower


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_never_overwrites_original_except_save() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "must never be rewritten to fit it" in lower
    assert "the original file is never overwritten in place by a" in lower
    assert "the only write to the original file's path remains the explicit final save step" in (
        lower
    )


@pytest.mark.asyncio
async def test_auto_tune_constraint_problem_prompt_non_parameterized_mzn_needs_permission() -> None:
    text = await _get_prompt_text("auto_tune_constraint_problem", {"problem": SAMPLE_PROBLEM})
    normalized = " ".join(text.split())
    lower = normalized.lower()

    assert "hardcodes instance data instead of reading it from a `.dzn`" in lower
    assert "cannot scale through data values alone" in lower
    assert "ask the user before deriving a parameterized copy for multi-scale racing" in lower


def test_auto_tune_constraint_problem_named_in_instructions_and_sibling_prompts() -> None:
    # Mirrors test_backend_routing_presents_minizinc_and_cpsat_as_peers: a client
    # without prompt-listing support must still be able to find the auto-tune
    # prompt from the server instructions or either single-backend prompt.
    assert "auto_tune_constraint_problem" in MCP_SERVER_INSTRUCTIONS
    assert "auto_tune_constraint_problem" in MINIZINC_SOLUTION_WORKFLOW_PROMPT
    assert "auto_tune_constraint_problem" in SOLVE_CPSAT_PYTHON_PROMPT

    assert "minizinc_solution_workflow" in AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION
    assert "cpsat_python_solution_workflow" in AUTO_TUNE_CONSTRAINT_PROBLEM_PROMPT_DESCRIPTION
