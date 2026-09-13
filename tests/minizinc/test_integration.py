"""Real-runtime checks for ``check_model`` and ``find_unsat_core``.

These exercise the actual managed MiniZinc binary, so they prove what the
mocked unit tests cannot: that a `-c` compile returns 0 / non-zero for
valid / invalid models, and that a clean compile leaves stdout empty (the
`.fzn` is written to a file, not streamed). They are marked ``integration``
and excluded from ``just check``; run them with ``just integration`` on a
machine where ``install-runtime`` has placed a runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openconstraint_mcp.minizinc.core import (
    _build_solve_result,
    _run_managed_minizinc,
    build_solve_extra_args,
    check_model,
    check_model_path,
    find_unsat_core,
    find_unsat_core_path,
    inspect_model,
    list_solvers,
    save_verified_model,
    solve_model,
    solve_model_path,
)
from openconstraint_mcp.schemas.minizinc import SolveControls, SolveResult

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_real_runtime")]

_UNSAT_CORE_MODEL = (
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "\n"
    "constraint x + y > 5;\n"
    "constraint x + y < 3;\n"
    "constraint x != y;\n"
    "\n"
    "solve satisfy;\n"
)

# Parameterized model: `n` is undeclared data, and it *bounds the domain* of
# `x` (`var 1..n`), so without the data file the model cannot even flatten. A
# clean run that prints `x=4` therefore proves the bundled binary honored the
# positional data file.
_PARAM_MODEL = 'int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;\noutput ["x=\\(x)\\n"];\n'

# Parameterized *unsat* model: the conflicting bounds live in the data
# (`lo > hi`), so findMUS can only reach `mus_found` if the data file was read.
# The conflict is phrased over `x + y` rather than direct bounds on a single
# variable: a direct `x >= lo`/`x <= hi` contradiction is caught during
# flattening and folded into findMUS's hard background ("Background is not
# satisfiable, exiting"), leaving no soft constraints to minimize. Phrasing it
# over a sum keeps both constraints soft so findMUS isolates them as a MUS.
_PARAM_UNSAT_MODEL = (
    "int: lo;\n"
    "int: hi;\n"
    "var 0..10: x;\n"
    "var 0..10: y;\n"
    "constraint x + y > lo;\n"
    "constraint x + y < hi;\n"
    "constraint x != y;\n"
    "solve satisfy;\n"
)


def test_valid_model_compiles_with_empty_stdout() -> None:
    result = check_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert result.status == "ok"
    # A clean `-c` compile writes the FlatZinc to a file, not stdout.
    assert result.stdout == ""


def test_invalid_model_reports_compile_error() -> None:
    result = check_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert result.stderr.strip()


def test_find_unsat_core_reports_conflicting_constraints() -> None:
    result = find_unsat_core(_UNSAT_CORE_MODEL)

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    assert result.status == "mus_found"
    assert any("x + y > 5" in source for source in normalized_sources)
    assert any("x + y < 3" in source for source in normalized_sources)
    assert all("x != y" not in source for source in normalized_sources)


def test_solve_model_honors_inline_data() -> None:
    result = solve_model(_PARAM_MODEL, data="n = 4;")

    # `n` was supplied only through the inline data file; the constraint forces
    # `x = n = 4`.
    assert result.status in {"satisfied", "optimal"}
    assert "x=4" in result.stdout


# Keys observed in `--statistics` output on the pinned runtime
# (MiniZinc 2.9.7 / cp-sat). The exact key set is solver- and version-defined,
# so accept any one of a small set rather than hardcode a single key a patch
# release might rename (compile-stat keys like `flatIntVars` also vary by run).
_ACCEPTED_STAT_KEYS = {"flatTime", "method", "paths", "solveTime", "failures"}


def test_solve_model_emits_statistics() -> None:
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    # Status is classified from the stream, not from stdout markers.
    assert result.status in {"satisfied", "optimal"}
    # `--statistics` makes the {"type":"statistics"} stream objects appear; they
    # parse into the structured view with a recognizable key.
    assert result.statistics
    assert _ACCEPTED_STAT_KEYS & result.statistics.keys()
    # The json-stream transport keeps raw stat lines out of the reconstructed
    # human stdout — statistics are sibling stream objects, not output text.
    assert "%%%mzn-stat:" not in result.stdout


def test_solve_model_optimization_returns_structured_solution() -> None:
    # Unique-optimum maximization: `x` is forced to 5, so both the objective and
    # the solution variables are deterministic and the stream reports
    # OPTIMAL_SOLUTION — the Phase-1 single-solution structured-solve smoke.
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve maximize x;")

    assert result.status == "optimal"
    assert result.objective == 5
    # The structured solution carries the model variable with _objective stripped.
    assert result.solution == {"x": 5}
    assert "_objective" not in result.solution
    # `solutions` preserves emission order and ends at the best (== `solution`).
    assert result.solutions
    assert result.solutions[-1] == result.solution
    # This model has no explicit `output` item, so the stream carries only the json
    # section. The human stdout must still be synthesized so the MCP-visible result
    # is never solution-less; the `_objective` artifact stays out of it.
    assert "x = 5" in result.stdout
    assert "_objective" not in result.stdout


def test_solve_model_satisfaction_returns_solution() -> None:
    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    # A single `satisfy` emits a solution but no status object → satisfied, and a
    # satisfaction model carries no objective.
    assert result.status == "satisfied"
    assert result.objective is None
    assert result.solution is not None
    assert result.solution["x"] > 2
    assert result.solutions and result.solutions[-1] == result.solution
    # No explicit `output` item → json-only solution object; the synthesized human
    # stdout still names the variable, so the solution does not vanish from the
    # model-visible text.
    assert f"x = {result.solution['x']}" in result.stdout


# --- Phase 2: solver/search-control flags ----------------------------------

# `x < y` over 1..3 has exactly three solutions: (1,2), (1,3), (2,3).
_ALL_SOLUTIONS_MODEL = "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;\n"


def test_solve_model_all_solutions_enumerates_multiple() -> None:
    result = solve_model(_ALL_SOLUTIONS_MODEL, controls=SolveControls(all_solutions=True))

    # `-a` enumerates every solution and the stream ends in ALL_SOLUTIONS, which
    # maps to `satisfied`; `solution` is the last enumerated entry of `solutions`.
    assert result.status == "satisfied"
    assert len(result.solutions) >= 2
    assert result.solution == result.solutions[-1]
    assert result.objective is None


def test_solve_model_output_cap_truncates_large_enumeration() -> None:
    # A high-cardinality `-a` enumeration on Gecode emits well past the 1 MiB output
    # cap long before the 60 s timeout, so the executor tree-kills it and the runner
    # reports a truncated-but-usable result. This proves the managed binary's stream
    # is really capped and classified — the plumbing unit tests cannot show that.
    result = solve_model(
        "var 1..100000: x;\nsolve satisfy;",
        solver="org.gecode.gecode",
        controls=SolveControls(all_solutions=True),
        timeout_ms=60_000,
    )

    assert result.truncated is True
    assert result.timed_out is False  # the cap fired, not the wall-clock deadline
    assert result.status == "satisfied"  # partial enumeration, never the rc-error fallback
    assert result.solutions  # partial solutions survive the kill
    assert result.return_code is None  # the kill is ours, not the model's exit
    assert result.diagnostic is not None
    assert result.diagnostic.category == "output_truncated"
    # The response stays bounded: stdout and stderr are each ≤ the 1 MiB cap, so a
    # worst-case serialized SolveResult is a few MiB, not unbounded.
    assert len(result.model_dump_json()) <= 8 * 1024 * 1024


def test_solve_model_random_seed_runs_cleanly() -> None:
    # `random_seed` is accepted and the solve completes; the seed effect is
    # solver-internal, so assert only a clean satisfied result.
    result = solve_model(
        "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", controls=SolveControls(random_seed=12345)
    )

    assert result.status == "satisfied"
    assert result.solution is not None


def test_solve_model_negative_random_seed_surfaces_as_error() -> None:
    # cp-sat's seed must be a non-negative int32; a negative seed is rejected at
    # runtime as a `{"type":"status","status":"ERROR"}` verdict. Our parser must
    # map that to `status="error"` (not the silent "unknown" fallback), so a bad
    # parameter is visibly an error rather than an empty no-solution result.
    result = solve_model(
        "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", controls=SolveControls(random_seed=-5)
    )

    assert result.status == "error"
    assert result.solution is None


def test_solve_model_parallel_runs_cleanly() -> None:
    # `parallel=2` requests two search threads; assert it solves, not a specific
    # threading effect (solver-dependent).
    result = solve_model(
        "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", controls=SolveControls(parallel=2)
    )

    assert result.status == "satisfied"
    assert result.solution is not None


def test_solve_model_free_search_runs_cleanly() -> None:
    # `free_search=True` lets the solver use its own search; assert only that the
    # default managed solver accepts the flag and solves.
    result = solve_model(
        "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;", controls=SolveControls(free_search=True)
    )

    assert result.status == "satisfied"
    assert result.solution is not None


# --- num_solutions (solver-gated, satisfaction-only) -----------------------
#
# These prove what mocked-argv unit tests cannot: the managed binary actually
# accepts `-n N` for the gated solvers and caps the count (the exact "a string
# was built != the binary accepts it" trap that produced the original `-n` bug).


def _skip_if_solver_absent(solver: str) -> None:
    """Skip when ``solver`` is not in the managed runtime.

    These are the first integration tests to need a non-default solver; a
    user-pointed runtime (`configure-runtime` / `OPENCONSTRAINT_MCP_RUNTIME_DIR`)
    may lack gecode/chuffed, so resolve the gate against what `list_solvers`
    actually reports rather than assuming the bundled set.
    """
    available = {s.id for s in list_solvers().solvers}
    if solver not in available:
        pytest.skip(f"{solver} not in managed runtime")


@pytest.mark.parametrize("solver", ["org.gecode.gecode", "org.chuffed.chuffed"])
def test_solve_model_num_solutions_caps_satisfaction_count(solver: str) -> None:
    # `_ALL_SOLUTIONS_MODEL` has three solutions; `-n 2` must cap the enumeration
    # at exactly two for a solver whose stdFlags include `-n`.
    _skip_if_solver_absent(solver)

    result = solve_model(
        _ALL_SOLUTIONS_MODEL, solver=solver, controls=SolveControls(num_solutions=2)
    )

    assert result.status == "satisfied"
    assert len(result.solutions) == 2


def test_solve_model_num_solutions_rejected_for_cp_sat() -> None:
    # cp-sat is the pinned default and does NOT support `-n`: the gate raises a
    # ValueError before any subprocess, so the doomed command is never built and
    # the solver never fakes a success. Unconditional — cp-sat is always present.
    with pytest.raises(ValueError, match="num_solutions"):
        solve_model(_ALL_SOLUTIONS_MODEL, solver="cp-sat", controls=SolveControls(num_solutions=2))


@pytest.mark.parametrize("solver", ["org.gecode.gecode", "org.chuffed.chuffed"])
def test_list_solvers_reports_num_solutions_capability_for_supported_solvers(
    solver: str,
) -> None:
    # Validates the parsing assumption against the real 2.9.7 config: the bundled
    # gecode/chuffed entries actually carry stdFlags, and both are allowlisted, so
    # the conservative gate reports supports_num_solutions True for them.
    _skip_if_solver_absent(solver)

    by_id = {s.id: s for s in list_solvers().solvers}
    caps = by_id[solver].capabilities

    assert caps.std_flags
    assert caps.supports_num_solutions is True


def test_list_solvers_reports_cpsat_without_num_solutions_capability() -> None:
    # The conservative gate holds against the real config: cp-sat declares the
    # standard flags but is not allowlisted, so supports_num_solutions stays False.
    # The managed 2.9.7 runtime reports the OR-Tools solver under the canonical id
    # `cp-sat` (not `com.google.or-tools.cpsat`), matching the `solver="cp-sat"`
    # the solve-path gate rejects in test_solve_model_num_solutions_rejected_for_cp_sat.
    _skip_if_solver_absent("cp-sat")

    by_id = {s.id: s for s in list_solvers().solvers}
    caps = by_id["cp-sat"].capabilities

    assert caps.supports_num_solutions is False


# --- capability enforcement (-a/-f/-p/-r, runtime-local) -------------------
#
# These prove what mocked-capability unit tests cannot: the gate runs against the
# REAL --solvers-json output, so a control the bundled solver genuinely omits is
# rejected before the solve, and one it genuinely declares still solves.


def test_solve_model_rejects_control_the_solver_does_not_declare() -> None:
    # The managed chuffed declares no `-p` (verified against the 2.9.7 config), so
    # a parallel request is rejected before any solve. Defensive skip if a
    # user-pointed runtime's chuffed does declare it.
    _skip_if_solver_absent("org.chuffed.chuffed")
    caps = {s.id: s for s in list_solvers().solvers}["org.chuffed.chuffed"].capabilities
    if caps.supports_parallel:
        pytest.skip("this runtime's chuffed declares -p; candidate no longer applies")

    with pytest.raises(ValueError, match="parallel") as exc_info:
        solve_model(
            _ALL_SOLUTIONS_MODEL, solver="org.chuffed.chuffed", controls=SolveControls(parallel=2)
        )
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "-p" in message


def test_solve_model_accepts_control_the_solver_declares() -> None:
    # The inverse of the rejection: chuffed declares `-f`, so a free_search request
    # passes the gate and the real solve runs — guarding against over-rejection.
    _skip_if_solver_absent("org.chuffed.chuffed")
    caps = {s.id: s for s in list_solvers().solvers}["org.chuffed.chuffed"].capabilities
    if not caps.supports_free_search:
        pytest.skip("this runtime's chuffed does not declare -f")

    result = solve_model(
        _ALL_SOLUTIONS_MODEL, solver="org.chuffed.chuffed", controls=SolveControls(free_search=True)
    )

    assert result.status == "satisfied"


def test_check_model_honors_inline_data() -> None:
    # Without data, `var 1..n` has an unbound domain and the model cannot
    # flatten — a clean `ok` proves the data file was read.
    result = check_model(_PARAM_MODEL, data="n = 4;")

    assert result.status == "ok"


def test_find_unsat_core_honors_inline_data() -> None:
    result = find_unsat_core(_PARAM_UNSAT_MODEL, data="lo = 5;\nhi = 3;")

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    assert result.status == "mus_found"
    assert any("x + y > lo" in source for source in normalized_sources)
    assert any("x + y < hi" in source for source in normalized_sources)


# --- path-based file tools: CLI-style include behavior ---------------------

# A stdlib include (`globals.mzn`) is resolved from the solver's library path,
# never the model's directory.
_STDLIB_INCLUDE_MODEL = (
    'include "globals.mzn";\n'
    "array[1..3] of var 1..3: x;\n"
    "constraint alldifferent(x);\n"
    "solve satisfy;\n"
)


def test_stdlib_include_compiles(tmp_path: Path) -> None:
    model_path = tmp_path / "stdlib.mzn"
    model_path.write_text(_STDLIB_INCLUDE_MODEL)

    result = check_model_path(model_path)

    # A global-constraint model compiles via the stdlib include.
    assert result.status == "ok"


def test_check_model_path_leaves_no_intermediate_artifacts(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text("var 1..3: x;\nconstraint x > 1;\nsolve satisfy;\n")

    result = check_model_path(model_path)

    # The compile-only (`-c`) run must not leave its FlatZinc/output-model next to
    # the user's model: those artifacts are redirected to a private temp dir.
    assert result.status == "ok"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.suffix in {".fzn", ".ozn"})
    assert leftovers == []


def test_check_model_path_error_leaves_no_intermediate_artifacts(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    # Errors during flattening (here a div-by-zero in a par declaration) can emit a
    # partial FlatZinc before the run aborts — the artifact redirect must hold on the
    # error path too, not just the happy path.
    model_path.write_text("int: x = 1 div 0;\nconstraint x > 0;\nsolve satisfy;\n")

    result = check_model_path(model_path)

    assert result.status == "error"
    leftovers = sorted(p.name for p in tmp_path.iterdir() if p.suffix in {".fzn", ".ozn"})
    assert leftovers == []


def test_relative_local_include_resolves(tmp_path: Path) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "helpers.mzn").write_text("int: helper_bound = 5;\n")
    entry = proj / "entry.mzn"
    entry.write_text(
        'include "helpers.mzn";\nvar 1..helper_bound: x;\nconstraint x > 2;\nsolve satisfy;\n'
    )

    result = solve_model_path(entry)

    # File tools run from the model's own directory, so a relative sibling
    # include resolves like the MiniZinc CLI — the core of the file-tool design.
    assert result.status in {"ok", "satisfied", "optimal"}


def test_find_unsat_core_path_resolves_entry_basename(tmp_path: Path) -> None:
    model_path = tmp_path / "conflict.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)

    result = find_unsat_core_path(model_path)

    normalized_sources = [" ".join(item.source.split()) for item in result.core]
    # The real model basename (`conflict.mzn`) must match findMUS's trace token
    # for the structured core to resolve the entry-file spans.
    assert result.status == "mus_found"
    assert any("x + y > 5" in source for source in normalized_sources)
    assert any("x + y < 3" in source for source in normalized_sources)
    assert all("x != y" not in source for source in normalized_sources)


# --- inspect_model (--model-interface-only) --------------------------------
#
# Mandatory real-binary coverage for the new CLI flag: a mocked-argv unit test
# proves the flag string was built, NOT that the managed binary accepts it (the
# `num_solutions`/`-n` lesson). These exercise the actual runtime.

# Required params (n, capacity) plus two int *arrays* and a var-bool array; the
# `maximize` objective fixes `method`. `take` is the decision variable.
_KNAPSACK_MODEL = (
    "int: n;\n"
    "int: capacity;\n"
    "array[1..n] of int: weight;\n"
    "array[1..n] of int: profit;\n"
    "array[1..n] of var bool: take;\n"
    "constraint sum(i in 1..n)(weight[i] * take[i]) <= capacity;\n"
    "solve maximize sum(i in 1..n)(profit[i] * take[i]);\n"
)
_KNAPSACK_DATA = "n = 3;\ncapacity = 10;\nweight = [2, 3, 4];\nprofit = [5, 6, 7];\n"


def test_inspect_model_reports_interface_without_data() -> None:
    result = inspect_model(_KNAPSACK_MODEL)

    assert result.status == "ok"
    assert result.interface is not None
    # The objective kind comes straight from the model-interface output.
    assert result.interface.method == "max"
    # With no data, every unassigned parameter is still required.
    assert set(result.interface.required_parameters) == {"n", "capacity", "weight", "profit"}
    # The int arrays carry dim 1; the scalars carry dim 0.
    assert result.interface.required_parameters["weight"].dim == 1
    assert result.interface.required_parameters["n"].dim == 0


def test_inspect_model_with_data_reports_complete() -> None:
    # The SAME model with matching data: the interface must report nothing still
    # required, proving data threading and the completeness mode end to end.
    result = inspect_model(_KNAPSACK_MODEL, data=_KNAPSACK_DATA)

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters == {}


def test_inspect_model_reports_satisfy_method() -> None:
    result = inspect_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.method == "sat"


def test_inspect_model_type_error_reports_error() -> None:
    result = inspect_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert result.interface is None
    assert result.stderr.strip()


def test_inspect_model_reports_tuple_and_record_base_types() -> None:
    # Real-binary proof that `tuple`/`record` are in the parsed vocabulary: a
    # mocked unit test only proves the Literal accepts the strings, not that the
    # managed 2.9.7 binary actually emits them (the num_solutions/-n lesson). A
    # tuple parameter and a record output var are both valid models the tool must
    # report as ok, not degrade to error.
    result = inspect_model(
        "tuple(int, float): pt;\nvar record(int: a, bool: b): r;\nsolve satisfy;"
    )

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters["pt"].base_type == "tuple"
    assert result.interface.output_variables["r"].base_type == "record"


def test_inspect_model_reports_optional_parameter() -> None:
    # Real-binary proof that an `opt` parameter surfaces as is_optional rather than
    # being silently indistinguishable from a required one.
    result = inspect_model("opt int: g;\nvar 1..3: x;\nconstraint x > 1;\nsolve satisfy;")

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters["g"].base_type == "int"
    assert result.interface.required_parameters["g"].is_optional is True


def test_inspect_model_reports_ann_base_type() -> None:
    # Real-binary proof that `ann` (the annotation type) is in the parsed
    # vocabulary. An `array of ann` strategy list feeding `seq_search` is a valid
    # model the tool must report as ok, not degrade to error (the original `ann`
    # gap). A mocked unit test only proves the Literal accepts the string, NOT that
    # the managed 2.9.7 binary emits `{"type": "ann", "dim": 1}` for it.
    result = inspect_model(
        "array[1..2] of ann: strategies;\nsolve :: seq_search(strategies) satisfy;"
    )

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters["strategies"].base_type == "ann"
    assert result.interface.required_parameters["strategies"].dim == 1


def test_inspect_model_reports_globals_from_real_binary() -> None:
    # Real-binary proof that the `globals` passthrough is populated by the managed
    # binary, not merely parseable from hand-written stdout (the same standard the
    # type fields are held to). An `alldifferent` model must surface
    # globals == ["alldifferent"]; the binary pads the JSON array with cosmetic
    # whitespace (`[    "alldifferent"]`), which json.loads absorbs.
    result = inspect_model(
        'include "globals.mzn";\n'
        "int: n;\n"
        "array[1..n] of var 1..n: q;\n"
        "constraint alldifferent(q);\n"
        "solve satisfy;\n"
    )

    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.globals == ["alldifferent"]


# --- solve_model_path(checker_path=...) (--solution-checker) ---------------
#
# Mandatory real-binary coverage: a built `--solution-checker` flag string is NOT
# proof the managed binary accepts it and emits the checker stream (the
# num_solutions/-n lesson). These exercise the actual runtime end to end.

# A fully-pinned solution (x=1, y=2) so the checker verdict is deterministic.
_PINNED_MODEL = (
    "var 1..3: x;\n"
    "var 1..3: y;\n"
    "constraint x = 1;\n"
    "constraint y = 2;\n"
    "constraint x < y;\n"
    "solve satisfy;\n"
    'output ["x=\\(x) y=\\(y)\\n"];\n'
)
# Output-style checker: re-declares the model's output vars as par and emits the
# author verdict text. Explicit par logic (a bare global would resolve to its var
# overload and fail to compile as a checker).
_OUTPUT_CHECKER = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)
# Constraint-style checker: validation as a hard constraint — the only idiom that
# yields a machine-readable failure. `x != 1` rejects the pinned x=1 solution.
_VIOLATION_CHECKER = 'int: x;\nint: y;\nconstraint x != 1;\noutput ["checked\\n"];\n'
# Broken checker: references an undeclared identifier, so it fails to compile
# (rc != 0) rather than producing a verdict.
_BROKEN_CHECKER = 'int: x;\nint: y;\nconstraint x = zzz;\noutput ["bad\\n"];\n'
# Evaluation-error checker: compiles with an unassigned `x` parameter, then
# fails only when MiniZinc evaluates the checker against the pinned solution.
_EVALUATION_ERROR_CHECKER = (
    'int: x;\narray[1..1] of int: witness = [10];\noutput ["value=\\(witness[x])\\n"];\n'
)


def _write_pair(tmp_path: Path, model_src: str, checker_src: str) -> tuple[Path, Path]:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(model_src)
    checker_path = tmp_path / "model.mzc.mzn"
    checker_path.write_text(checker_src)
    return model_path, checker_path


def test_solve_model_path_with_checker_valid_solution_completes(tmp_path: Path) -> None:
    model_path, checker_path = _write_pair(tmp_path, _PINNED_MODEL, _OUTPUT_CHECKER)

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.status in {"satisfied", "optimal"}
    assert result.checker is not None
    assert result.checker.status == "completed"
    assert len(result.checker.checks) == 1
    assert result.checker.checks[0].violation is False
    # The author verdict text must be surfaced from the real transcript, not lost
    # to an empty string — proving the verdict is recovered from whichever shape
    # (top-level or nested-solution `output.default`) the binary actually emits.
    assert "CORRECT" in result.checker.checks[0].output


def test_solve_model_path_with_checker_constraint_violation_keeps_solution(tmp_path: Path) -> None:
    model_path, checker_path = _write_pair(tmp_path, _PINNED_MODEL, _VIOLATION_CHECKER)

    result = solve_model_path(model_path, checker_path=checker_path)

    # The constraint-style checker rejects the pinned x=1 solution...
    assert result.checker is not None
    assert result.checker.status == "violation"
    assert result.checker.checks[0].violation is True
    # ...but the rejected solution is still emitted in solutions (fact 5).
    assert result.solutions


def test_solve_model_path_with_checker_broken_checker_is_error(tmp_path: Path) -> None:
    model_path, checker_path = _write_pair(tmp_path, _PINNED_MODEL, _BROKEN_CHECKER)

    result = solve_model_path(model_path, checker_path=checker_path)

    # A checker that fails to compile gives a nonzero return code, not a verdict.
    assert result.checker is not None
    assert result.checker.status == "error"
    assert result.return_code not in (0, None)


def test_solve_model_path_with_checker_evaluation_error_is_error(tmp_path: Path) -> None:
    model_path, checker_path = _write_pair(
        tmp_path,
        "var 1..3: x;\nconstraint x = 2;\nsolve satisfy;\n",
        _EVALUATION_ERROR_CHECKER,
    )

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "error"
    assert result.status == "error"
    # MiniZinc emits checker evaluation errors as top-level error objects, so
    # the solve stream verdict drives the checker aggregate.
    objects = [json.loads(line) for line in result.checker.transcript.splitlines()]
    assert any(obj.get("type") == "error" for obj in objects)


# Unique-optimum maximization (x forced to 5): the default optimization stream
# emits ONE final checker/solution pair, not an improving sequence — so a verdict
# is NOT a proof of optimality (solve.status carries that, separately).
_OPT_MODEL = 'var 1..5: x;\nconstraint x > 2;\nsolve maximize x;\noutput ["x=\\(x)\\n"];\n'
_OPT_CHECKER = 'int: x;\noutput [ if x >= 3 then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'


def test_solve_model_path_with_checker_optimization_completes_with_single_check(
    tmp_path: Path,
) -> None:
    model_path, checker_path = _write_pair(tmp_path, _OPT_MODEL, _OPT_CHECKER)

    result = solve_model_path(model_path, checker_path=checker_path)

    # The checker re-emits a verdict; optimality is proved only by solve.status.
    assert result.status == "optimal"
    assert result.checker is not None
    assert result.checker.status == "completed"
    assert len(result.checker.checks) == 1


def test_solve_model_path_with_checker_composes_with_all_solutions(tmp_path: Path) -> None:
    model_path, checker_path = _write_pair(tmp_path, _ALL_SOLUTIONS_MODEL, _OUTPUT_CHECKER)

    result = solve_model_path(
        model_path, checker_path=checker_path, controls=SolveControls(all_solutions=True)
    )

    assert result.status == "satisfied"
    assert len(result.solutions) >= 2
    assert result.checker is not None
    assert result.checker.status == "completed"
    assert len(result.checker.checks) == len(result.solutions)
    assert all(check.violation is False for check in result.checker.checks)


# --- save_verified_model (verified save end to end) -------------------------


def test_save_verified_model_writes_project_end_to_end(tmp_path: Path) -> None:
    # The save gate composes two real managed runs (compile check, then solve)
    # with the staged commit; per the project rule, gate behavior built from
    # real solver runs gets at least one real-binary check beyond mocked argv.
    target = tmp_path / "saved-project"

    result = save_verified_model(
        _PARAM_MODEL,
        target_dir=target,
        data="n = 4;",
        problem="Force x to equal n.",
    )

    assert result.status == "saved"
    assert result.check.status == "ok"
    assert result.solve is not None
    assert result.solve.status in {"satisfied", "optimal"}
    assert (target / "model.mzn").read_text() == _PARAM_MODEL
    assert (target / "data.dzn").read_text() == "n = 4;"
    assert (target / "problem.md").read_text() == "Force x to equal n."
    solve_payload = json.loads((target / "solve-result.json").read_text())
    assert "x=4" in solve_payload["stdout"]
    manifest = json.loads((target / ".openconstraint-model.json").read_text())
    assert manifest["managed_by"] == "openconstraint-mcp"
    assert manifest["verification"]["check_status"] == "ok"


# --- Raw --json-stream status spellings --------------------------------------
#
# `solve_model()` returns normalized statuses only, so these probes drop one
# level down: run the production solve transport (`build_solve_extra_args` +
# `_run_managed_minizinc`), read the raw `{"type": "status"}` objects straight
# off the stream, and build the normalized result from the *same* execution via
# `_build_solve_result`. Each probe asserts the exact raw spelling from
# `_STATUS_MAP` together with its normalized mapping; a missing or unexpected
# status object is a failure, never a skip, so a spelling regression in a
# future runtime bump cannot pass silently.
#
# Three statuses the pinned runtime (MiniZinc 2.9.7; cp-sat/gecode/chuffed)
# cannot produce deterministically are intentionally unprobed. Observed during
# implementation-time probing:
#
# - SATISFIED: never emitted on any attempted path. An incomplete-but-fruitful
#   run ends with *no status object at all*, and the normalized `satisfied`
#   comes from the no-status fallback in `_resolve_status`. Attempted: plain
#   satisfy; gecode holding an incumbent at a 1 s limit on a number-partition
#   minimize; cp-sat Golomb-ruler m=11 and hard partitions at 100 ms–3 s limits
#   (each either closes to OPTIMAL_SOLUTION or finds nothing → UNKNOWN, and
#   without `-a` the cp-sat driver prints no intermediate incumbents); `-a` on
#   Golomb at 2 s on cp-sat and gecode (incumbents printed, still no status
#   object); gecode/chuffed `-n 2` on a 3-solution satisfy. Its `_STATUS_MAP`
#   entry stays as defensive coverage — it maps to the verdict the fallback
#   already produces.
# - UNBOUNDED / UNSAT_OR_UNBOUNDED: unreachable. `var int: x; solve maximize x;`
#   returns OPTIMAL_SOLUTION on all three solvers because the flattener imposes
#   default finite int bounds, and UNSAT_OR_UNBOUNDED is a MIP-relaxation
#   verdict with no MIP solver bundled.
#
# Re-open on a runtime/solver bump that starts emitting SATISFIED at limits, or
# on bundling a solver with a genuine unbounded verdict — then add the probe.


def _probe_raw_statuses(
    model: str,
    *,
    solver: str = "cp-sat",
    timeout_ms: int = 60_000,
    all_solutions: bool = False,
    random_seed: int | None = None,
) -> tuple[list[str], SolveResult]:
    """Solve ``model`` once; return its raw stream statuses and normalized result."""
    extra_args = build_solve_extra_args(
        solver, SolveControls(random_seed=random_seed, all_solutions=all_solutions)
    )
    outcome = _run_managed_minizinc(
        model, solver=solver, timeout_ms=timeout_ms, extra_args=extra_args
    )
    raw_statuses: list[str] = []
    for raw_line in outcome.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if isinstance(obj, dict) and obj.get("type") == "status":
            raw_statuses.append(obj["status"])
    return raw_statuses, _build_solve_result(outcome, solver=solver)


def test_raw_status_spelling_optimal_solution() -> None:
    raw, result = _probe_raw_statuses("var 1..5: x;\nconstraint x > 2;\nsolve maximize x;\n")

    assert raw == ["OPTIMAL_SOLUTION"]
    assert result.status == "optimal"


def test_raw_status_spelling_all_solutions() -> None:
    raw, result = _probe_raw_statuses(_ALL_SOLUTIONS_MODEL, all_solutions=True)

    assert raw == ["ALL_SOLUTIONS"]
    assert result.status == "satisfied"


def test_raw_status_spelling_unsatisfiable() -> None:
    raw, result = _probe_raw_statuses("var 1..3: x;\nconstraint x > 5;\nsolve satisfy;\n")

    assert raw == ["UNSATISFIABLE"]
    assert result.status == "unsatisfiable"


def test_raw_status_spelling_error() -> None:
    # cp-sat rejects a negative seed at runtime. The runtime reports this as a
    # real `{"type": "status", "status": "ERROR"}` verdict (with rc 1 and an
    # `Illegal value ... fz_seed` line on stderr), not as a standalone
    # `{"type": "error"}` diagnostic object — proving the `ERROR` entry in
    # `_STATUS_MAP` is exercised by the status channel itself.
    raw, result = _probe_raw_statuses(
        "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n", random_seed=-5
    )

    assert raw == ["ERROR"]
    assert result.status == "error"


# Pigeonhole (n items into n-1 slots) with the alldifferent *decomposed* into
# binary disequalities: gecode's plain != propagators cannot detect the Hall-set
# conflict, so proving UNSATISFIABLE needs an exponential search (~13! nodes at
# n=14) that no realistic machine finishes in 500 ms — while the instance being
# unsatisfiable means no solution can ever appear either. Both escape hatches
# are closed, so the run deterministically ends with the solver giving up:
# a raw UNKNOWN status. (The global `alldifferent` would presolve this away
# instantly — cp-sat proves it UNSATISFIABLE — which is why the decomposition
# and solver choice both matter here.)
_PIGEONHOLE_MODEL = (
    "int: n = 14;\n"
    "array[1..n] of var 1..n-1: p;\n"
    "constraint forall(i, j in 1..n where i < j)(p[i] != p[j]);\n"
    "solve satisfy;\n"
)


def test_raw_status_spelling_unknown() -> None:
    _skip_if_solver_absent("org.gecode.gecode")
    raw, result = _probe_raw_statuses(_PIGEONHOLE_MODEL, solver="org.gecode.gecode", timeout_ms=500)

    assert raw == ["UNKNOWN"]
    assert result.status == "unknown"
