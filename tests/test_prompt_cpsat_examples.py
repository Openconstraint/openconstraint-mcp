"""The cpsat_python_solution_workflow prompt's code examples must be copyable as-is.

These tests validate the examples AFTER template rendering (when doubled
braces have become normal Python braces) and run them in-process against the
pinned OR-Tools dependency — no subprocess, no network, no managed runtime.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import textwrap
from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.protocol_text.prompts import SOLVE_CPSAT_PYTHON_PROMPT

_CONTRACT_KEYS = {"status", "objective", "solution", "best_objective_bound"}

_SOLVE_SIGNATURE = "def solve(instance: ProblemInstance) -> Solution:"

_REPLAY_ENV_VARS = ("OPENCONSTRAINT_MCP_CPSAT_SEED", "OPENCONSTRAINT_MCP_CPSAT_CONFIG")


@contextlib.contextmanager
def _isolated_replay_env() -> Generator[None]:
    # run_cpsat_python clears both replay-protocol env vars before executing a
    # script; mirror that here so an inherited OPENCONSTRAINT_MCP_CPSAT_SEED
    # (e.g. a non-integer value) or OPENCONSTRAINT_MCP_CPSAT_CONFIG (e.g. a
    # path to a missing file) cannot change what an example does.
    saved = {name: os.environ.pop(name, None) for name in _REPLAY_ENV_VARS}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value


def _rendered_code_fences() -> list[str]:
    rendered = SOLVE_CPSAT_PYTHON_PROMPT.format(problem="toy problem")
    fences: list[str] = []
    inside = False
    buf: list[str] = []
    for line in rendered.splitlines():
        # startswith, not ==, so an opening fence with a language tag
        # (```python) still toggles instead of silently joining the body.
        if line.strip().startswith("```"):
            if inside:
                fences.append(textwrap.dedent("\n".join(buf)))
                buf = []
            inside = not inside
        elif inside:
            buf.append(line)
    return fences


def _run_capturing_stdout(source: str) -> list[str]:
    out = io.StringIO()
    with _isolated_replay_env(), contextlib.redirect_stdout(out):
        # The example guards its main() call on __name__, which an exec
        # against bare globals resolves through builtins to "builtins" —
        # so without this injection nothing runs and stdout stays empty.
        exec(compile(source, "<prompt-example>", "exec"), {"__name__": "__main__"})
    return [line for line in out.getvalue().strip().splitlines() if line]


def _define_example(source: str) -> dict[str, Any]:
    """Exec a fence WITHOUT ``__name__``, so it defines names and runs no main()."""
    namespace: dict[str, Any] = {}
    exec(compile(source, "<prompt-example>", "exec"), namespace)
    return namespace


def _infeasible_record() -> tuple[dict[str, Any], Any]:
    """Drive the main example's solve() infeasible and return its Solution record.

    OR-Tools returns 0.0 (not an exception) from objective_value and
    best_objective_bound on an infeasible solve, so the example's status guards
    — not the properties themselves — are what keep fabricated values out of
    both the record and the emitted JSON.
    """
    namespace = _define_example(_rendered_code_fences()[0])
    instance = namespace["parse_input"](namespace["read_input"]())
    # Requiring every item exceeds the capacity, so the coverage constraint
    # makes the instance genuinely infeasible.
    infeasible = dataclasses.replace(instance, min_items=len(instance.items))
    with _isolated_replay_env():
        return namespace, namespace["solve"](infeasible)


def test_cpsat_prompt_code_fences_have_no_placeholders() -> None:
    fences = _rendered_code_fences()
    assert len(fences) == 2, "expected the main example and the callback variant"
    for fence in fences:
        assert "..." not in fence, "a copyable example must not contain placeholders"


def test_cpsat_prompt_main_example_runs_and_emits_contract_json() -> None:
    main = _rendered_code_fences()[0]

    payload = json.loads(_run_capturing_stdout(main)[-1])

    assert set(payload) == _CONTRACT_KEYS
    assert payload["status"] == "optimal"
    assert payload["objective"] == 15.0
    assert payload["solution"] == {"selected": ["radio", "lamp", "clock"]}


def test_cpsat_prompt_main_example_returns_a_normalized_record_when_infeasible() -> None:
    _, record = _infeasible_record()

    assert record.status == "infeasible"
    assert record.selected is None, "absence must be None, never an empty list"
    assert record.objective is None
    assert record.best_objective_bound is None


def test_cpsat_prompt_serializer_emits_no_fabricated_values_for_an_infeasible_record() -> None:
    namespace, record = _infeasible_record()

    payload = namespace["serialize_solution"](record)

    assert set(payload) == _CONTRACT_KEYS
    assert payload["status"] == "infeasible"
    assert payload["objective"] is None
    assert payload["solution"] == {}
    assert payload["best_objective_bound"] is None


def test_cpsat_prompt_main_example_reports_no_objective_for_a_feasibility_model() -> None:
    # Both value guards also test model.has_objective(), and the infeasible and
    # unknown paths reach None through the STATUS half alone. Neutralize the
    # objective — a same-line token swap that leaves the indentation intact —
    # and a genuinely solved run must still report no objective.
    main = _rendered_code_fences()[0]
    marker = "model.maximize(total_value)"
    assert marker in main
    namespace = _define_example(main.replace(marker, "pass"))

    with _isolated_replay_env():
        record = namespace["solve"](namespace["parse_input"](namespace["read_input"]()))

    assert record.selected, "a feasibility model still reports its decision values"
    assert record.objective is None
    assert record.best_objective_bound is None


def test_cpsat_prompt_main_example_keeps_a_distinct_unknown_bound_guard() -> None:
    # UNKNOWN is not reliably producible from a knapsack this small, so pin the
    # bound guard's breadth at the source: it must stay wider than has_solution.
    main = _rendered_code_fences()[0]

    assert "bound_states = (cp_model.OPTIMAL, cp_model.FEASIBLE, cp_model.UNKNOWN)" in main
    assert "if model.has_objective() and status_code in bound_states" in main


def test_cpsat_prompt_serializer_keeps_the_bound_for_an_unknown_without_an_incumbent() -> None:
    namespace = _define_example(_rendered_code_fences()[0])
    record = namespace["Solution"](status="unknown", best_objective_bound=29.0)

    payload = namespace["serialize_solution"](record)

    assert set(payload) == _CONTRACT_KEYS
    assert payload["status"] == "unknown"
    assert payload["objective"] is None
    assert payload["solution"] == {}
    assert payload["best_objective_bound"] == 29.0


def test_cpsat_prompt_serializer_emits_a_legitimate_empty_selection_as_an_answer() -> None:
    # None means "no incumbent"; an EMPTY list is a real answer a coverage-free
    # instance can prove optimal. A truthiness check in the serializer would
    # collapse the two and make a proven optimum look like no answer at all.
    namespace = _define_example(_rendered_code_fences()[0])
    record = namespace["Solution"](status="optimal", selected=[], objective=0.0)

    payload = namespace["serialize_solution"](record)

    assert payload["solution"] == {"selected": []}


def test_cpsat_prompt_main_example_reads_the_seed_from_the_environment() -> None:
    # The prompt's prose teaches the seed protocol; the example must DO it,
    # since save_verified_cpsat_python's replay works by setting this env var.
    # num_workers is keyed on the config-driven literal, scoped to this fence
    # alone: the same literal already exists in step 6's experiment guidance,
    # but that line sits outside the fence, so scoping it here still has bite.
    main = _rendered_code_fences()[0]

    assert 'os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42")' in main
    assert 'solver.parameters.num_workers = config.get("num_workers", 1)' in main


def test_cpsat_prompt_callback_example_repeats_the_main_examples_guards() -> None:
    # The callback fence replaces the WHOLE solve(), so it carries its own copy
    # of the status guards. Nothing else compares the two copies, so a drifted
    # one — say a callback variant that lost model.has_objective() — would ship
    # self-contradictory guidance with every test still green.
    main, callback = _rendered_code_fences()
    start = "status_map = {"
    end = "def serialize_solution("
    assert start in main
    assert start in callback
    assert end in main

    main_guards = main[main.index(start) : main.index(end)]
    callback_guards = callback[callback.index(start) :]

    assert main_guards.strip() == callback_guards.strip()


def _composed_callback_script() -> str:
    """Splice the callback fence's solve() into the main fence, as the prompt says to."""
    main, callback = _rendered_code_fences()
    assert main.count(_SOLVE_SIGNATURE) == 1
    assert callback.startswith(_SOLVE_SIGNATURE)
    return (
        main[: main.index(_SOLVE_SIGNATURE)]
        + callback.strip()
        + "\n\n\n"
        + main[main.index("def serialize_solution(") :]
    )


def test_cpsat_prompt_callback_example_replaces_the_whole_solve_function() -> None:
    # The prompt instructs replacing the whole solve() function with the
    # callback variant, so the two fences must compose into one runnable script
    # that emits intermediate JSON lines before the authoritative final line.
    lines = _run_capturing_stdout(_composed_callback_script())

    assert len(lines) >= 2, "callback should emit at least one intermediate line"
    intermediate = json.loads(lines[0])
    assert intermediate["status"] == "feasible"
    assert set(intermediate) == _CONTRACT_KEYS
    final = json.loads(lines[-1])
    assert final["status"] == "optimal"


def test_cpsat_prompt_callback_bounds_intermediate_bytes_but_keeps_the_final_result() -> None:
    source = _composed_callback_script().replace(
        "self._remaining_output_bytes = 512 * 1024",
        "self._remaining_output_bytes = 1",
    )

    lines = _run_capturing_stdout(source)

    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "optimal"


def test_cpsat_prompt_callback_example_still_streams_under_a_configured_search_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the callback used to be gated on the config's CP-SAT limit, so a
    # CONFIGURED run emitted nothing until solve() returned. A child tree-killed at
    # the executor's deadline before that left no JSON on stdout at all, and timeout
    # recovery reported no incumbent — discarding every solution CP-SAT had found.
    # A search limit bounds search alone and is never validated against the executor
    # deadline, which the script cannot see, so streaming must stay unconditional.
    # Every other example test runs with the config env var cleared, which is why
    # this path went uncovered.
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"solver_time_limit_seconds": 30}), encoding="utf-8")
    monkeypatch.setenv("OPENCONSTRAINT_MCP_CPSAT_CONFIG", str(config_path))

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        exec(  # noqa: S102 - the prompt's own example, executed to prove it works
            compile(_composed_callback_script(), "<prompt-example>", "exec"),
            {"__name__": "__main__"},
        )
    lines = [line for line in out.getvalue().strip().splitlines() if line]

    assert len(lines) >= 2, "a configured search limit must not suppress the stream"
