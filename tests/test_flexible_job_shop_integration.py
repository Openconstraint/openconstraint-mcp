"""Real-subprocess smoke test for the flexible job shop example workflow.

``examples/flexible_job_shop/checker.py`` advertises a specific way to be run:
`payload["problem"]` may name a data file, which the checker resolves next to
its own `__file__` -- and that only works under a PATH-BASED checker run
(`checker_path`, i.e. `run_cpsat_python_file_checked` or
`submit_cpsat_python_file_job`). ``tests/test_flexible_job_shop_checker.py``
covers the grading logic by importing the checker directly, and proves the
NEGATIVE half of that claim (a copied checker cannot resolve the filename), but
importing a function never exercises the tool that is supposed to invoke it.

This test closes the positive half end to end, with no mocks: the real MCP tool
spawns the real model script, parses its real stdout, builds the checker payload
from it, and spawns the real checker in place. The seam it guards is the one
between two separately-tested artifacts -- a model script's printed `solution`
object and ``checker.py``'s expectations of it -- which every mocked test of
``run_cpsat_python_file_checked`` supplies both sides of and therefore cannot
check. That seam has broken before: the models once printed a SUMMARY of the
schedule rather than the schedule itself, the regression
``test_compact_summary_solution_yields_error_status`` was written for. It is
parametrized over all six formulations because that history is per-file, not
per-directory: each model script owns its own copy of the output tail, so a
mistake in one script's copy needs its own run to catch, not a neighbor's.

Marked ``integration``: it spawns real children (excluded from ``just check``,
run with ``just integration``). It needs no managed MiniZinc runtime -- the
CP-SAT path runs on ``sys.executable``, whose venv ships ``ortools``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.server import create_mcp_server

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "flexible_job_shop"

# The proven optimum of the 10x6 Brandimarte mk01 instance. Every parametrized
# script here reaches it in ~0.1s single-worker at seed 42, so the 10s in-model
# cap below is a wide margin rather than a race -- the assertion pins a property
# of the INSTANCE, not a solver-performance timing.
_MK01_OPTIMUM = 40


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script_name", "formulation"),
    [
        ("model.py", "optional_intervals"),
        ("model_direct_optional_intervals.py", "direct_optional_intervals"),
        ("model_pairwise_disjunctive.py", "pairwise_disjunctive"),
        ("model_redundant_bounds.py", "redundant_bounds"),
        ("model_composite.py", "composite"),
        # The one *search-order* ablation (add_decision_strategy over the direct
        # optional-interval encoding) -- non-trivial solver behavior that no other
        # test exercises, so it gets its own share of this seam check rather than
        # relying on model.py alone to stand in for every formulation.
        ("model_earliest_start_branching.py", "earliest_start_branching"),
    ],
)
async def test_mk01_model_and_checker_reach_an_accepted_verdict_through_the_mcp_tool(
    script_name: str,
    formulation: str,
) -> None:
    mcp = create_mcp_server("full")

    # Note the two independent channels: `args` tells the MODEL which instance to
    # solve, `problem` tells the CHECKER which instance to grade against. The
    # model does not get to pick its own ground truth. Both are bare filenames
    # resolved next to their own script, which is exactly what the path-based
    # tool's cwd contract makes work. The results-dir argument is deliberately
    # omitted so the run writes nothing into the checkout.
    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(_EXAMPLE_DIR / script_name),
            "checker_path": str(_EXAMPLE_DIR / "checker.py"),
            "args": ["data_mk01.json", "10"],
            "problem": "data_mk01.json",
            "script_timeout_ms": 60_000,
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    checker: dict[str, Any] = result["checker"]

    assert result["status"] == "optimal"
    assert result["objective"] == _MK01_OPTIMUM
    assert checker["status"] == "accepted", checker["errors"]
    # The checker graded the SIBLING data file, not inline JSON -- the filename
    # channel this whole path-based workflow exists for.
    assert checker["details"]["instance_source"] == "file:data_mk01.json"

    # `_extract_solution_objective` (pyexec/core.py:496-512) does not put `stats`
    # into the structured CpsatPythonResult -- `result["stats"]` does not exist,
    # so no assertion on `result` alone can check it. Parse the LAST line of raw
    # stdout directly: this is what actually proves Decision #3's instance_name
    # threading and Decision #7's rewrite didn't drop or misname a field.
    stdout_lines = result["stdout"].strip().splitlines()
    payload = json.loads(stdout_lines[-1])
    assert payload.keys() == {"status", "objective", "solution", "best_objective_bound", "stats"}
    solution = payload["solution"]
    assert solution.keys() == {"makespan", "schedule", "instance", "num_tasks"}
    assert solution["instance"] == "data_mk01.json"
    assert solution["num_tasks"] == len(solution["schedule"])
    stats = payload["stats"]
    assert stats["formulation"] == formulation
    assert stats["instance"] == "data_mk01.json"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_results_dir_write_matches_final_stdout(tmp_path: Path) -> None:
    """Nothing else exercises the opt-in RESULTS_DIR write path
    (`RESULT_PATH.write_text(...)`, only triggered when the CLI's 3rd arg names
    a directory). The write-side wiring (`write_output`'s
    `results_dir`/`data_path`/`formulation` params, Decisions #3/#6) is
    identical across all six variants, so this representative case on the
    canonical `model.py` alone is enough -- it exercises the shared side-effect
    mechanism, not per-file model logic."""
    mcp = create_mcp_server("full")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            # An absolute 3rd argument is taken as given (model.py's own
            # docstring), so no cwd-relative resolution games are needed.
            "args": ["data_mk01.json", "10", str(tmp_path)],
            "script_timeout_ms": 60_000,
        },
    )
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    assert result["status"] == "optimal"

    result_path = tmp_path / "optional_intervals__data_mk01.json"
    assert result_path.exists()
    written = json.loads(result_path.read_text(encoding="utf-8"))
    payload = json.loads(result["stdout"].strip().splitlines()[-1])

    assert written["objective"] == payload["objective"]
    assert written["solution"] == payload["solution"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_env_var_changes_solver_search_stats() -> None:
    """Decision #4 replaces the hardcoded `random_seed = 42` with an
    `OPENCONSTRAINT_MCP_CPSAT_SEED` read. mk01 is small enough that
    status/objective are seed-invariant, so Tasks 4/10's assertions can't
    catch a script that still hardcodes 42 -- this instead compares the
    solver's own search counters between a default-seed run and a
    distinctly-seeded run. If both come out identical, that is a FAILURE this
    test must catch: it means the seed parameter never reached
    `solver.parameters.random_seed`."""
    mcp = create_mcp_server("full")

    default_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "args": ["data_mk01.json", "10"],
            "script_timeout_ms": 60_000,
        },
    )
    seeded_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "args": ["data_mk01.json", "10"],
            "script_timeout_ms": 60_000,
            "seed": 12345,
        },
    )
    assert default_result.structured_content is not None
    assert seeded_result.structured_content is not None
    default_payload = json.loads(
        default_result.structured_content["stdout"].strip().splitlines()[-1]
    )
    seeded_payload = json.loads(seeded_result.structured_content["stdout"].strip().splitlines()[-1])

    assert default_payload["status"] == "optimal"
    assert seeded_payload["status"] == "optimal"
    assert default_payload["objective"] == _MK01_OPTIMUM
    assert seeded_payload["objective"] == _MK01_OPTIMUM

    default_stats = default_payload["stats"]
    seeded_stats = seeded_payload["stats"]
    assert (
        default_stats["num_conflicts"] != seeded_stats["num_conflicts"]
        or default_stats["num_branches"] != seeded_stats["num_branches"]
    ), "seed had no observable effect on solver search -- the seed may not be reaching random_seed"
