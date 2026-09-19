"""Real-subprocess smoke test for the assembly line balancing example workflow.

The real MCP tool spawns the real script on the Scholl benchmark instance,
parses its stdout, builds the checker payload from it, and spawns the real
checker -- the seam between the script's printed `solution` and checker.py's
expectations of it, which the checker's unit tests supply both sides of and
therefore cannot check.

Marked ``integration``: it spawns real children (excluded from ``just check``,
run with ``just pytest -m integration -k assembly_line_balancing -v``). It needs
no managed MiniZinc runtime -- the CP-SAT path runs on ``sys.executable``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_types import CallToolResult

from openconstraint_mcp.server import create_mcp_server

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "assembly_line_balancing"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_jackson_reaches_the_published_optimum_with_an_accepted_verdict() -> None:
    mcp = create_mcp_server("full")
    problem_text = (_EXAMPLE_DIR / "parsed" / "JACKSON_c10.json").read_text(encoding="utf-8")
    published_optimum: int = json.loads(problem_text)["published_optimum"]["value"]

    call_result = await mcp.call_tool(
        "run_cpsat_python_file_checked",
        {
            "script_path": str(_EXAMPLE_DIR / "model.py"),
            "checker_path": str(_EXAMPLE_DIR / "checker.py"),
            "args": ["JACKSON_c10.json"],
            "problem": problem_text,
            "script_timeout_ms": 20_000,
        },
    )
    assert isinstance(call_result, CallToolResult)
    assert call_result.structured_content is not None
    result: dict[str, Any] = call_result.structured_content
    checker: dict[str, Any] = result["checker"]

    assert (result["status"], result["objective"], checker["status"]) == (
        "optimal",
        published_optimum,
        "accepted",
    ), checker["errors"]
