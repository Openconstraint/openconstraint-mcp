"""Real-subprocess smoke test for the job shop example workflow.

``examples/job_shop/checker.py`` needs the LITERAL instance JSON text as
``payload["problem"]`` -- unlike ``examples/flexible_job_shop/checker.py``, its
``_parse_instance`` calls ``json.loads(problem)`` directly, with no bare-filename
resolution. ``tests/test_job_shop_checker.py`` covers the grading logic by
importing the checker directly, but never exercises the tool that is supposed to
invoke it.

This test closes that gap end to end, with no mocks: the real MCP tool spawns
the real model script, parses its real stdout, builds the checker payload from
it, and spawns the real checker in place. The seam it guards is the one between
two separately-tested artifacts -- a model script's printed ``solution`` object
and ``checker.py``'s expectations of it -- which every mocked test of
``run_cpsat_python_file_checked`` supplies both sides of and therefore cannot
check. It is parametrized over both scripts in this directory because each one
owns its own copy of the output tail, so a mistake in one script's copy needs
its own run to catch, not a neighbor's --
``tests/test_flexible_job_shop_integration.py`` makes the same point for the
sibling directory. Two further tests below cover this family's halves of the
seed-env-var and best_objective_bound-guard checks that file also carries for
``flexible_job_shop``.

Marked ``integration``: it spawns real children (excluded from ``just check``,
run with ``just integration``). It needs no managed MiniZinc runtime -- the
CP-SAT path runs on ``sys.executable``, whose venv ships ``ortools``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult

from openconstraint_mcp.server import create_mcp_server

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "job_shop"

# problem.txt's documented optimum for the 6x6 Fisher & Thompson benchmark.
# Every parametrized script here reaches it in well under a second
# single-worker at seed 42, so the 20s in-model timeout below is a wide margin
# rather than a race -- the assertion pins a property of the INSTANCE, not a
# solver-performance timing.
_FT06_OPTIMUM = 55
_FT06_NUM_TASKS = 36


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "script_name",
    [
        "model.py",
        "model_pairwise_disjunctive.py",
    ],
)
async def test_ft06_model_and_checker_reach_an_accepted_verdict_through_the_mcp_tool(
    script_name: str,
) -> None:
    mcp = create_mcp_server("full")

    # job_shop's checker needs the LITERAL instance JSON text, not a bare
    # filename -- its `_parse_instance` calls `json.loads(problem)` directly.
    problem_text = (_EXAMPLE_DIR / "data_ft06.json").read_text(encoding="utf-8")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(_EXAMPLE_DIR / script_name),
            "checker_path": str(_EXAMPLE_DIR / "checker.py"),
            "args": ["data_ft06.json"],
            "problem": problem_text,
            "script_timeout_ms": 20_000,
        },
    )
    assert isinstance(call_result, CallToolResult)
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    checker: dict[str, Any] = result["checker"]

    assert result["status"] == "optimal"
    assert result["objective"] == _FT06_OPTIMUM
    assert checker["status"] == "accepted", checker["errors"]

    # `_extract_solution_objective` (pyexec/core.py) only pulls
    # status/objective/solution/best_objective_bound into the structured
    # CpsatPythonResult -- it doesn't check the `solution` dict's own keys. Parse
    # the LAST line of raw stdout directly to prove `serialize_solution()` didn't
    # drop or rename a key during the reshape.
    stdout_lines = result["stdout"].strip().splitlines()
    payload = json.loads(stdout_lines[-1])
    assert payload.keys() == {"status", "objective", "solution", "best_objective_bound"}
    assert payload["solution"].keys() == {"makespan", "schedule"}
    assert len(payload["solution"]["schedule"]) == _FT06_NUM_TASKS


@pytest.mark.integration
@pytest.mark.asyncio
async def test_seed_env_var_changes_solver_search_result(tmp_path: Path) -> None:
    """A tiny seed-sensitive instance proves the env var reaches CP-SAT."""
    mcp = create_mcp_server("full")
    probe_path = tmp_path / "seed_probe.json"
    probe_path.write_text(
        json.dumps(
            {
                "num_machines": 4,
                "jobs": [
                    [[0, 6], [1, 7], [3, 9], [2, 6]],
                    [[2, 1], [1, 3], [3, 3], [0, 1]],
                    [[1, 1], [0, 7], [3, 1], [2, 5]],
                    [[1, 6], [2, 2], [0, 7], [3, 1]],
                ],
            }
        ),
        encoding="utf-8",
    )

    default_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "args": [str(probe_path)],
            "script_timeout_ms": 10_000,
        },
    )
    seeded_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "args": [str(probe_path)],
            "script_timeout_ms": 10_000,
            "seed": 12345,
        },
    )
    assert isinstance(default_result, CallToolResult)
    assert isinstance(seeded_result, CallToolResult)
    assert default_result.structured_content is not None
    assert seeded_result.structured_content is not None
    default_payload = default_result.structured_content
    seeded_payload = seeded_result.structured_content

    assert default_payload["status"] == "optimal", default_payload
    assert seeded_payload["status"] == "optimal", seeded_payload
    assert default_payload["objective"] == 29
    assert seeded_payload["objective"] == 29
    assert default_payload["solution"]["schedule"] != seeded_payload["solution"]["schedule"], (
        "seed had no observable effect on solver search -- the seed may not be reaching random_seed"
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_timeout_never_fabricates_best_objective_bound() -> None:
    """Decision #5 adds a guarded `best_objective_bound` to this directory's
    two scripts (they emitted no such key at all before this refactor): a
    real value on `optimal`/`feasible`/`unknown`, `null` otherwise.
    Exercising the guard's `unknown` branch the way
    `test_flexible_job_shop_integration.py` does needs the SCRIPT's own
    `solver.solve()` call to return CP-SAT's UNKNOWN status internally, which
    needs an in-model time cap -- and unlike the flexible family, job_shop's
    scripts take no such CLI argument (their CLI surface stays
    `[data_file.json]` only, unchanged by this refactor per Decision #6).
    Job shop's problem class also cannot produce `infeasible` (no due dates,
    unbounded horizon -- a schedule always exists given enough time), so
    neither branch the guard discriminates on is reachable from the script's
    OWN solver call without adding new CLI surface, which is out of scope.

    The reachable non-terminal state through the MCP tool is instead the
    EXECUTOR's own wall-clock timeout (`script_timeout_ms`), forced here with a tiny
    budget against ft20 (20x20, the largest job_shop instance) so the child is
    killed before it ever prints an envelope. This does not exercise the new
    in-script guard branch -- nothing of the script runs to completion -- but
    it is the closest available proxy for this family: it confirms a starved
    job_shop run reports no fabricated bound, matching Decision #5's intent
    even though the specific guard branch added to this family's scripts
    stays untested here.

    This is also why the sibling flexible_job_shop test's
    `payload["solution"] == {}` check (added alongside this docstring) has no
    equivalent here: that assertion parses the SCRIPT's own stdout JSON, which
    requires the script to have printed it. Here the child is killed before
    `model.py` ever reaches its `print(json.dumps(payload))` call -- there is
    no script-produced envelope to parse -- so asserting anything about a
    `solution` key would be checking a value that was never produced, not
    behavior this refactor added."""
    mcp = create_mcp_server("full")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "args": ["data_ft20.json"],
            "script_timeout_ms": 300,
        },
    )
    assert isinstance(call_result, CallToolResult)
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content

    assert result["status"] == "timeout"
    assert result["objective"] is None
    assert result["best_objective_bound"] is None
