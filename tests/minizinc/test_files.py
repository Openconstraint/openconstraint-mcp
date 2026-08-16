from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.core import (
    FINDMUS_SOLVER,
    MiniZincExecutionError,
    check_model_path,
    find_unsat_core_path,
    inspect_model_path,
    solve_model_path,
)
from openconstraint_mcp.minizinc.files import validate_checker_path, validate_model_data_paths
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas.minizinc import (
    CheckResult,
    ModelInspectionResult,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
    UnsatCoreResult,
)
from openconstraint_mcp.shared.childrun import ChildExecutionResult
from tests.minizinc.helpers import (
    checker_pass,
    checker_violation,
    child_result,
    solution_obj,
    stream,
)


def _patch_capabilities(
    monkeypatch: pytest.MonkeyPatch, caps_by_id: dict[str, SolverCapabilities]
) -> None:
    """Point the capability resolver's ``list_solvers`` at ``caps_by_id``."""
    solvers = [
        SolverInfo(id=solver_id, name=solver_id, capabilities=caps)
        for solver_id, caps in caps_by_id.items()
    ]
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.list_solvers", lambda: SolverList(solvers=solvers)
    )


# A minimal single-line interface object for `_MODEL_SRC` (no params, one output
# var, satisfy) as the managed binary would emit under `--model-interface-only`.
_INSPECT_STDOUT = (
    '{"type": "interface", "input": {}, "output": {"x": {"type": "int"}}, '
    '"method": "sat", "has_output_item": false, "included_files": [], "globals": []}'
)

_MODEL_SRC = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"

# A minimal `--json-stream` satisfy transcript: a single solution object and no
# status object (a single `satisfy` stops at the first solution, emitting no
# completeness verdict), so the parser resolves status to "satisfied".
_SOLVE_STREAM_SAMPLE = (
    json.dumps({"type": "solution", "output": {"default": "x = 3;\n", "json": {"x": 3}}}) + "\n"
)

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


def _record_run(
    monkeypatch: pytest.MonkeyPatch, completed: ChildExecutionResult
) -> list[dict[str, Any]]:
    """Patch ``minizinc.core.execute_child`` to record the model/data args and cwd.

    File tools run the real on-disk model, so the recorded model/data args are
    the caller's resolved positional paths. A checker path may also end in
    ``.mzn`` but lives in the flags slot, before the model/data positionals. The
    ``kwargs['cwd']`` key mirrors the old popen-record shape so cwd assertions read
    unchanged; the executor receives the run directory as its second positional.
    """
    calls: list[dict[str, Any]] = []

    def _fake_run(
        argv: list[str],
        cwd: Any,
        *,
        timeout_ms: int,
        tracker: Any = None,
        on_start: Any = None,
    ) -> ChildExecutionResult:
        cmd = list(argv)
        data_arg = cmd[-1] if str(cmd[-1]).endswith(".dzn") else None
        model_arg = cmd[-2] if data_arg is not None else cmd[-1]
        calls.append(
            {
                "cmd": cmd,
                "kwargs": {"cwd": str(cwd)},
                "cwd": str(cwd),
                "timeout_ms": timeout_ms,
                "tracker": tracker,
                "on_start": on_start,
                "model_arg": str(model_arg),
                "data_arg": str(data_arg) if data_arg is not None else None,
            }
        )
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    return calls


def _fail_if_run_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail)


# --- CLI-style execution (real path, cwd = model's parent) -----------------


def test_solve_runs_real_path_from_parent(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(
        monkeypatch, child_result(stdout=_SOLVE_STREAM_SAMPLE, stderr="", returncode=0)
    )

    result = solve_model_path(model_path)

    assert isinstance(result, SolveResult)
    # A single `satisfy` emits no status object; the parser's return-code
    # fallback resolves a clean exit with a solution to "satisfied".
    assert result.status == "satisfied"
    assert result.checker is None
    # The path solve runner requests statistics too, symmetric with the inline
    # solve_model (check/findMUS path runs assert its absence below).
    assert "--statistics" in calls[0]["cmd"]
    assert "--solution-checker" not in calls[0]["cmd"]
    # The managed binary ran on the resolved REAL path with cwd = its parent,
    # so a relative include resolves against the model's own directory.
    assert calls[0]["model_arg"] == str(model_path.resolve())
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_relative_input_is_resolved_without_double_counting(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "model.mzn").write_text(_MODEL_SRC)
    monkeypatch.chdir(tmp_path)
    calls = _record_run(
        monkeypatch, child_result(stdout=_SOLVE_STREAM_SAMPLE, stderr="", returncode=0)
    )

    # Relative input: cwd=parent + relative argv would make the subprocess look
    # under sub/sub/. Resolving up front pins both to the absolute path/parent.
    solve_model_path(Path("sub/model.mzn"))

    resolved = (tmp_path / "sub" / "model.mzn").resolve()
    assert calls[0]["model_arg"] == str(resolved)
    assert calls[0]["kwargs"]["cwd"] == str(resolved.parent)


def test_data_is_positional_after_model(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text("int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;\n")
    data_path = tmp_path / "params.dzn"
    data_path.write_text("n = 3;\n")
    calls = _record_run(
        monkeypatch, child_result(stdout=_SOLVE_STREAM_SAMPLE, stderr="", returncode=0)
    )

    solve_model_path(model_path, data_path=data_path)

    cmd = calls[0]["cmd"]
    assert cmd[-1] == str(data_path.resolve())
    assert cmd[-2] == str(model_path.resolve())


def test_check_passes_compile_flag_on_real_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    result = check_model_path(model_path)

    cmd = calls[0]["cmd"]
    assert isinstance(result, CheckResult)
    assert result.status == "ok"
    assert "-c" in cmd
    assert "--statistics" not in cmd
    assert cmd[-1] == str(model_path.resolve())
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_inspect_passes_interface_flag_on_real_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(monkeypatch, child_result(stdout=_INSPECT_STDOUT, stderr="", returncode=0))

    result = inspect_model_path(model_path)

    cmd = calls[0]["cmd"]
    assert isinstance(result, ModelInspectionResult)
    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.method == "sat"
    assert "--model-interface-only" in cmd
    # Read-only inspection: never the solve transport or any search control.
    assert "--json-stream" not in cmd
    assert "--statistics" not in cmd
    assert not ({"-a", "-f", "-p", "-r", "-n"} & set(cmd))
    # Runs the real path from the model's own parent dir (CLI-style include resolution).
    assert cmd[-1] == str(model_path.resolve())
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_inspect_data_is_positional_after_model(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text("int: n;\nvar 1..n: x;\nsolve satisfy;\n")
    data_path = tmp_path / "data.dzn"
    data_path.write_text("n = 3;\n")
    empty_interface = (
        '{"type": "interface", "input": {}, "output": {"x": {"type": "int"}}, '
        '"method": "sat", "has_output_item": false}'
    )
    calls = _record_run(monkeypatch, child_result(stdout=empty_interface, stderr="", returncode=0))

    result = inspect_model_path(model_path, data_path=data_path)

    cmd = calls[0]["cmd"]
    assert "--model-interface-only" in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert cmd[-1] == str(data_path.resolve())
    # Data supplied -> the interface reports nothing still required (completeness).
    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters == {}


def test_find_unsat_core_filters_by_real_basename(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "nurses.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)
    # Traces reference the real basename plus a differently-named included file.
    stdout = (
        "MUS: 1 2\nTraces: nurses.mzn|4|12|4|20|;nurses.mzn|5|12|5|20|;helpers.mzn|10|1|10|5|\n"
    )
    calls = _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = find_unsat_core_path(model_path)

    assert isinstance(result, UnsatCoreResult)
    assert result.status == "mus_found"
    # Entry-file spans resolve from the real model text; helpers.mzn is excluded.
    assert len(result.core) == 2
    assert "x + y > 5" in result.core[0].source
    assert "x + y < 3" in result.core[1].source
    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("--solver") + 1] == FINDMUS_SOLVER
    assert "-c" not in cmd
    assert "--statistics" not in cmd
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_find_unsat_core_duplicate_basename_is_known_limitation(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Entry model at a/model.mzn; a *different* file b/model.mzn shares the
    # basename. The basename-only filter cannot tell them apart, so a trace
    # span that actually came from b/model.mzn is (mis-)attributed to the entry
    # model's core. This documents the best-effort limitation — raw stdout
    # stays authoritative.
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    model_path = tmp_path / "a" / "model.mzn"
    model_path.write_text(_UNSAT_CORE_MODEL)
    (tmp_path / "b" / "model.mzn").write_text(_UNSAT_CORE_MODEL)
    stdout = "MUS: 1\nTraces: model.mzn|4|12|4|20|\n"
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = find_unsat_core_path(model_path)

    assert result.status == "mus_found"
    # The collision: the span is attributed to core despite the ambiguity.
    assert len(result.core) == 1
    assert "x + y > 5" in result.core[0].source


def test_compile_error_returns_structured_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text('include "missing.mzn";\nvar 1..5: x;\nsolve satisfy;\n')
    diagnostic = "Error: cannot open included file 'missing.mzn'\n"
    _record_run(monkeypatch, child_result(stdout="", stderr=diagnostic, returncode=1))

    result = solve_model_path(model_path)

    # A nonzero-rc compile failure is returned as a structured result, not raised.
    assert result.status == "error"
    assert "cannot open included file" in result.stderr


# --- num_solutions gate wires through the path call site -------------------


def test_solve_model_path_num_solutions_adds_valued_n_flag(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    calls = _record_run(
        monkeypatch, child_result(stdout=_SOLVE_STREAM_SAMPLE, stderr="", returncode=0)
    )

    solve_model_path(model_path, num_solutions=2, solver="org.chuffed.chuffed")

    cmd = calls[0]["cmd"]
    assert cmd[cmd.index("-n") + 1] == "2"


def test_solve_model_path_num_solutions_rejected_for_default_solver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Path validation runs before the gate, so an EXISTING model file is required
    # to reach the gate (a missing path would raise the path error first).
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    _fail_if_run_called(monkeypatch)

    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model_path(model_path, num_solutions=2)
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


def test_solve_model_path_rejects_non_positive_num_solutions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    _fail_if_run_called(monkeypatch)

    with pytest.raises(ValueError, match="num_solutions"):
        solve_model_path(model_path, num_solutions=0, solver="org.chuffed.chuffed")


def test_solve_model_path_rejects_unsupported_control_before_solve(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The path solve inherits the inline capability gate: a control the resolved
    # solver omits is rejected before the solve runs (D4 case a).
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _fail_if_run_called(monkeypatch)

    with pytest.raises(ValueError, match="free_search") as exc_info:
        solve_model_path(model_path, free_search=True)
    message = str(exc_info.value)
    assert "cp-sat" in message
    assert "-f" in message


# --- validate_model_data_paths --------------------------------------------


def test_validate_rejects_missing_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_model_data_paths(tmp_path / "nope.mzn", None)


def test_validate_rejects_directory_model(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a regular file"):
        validate_model_data_paths(tmp_path, None)


def test_validate_rejects_missing_data(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    with pytest.raises(ValueError, match="does not exist"):
        validate_model_data_paths(model_path, tmp_path / "missing.dzn")


@pytest.mark.parametrize("body", ["", "   \n\t\n"])
def test_validate_rejects_empty_model(tmp_path: Path, body: str) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(body)
    with pytest.raises(ValueError, match="empty"):
        validate_model_data_paths(model_path, None)


def test_validate_returns_resolved_absolute_paths(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    data_path = tmp_path / "d.dzn"
    data_path.write_text("n = 1;\n")

    resolved_model, resolved_data = validate_model_data_paths(model_path, data_path)

    assert resolved_model == model_path.resolve()
    assert resolved_model.is_absolute()
    assert resolved_data == data_path.resolve()
    assert resolved_data is not None and resolved_data.is_absolute()


def test_validate_allows_empty_data(tmp_path: Path) -> None:
    model_path = tmp_path / "m.mzn"
    model_path.write_text(_MODEL_SRC)
    data_path = tmp_path / "d.dzn"
    data_path.write_text("")

    _, resolved_data = validate_model_data_paths(model_path, data_path)

    assert resolved_data == data_path.resolve()


# --- every public function validates before any run ------------------------

_PATH_FUNCS: list[Callable[..., Any]] = [
    solve_model_path,
    check_model_path,
    find_unsat_core_path,
    inspect_model_path,
]


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_missing_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="does not exist"):
        func(tmp_path / "nope.mzn")


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_empty_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    model_path = tmp_path / "empty.mzn"
    model_path.write_text("   \n")
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        func(model_path)


@pytest.mark.parametrize("func", _PATH_FUNCS, ids=lambda f: f.__name__)
def test_every_path_func_rejects_non_utf8_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    func: Callable[..., Any],
) -> None:
    model_path = tmp_path / "bad.mzn"
    model_path.write_bytes(b"\xff\xfe garbage")
    _fail_if_run_called(monkeypatch)
    with pytest.raises(ValueError, match="UTF-8") as exc_info:
        func(model_path)
    assert str(model_path.resolve()) in str(exc_info.value)


# --- runtime / timeout / exec guards ---------------------------------------


def test_runtime_missing_raises(tmp_path: Path, fake_runtime_dir: Path) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    with pytest.raises(RuntimeMissingError) as exc_info:
        solve_model_path(model_path)
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


@pytest.mark.parametrize("bad_timeout", [0, -1])
def test_non_positive_timeout_raises(
    tmp_path: Path,
    fake_runtime_dir: Path,
    bad_timeout: int,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    with pytest.raises(ValueError, match="positive"):
        solve_model_path(model_path, timeout_ms=bad_timeout)


def test_oserror_wraps_as_execution_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "entry.mzn"
    model_path.write_text(_MODEL_SRC)

    def _fake_run(*args: Any, **kwargs: Any) -> Any:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        solve_model_path(model_path)
    assert "install-runtime" in str(exc_info.value)


# --- solve_model_path(checker_path=...) (--solution-checker) ----------------
#
# Captured production-transport (`--json-stream --solution-checker`) shapes: the
# checker object is emitted immediately BEFORE the solution it validated, so the
# per-checker entries stay positionally aligned with the solve parser's solutions.

_CHECKER_SRC = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)


def _write_model_and_checker(tmp_path: Path) -> tuple[Path, Path]:
    model_path = tmp_path / "model.mzn"
    model_path.write_text("var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;\n")
    checker_path = tmp_path / "model.mzc.mzn"
    checker_path.write_text(_CHECKER_SRC)
    return model_path, checker_path


def test_solve_model_path_with_checker_adds_solution_checker_flag_on_real_path(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    calls = _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    solve_model_path(model_path, checker_path=checker_path)

    cmd = calls[0]["cmd"]
    # The checker flag carries the RESOLVED ABSOLUTE checker path...
    checker_arg = cmd[cmd.index("--solution-checker") + 1]
    assert checker_arg == str(checker_path.resolve())
    assert Path(checker_arg).is_absolute()
    # ...layered on top of the full solve transport (not a bare invocation)...
    for transport_arg in ("--statistics", "--json-stream", "--output-objective"):
        assert transport_arg in cmd
    # ...and the run happens from the model's own directory.
    assert calls[0]["kwargs"]["cwd"] == str(model_path.resolve().parent)


def test_solve_model_path_with_checker_completed_when_no_violation(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert isinstance(result, SolveResult)
    assert result.checker is not None
    assert result.checker.status == "completed"
    # One checker verdict per produced solution: the alignment invariant.
    assert len(result.checker.checks) == len(result.solutions) == 1
    assert result.checker.checks[0].violation is False
    assert result.checker.checks[0].output == "CORRECT\n"


def test_solve_model_path_with_checker_incorrect_text_is_not_a_violation(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The author's "INCORRECT" text is a convention the server must NOT adjudicate:
    # it is surfaced verbatim but checker status stays `completed` (only a nested
    # UNSATISFIABLE flips to `violation`).
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_pass("INCORRECT\n"),
        solution_obj("x=3 y=1\n", {"x": 3, "y": 1}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "completed"
    assert result.checker.checks[0].violation is False
    assert result.checker.checks[0].output == "INCORRECT\n"


def test_solve_model_path_with_checker_violation_keeps_rejected_solution(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A constraint-style rejection flips checker status to `violation`, but the
    # rejected solution stays in `solutions` (fact 5) — clients must consult
    # the per-solution `checks`, not assume every produced solution is valid.
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_violation(),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "violation"
    assert result.checker.checks[0].violation is True
    assert result.solutions == [{"x": 1, "y": 2}]


def test_solve_model_path_with_checker_missing_checker_verdict_is_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A produced solution with NO checker object: the verdict count (0) misaligns
    # with the solution count (1), so the aggregate is `error`, not a misleading
    # `completed` over an unchecked solution.
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_path_with_checker_stream_error_with_zero_rc_is_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stream ERROR verdict maps to `error` independently of the return code, and
    # the derivation order puts `error` ahead of `no_solution`/`completed` so a
    # solver error is never hidden behind "no solution produced".
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream({"type": "status", "status": "ERROR"})
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.status == "error"
    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_path_with_checker_nonzero_rc_is_error(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A nonzero return code (a broken/missing checker) is `error` even when the
    # solve stream itself reported a solution and the counts align — the rc branch
    # of the derivation in isolation.
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="boom", returncode=1))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_path_with_checker_no_solution_status(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A clean unsatisfiable run produces no solution, so no checker ran and the
    # verdict count matches (both zero): `no_solution`, with the detail in
    # `solve.status`.
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream({"type": "status", "status": "UNSATISFIABLE"})
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.status == "unsatisfiable"
    assert result.checker is not None
    assert result.checker.status == "no_solution"


def test_solve_model_path_with_checker_timeout_status(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path, checker_path = _write_model_and_checker(tmp_path)

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout="", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.status == "timeout"
    assert result.timed_out is True


def test_solve_model_path_with_checker_transcript_is_raw_transcript(
    tmp_path: Path,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `checker.transcript` is the AUTHORITATIVE record: the raw transcript verbatim,
    # checker objects intact. `solve.stdout` is the reconstructed solution text
    # only — the solve parser drops checker objects — which is why the transcript
    # is preserved separately.
    model_path, checker_path = _write_model_and_checker(tmp_path)
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_run(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model_path(model_path, checker_path=checker_path)

    assert result.checker is not None
    assert result.checker.transcript == stdout
    assert "checker" in result.checker.transcript
    assert "checker" not in result.stdout


# --- validate_checker_path -------------------------------------------------


def test_validate_checker_returns_resolved_absolute_path(tmp_path: Path) -> None:
    checker_path = tmp_path / "c.mzc.mzn"
    checker_path.write_text(_CHECKER_SRC)

    resolved = validate_checker_path(checker_path)

    assert resolved == checker_path.resolve()
    assert resolved.is_absolute()


def test_validate_checker_accepts_bare_mzc_suffix(tmp_path: Path) -> None:
    checker_path = tmp_path / "c.mzc"
    checker_path.write_text(_CHECKER_SRC)

    assert validate_checker_path(checker_path) == checker_path.resolve()


@pytest.mark.parametrize("name", ["checker.mzn", "checker.mzc.txt"])
def test_validate_checker_rejects_bad_suffix(tmp_path: Path, name: str) -> None:
    # `.mzn` (the model suffix) and a `.mzc.txt` decoy both fail: MiniZinc rejects
    # any checker not ending in `.mzc`/`.mzc.mzn` at argument parsing, and the check
    # is on `name`, not `Path.suffix` (which returns `.mzn` for `c.mzc.mzn`).
    checker_path = tmp_path / name
    checker_path.write_text(_CHECKER_SRC)
    with pytest.raises(ValueError, match="mzc"):
        validate_checker_path(checker_path)


def test_validate_checker_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_checker_path(tmp_path / "missing.mzc.mzn")


def test_validate_checker_rejects_directory(tmp_path: Path) -> None:
    checker_dir = tmp_path / "dir.mzc.mzn"
    checker_dir.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        validate_checker_path(checker_dir)


def test_validate_checker_rejects_non_utf8(tmp_path: Path) -> None:
    checker_path = tmp_path / "bad.mzc.mzn"
    checker_path.write_bytes(b"\xff\xfe garbage")
    with pytest.raises(ValueError, match="UTF-8"):
        validate_checker_path(checker_path)


def test_solve_model_path_rejects_bad_checker_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model_path = tmp_path / "model.mzn"
    model_path.write_text(_MODEL_SRC)
    bad_checker = tmp_path / "checker.mzn"  # wrong suffix
    bad_checker.write_text(_CHECKER_SRC)
    _fail_if_run_called(monkeypatch)

    with pytest.raises(ValueError, match="mzc"):
        solve_model_path(model_path, checker_path=bad_checker)


def test_solve_model_path_with_checker_rejects_missing_model_before_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker_path = tmp_path / "model.mzc.mzn"
    checker_path.write_text(_CHECKER_SRC)
    _fail_if_run_called(monkeypatch)

    with pytest.raises(ValueError, match="does not exist"):
        solve_model_path(tmp_path / "nope.mzn", checker_path=checker_path)
