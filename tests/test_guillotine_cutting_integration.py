"""Real-subprocess smoke test for the guillotine cutting example workflow.

The real MCP tool spawns the real script, parses its stdout, builds the checker
payload from it, and spawns the real checker -- the seam between a script's
printed `solution` and checker.py's expectations of it, which the checker's
unit tests (tests/test_guillotine_cutting_checker.py) supply both sides of and
therefore cannot check. It is parametrized over model.py and the
shelf_packing.py comparison script, because each owns its own copy of the
output code.

Marked ``integration``: it spawns real children (excluded from ``just check``,
run with ``just pytest -m integration -k guillotine -v``). It needs no managed
MiniZinc runtime -- the CP-SAT path runs on ``sys.executable``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult

from openconstraint_mcp.server import create_mcp_server

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "guillotine_cutting"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script_name", "expected_status", "expected_objective"),
    [
        # model.py proves the optimum in well under a second single-worker.
        ("model.py", "optimal", 110),
        # The shelf rule's pattern, documented in problem.txt.
        ("shelf_packing.py", "feasible", 81),
    ],
)
async def test_polarizing_film_script_and_checker_reach_an_accepted_verdict(
    script_name: str, expected_status: str, expected_objective: int
) -> None:
    mcp = create_mcp_server("full")
    problem_text = (_EXAMPLE_DIR / "parsed" / "polarizing_film.json").read_text(encoding="utf-8")

    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(_EXAMPLE_DIR / script_name),
            "checker_path": str(_EXAMPLE_DIR / "checker.py"),
            "args": ["polarizing_film.json"],
            "problem": problem_text,
            "script_timeout_ms": 20_000,
        },
    )
    assert isinstance(call_result, CallToolResult)
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    checker: dict[str, Any] = result["checker"]

    assert (result["status"], result["objective"], checker["status"]) == (
        expected_status,
        expected_objective,
        "accepted",
    ), checker["errors"]
