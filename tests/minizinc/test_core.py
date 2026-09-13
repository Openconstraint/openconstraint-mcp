from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.minizinc.artifacts import (
    CHECKER_FILENAME,
    DATA_FILENAME,
    MANIFEST_FILENAME,
    MODEL_FILENAME,
    PROBLEM_FILENAME,
    SOLVE_RESULT_FILENAME,
)
from openconstraint_mcp.minizinc.core import (
    _SOLVE_STREAM_ARGS,
    DEFAULT_CHECK_TIMEOUT_MS,
    DEFAULT_INSPECT_TIMEOUT_MS,
    DEFAULT_SOLVE_TIMEOUT_MS,
    DEFAULT_SOLVER,
    DEFAULT_UNSAT_CORE_TIMEOUT_MS,
    FINDMUS_SOLVER,
    MiniZincExecutionError,
    _build_solve_result,
    _run_managed_minizinc,
    _RunOutcome,
    _solver_capabilities,
    build_solve_extra_args,
    check_model,
    find_unsat_core,
    inspect_model,
    list_solvers,
    prepare_solve_args,
    run_prepared_solve,
    save_verified_model,
    solve_model,
    solver_supports_num_solutions,
)
from openconstraint_mcp.runtime import RuntimeMissingError
from openconstraint_mcp.schemas.diagnostics import UnsupportedFeatureError
from openconstraint_mcp.schemas.minizinc import (
    CheckResult,
    ModelInspectionResult,
    SolveControls,
    SolverCapabilities,
    SolveResult,
    SolverInfo,
    SolverList,
)
from openconstraint_mcp.schemas.portfolio import (
    PortfolioAttempt,
    PortfolioSolveControls,
    PortfolioSolveResult,
)
from openconstraint_mcp.shared.childrun import ChildExecutionResult
from openconstraint_mcp.shared.save_target import EXPERIMENT_LOG_FILENAME, text_sha256
from tests.minizinc.helpers import (
    STREAM_ERROR,
    STREAM_OPTIMAL,
    STREAM_SATISFY,
    STREAM_SATISFY_ALL,
    STREAM_UNSAT,
    UNSAT_CORE_MODEL,
    UNSAT_CORE_STDOUT,
    checker_pass,
    checker_violation,
    child_result,
    solution_obj,
    solution_obj_json_only,
    stream,
)


def test_list_solvers_raises_clear_error_when_runtime_missing(
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        list_solvers()
    message = str(exc_info.value)
    assert "install-runtime" in message
    assert "MiniZinc" in message


def test_list_solvers_parses_solvers_json(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.dumps(
        [
            {
                "id": "org.gecode.gecode",
                "name": "Gecode",
                "version": "6.3.0",
                "tags": ["cp", "int"],
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

    class _FakeCompleted:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.returncode = 0

    def _fake_run(*args: object, **kwargs: object) -> _FakeCompleted:
        return _FakeCompleted(payload)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    result = list_solvers()
    assert isinstance(result, SolverList)
    assert [solver.id for solver in result.solvers] == [
        "org.gecode.gecode",
        "com.google.or-tools.cpsat",
    ]
    assert result.solvers[0].tags == ["cp", "int"]
    assert result.solvers[1].version == "9.10"
    assert result.solvers[1].tags == []

    # Capabilities flow from each entry's stdFlags. gecode declares -n and is
    # allowlisted, so every control reads True; or-tools declares the standard
    # flags but no -n and is not allowlisted, so supports_num_solutions is False.
    gecode_caps = result.solvers[0].capabilities
    assert gecode_caps.supports_all_solutions is True
    assert gecode_caps.supports_free_search is True
    assert gecode_caps.supports_parallel is True
    assert gecode_caps.supports_random_seed is True
    assert gecode_caps.supports_num_solutions is True

    cpsat_caps = result.solvers[1].capabilities
    assert cpsat_caps.supports_all_solutions is True
    assert cpsat_caps.supports_free_search is True
    assert cpsat_caps.supports_parallel is True
    assert cpsat_caps.supports_random_seed is True
    assert cpsat_caps.supports_num_solutions is False
    assert "Detailed solver capabilities" in result.capability_note


def test_list_solvers_wraps_subprocess_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.CalledProcessError(
            returncode=1,
            cmd=[str(fake_minizinc_binary), "--solvers-json"],
            stderr="MiniZinc: invalid solver configuration\n",
        )

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        list_solvers()
    message = str(exc_info.value)
    assert "invalid solver configuration" in message
    assert "install-runtime" in message


def test_list_solvers_wraps_exec_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: object, **kwargs: object) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        list_solvers()
    assert "install-runtime" in str(exc_info.value)


def test_list_solvers_decodes_output_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []

    class _FakeCompleted:
        stdout = "[]"
        returncode = 0

    def _fake_run(*args: Any, **kwargs: Any) -> _FakeCompleted:
        calls.append({"kwargs": kwargs})
        return _FakeCompleted()

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.subprocess.run", _fake_run)

    list_solvers()

    assert calls[0]["kwargs"].get("encoding") == "utf-8"


def test_solver_capabilities_gecode_declares_all_controls() -> None:
    caps = _solver_capabilities(
        {"id": "org.gecode.gecode", "stdFlags": ["-a", "-f", "-n", "-p", "-r"]}
    )
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_parallel is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is True
    assert caps.std_flags == ["-a", "-f", "-n", "-p", "-r"]


def test_solver_capabilities_chuffed_lacks_parallel() -> None:
    # chuffed's stdFlags omit -p: supports_parallel must be a real read, not a
    # constant True. The other three derived booleans stay True.
    caps = _solver_capabilities({"id": "org.chuffed.chuffed", "stdFlags": ["-a", "-f", "-n", "-r"]})
    assert caps.supports_parallel is False
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is True


def test_solver_capabilities_cpsat_declares_flags_but_not_num_solutions() -> None:
    # Real cp-sat: declares -a/-f/-p/-r (and -i, which is untracked) but no -n,
    # and is not in the num_solutions allowlist — so supports_num_solutions False.
    caps = _solver_capabilities(
        {"id": "com.google.or-tools.cpsat", "stdFlags": ["-a", "-i", "-f", "-p", "-r"]}
    )
    assert caps.supports_all_solutions is True
    assert caps.supports_free_search is True
    assert caps.supports_parallel is True
    assert caps.supports_random_seed is True
    assert caps.supports_num_solutions is False
    assert caps.std_flags == ["-a", "-i", "-f", "-p", "-r"]


def test_solver_capabilities_gist_diverges_from_std_flags() -> None:
    # The load-bearing divergence: gist lists -n in stdFlags but is excluded from
    # the canonical gate, so supports_num_solutions is False even though
    # "-n" stays present in the verbatim std_flags.
    caps = _solver_capabilities(
        {"id": "org.gecode.gist", "stdFlags": ["-a", "-f", "-n", "-p", "-r"]}
    )
    assert caps.supports_num_solutions is False
    assert "-n" in caps.std_flags


def test_solver_capabilities_unknown_without_std_flags_is_default_deny() -> None:
    # No stdFlags key and a non-allowlisted id: empty std_flags zeroes the four
    # derived booleans, and the id is not allowlisted — two independent reasons.
    caps = _solver_capabilities({"id": "com.example.unknown", "name": "Unknown"})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is False


@pytest.mark.parametrize("bad_std_flags", [None, " -a"])
def test_solver_capabilities_malformed_std_flags_non_allowlisted(
    bad_std_flags: object,
) -> None:
    # A present null or a scalar string must degrade to an empty std_flags — never
    # crash on list(None) or split " -a" into ['  ', '-', 'a']. Non-allowlisted id
    # keeps supports_num_solutions False.
    caps = _solver_capabilities({"id": "com.example.unknown", "stdFlags": bad_std_flags})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is False


def test_solver_capabilities_malformed_std_flags_allowlisted_keeps_num_solutions() -> None:
    # The independence invariant: a malformed stdFlags zeroes the four derived
    # booleans, but supports_num_solutions follows the id allowlist and stays True
    # for gecode — never re-coupled to stdFlags.
    caps = _solver_capabilities({"id": "org.gecode.gecode", "stdFlags": None})
    assert caps.std_flags == []
    assert caps.supports_all_solutions is False
    assert caps.supports_free_search is False
    assert caps.supports_parallel is False
    assert caps.supports_random_seed is False
    assert caps.supports_num_solutions is True


def _record_subprocess(
    monkeypatch: pytest.MonkeyPatch, completed: ChildExecutionResult
) -> list[dict[str, Any]]:
    """Patch ``minizinc.core.execute_child`` to record the call and return ``completed``.

    Captures the argv, run directory, the executor timeout budget (``timeout_ms`` =
    the model's ``--time-limit`` plus the outer grace), the forwarded
    tracker/on_start, and the model/data/checker file contents read from the argv at
    call time (the runner deletes the temp dir on return, so post-call reads would
    race the cleanup). The ``args``/``kwargs`` keys mirror the old popen-record shape
    — ``args[0]`` is the argv, ``kwargs['cwd']`` the run directory — so the existing
    argv/cwd assertions read unchanged.
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
        data_path = Path(cmd[-1]) if str(cmd[-1]).endswith(".dzn") else None
        model_path = Path(cmd[-2]) if data_path is not None else Path(cmd[-1])
        checker_path: Path | None = None
        if "--solution-checker" in cmd:
            checker_path = Path(cmd[cmd.index("--solution-checker") + 1])
        data_contents: str | None = None
        if data_path is not None and data_path.is_file():
            data_contents = data_path.read_text()
        checker_contents: str | None = None
        if checker_path is not None and checker_path.is_file():
            checker_contents = checker_path.read_text()
        calls.append(
            {
                "args": (cmd,),
                "kwargs": {"cwd": str(cwd)},
                "argv": cmd,
                "cwd": str(cwd),
                "timeout_ms": timeout_ms,
                "tracker": tracker,
                "on_start": on_start,
                "model_path": str(model_path),
                "model_path_existed": model_path.is_file(),
                "model_contents": (model_path.read_text() if model_path.is_file() else None),
                "data_contents": data_contents,
                "checker_contents": checker_contents,
            }
        )
        return completed

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    return calls


class _SpyTracker:
    """Records register/unregister so the in-flight-child wiring can be asserted."""

    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []

    def register(self, proc: Any) -> None:
        self.events.append(("register", proc))

    def unregister(self, proc: Any) -> None:
        self.events.append(("unregister", proc))


def test_solve_model_forwards_tracker_to_executor(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The synchronous solve forwards its tracker to the shared executor, which owns
    # the register/unregister lifecycle (proven in tests/shared/test_childrun.py) so
    # the lifespan can terminate the live child on teardown.
    calls = _record_subprocess(monkeypatch, child_result(stdout=STREAM_SATISFY, returncode=0))
    spy = _SpyTracker()

    solve_model("solve satisfy;", tracker=spy)

    assert calls[0]["tracker"] is spy


def test_solve_model_forwards_tracker_to_executor_on_timeout(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even a timed-out solve forwards the tracker; the executor's unregister-on-every-
    # exit-path guarantee (test_childrun.py) keeps the live set clean.
    calls = _record_subprocess(monkeypatch, child_result(stdout="", timed_out=True))
    spy = _SpyTracker()

    result = solve_model("solve satisfy;", tracker=spy)

    assert result.timed_out is True
    assert calls[0]["tracker"] is spy


def test_solve_model_happy_path_returns_satisfied(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single `satisfy` solve emits a solution but no status object, so it is
    # classified `satisfied` from the clean exit + solution — not `optimal` (the
    # classic-`==========` misread this whole change exists to kill).
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert isinstance(result, SolveResult)
    assert result.status == "satisfied"
    assert result.solver == "cp-sat"
    assert result.return_code == 0
    assert result.timed_out is False
    assert result.solution == {"x": 1, "y": 2}
    assert result.solutions == [{"x": 1, "y": 2}]
    assert result.objective is None
    assert result.stdout == "x=1 y=2\n"  # reconstructed from the `default` section
    assert result.stderr == ""
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_solve_model_synthesizes_human_stdout_when_model_has_no_output_item(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A satisfy model with no explicit `output` item streams only the json section.
    # The structured solution must still be paired with a human stdout block, so
    # the solution does not vanish from the model-visible text.
    stdout = stream(solution_obj_json_only({"x": 3}))
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert result.status == "satisfied"
    assert result.solution == {"x": 3}
    assert result.solutions == [{"x": 3}]
    assert result.stdout == "x = 3;\n"


def test_solve_model_returns_structured_optimal_solution(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An optimization run: the driver's OPTIMAL_SOLUTION verdict maps to
    # `optimal`, the best objective and solution come from the last solution
    # object, and statistics are the merged, stringified stream values.
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_OPTIMAL, stderr="", returncode=0))

    result = solve_model("var 0..10: x;\nvar 0..10: y;\nsolve maximize x + 2 * y;")

    assert result.status == "optimal"
    assert result.solution == {"x": 2, "y": 10}
    assert result.solutions == [{"x": 2, "y": 10}]
    assert result.objective == 22
    assert result.stdout == "x=2 y=10 total=22\n"
    assert result.statistics["objective"] == "22"
    assert result.statistics["solveTime"] == "0.0005"


def test_solve_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model(model)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]

    assert cmd[0] == str(fake_minizinc_binary)
    solver_idx = cmd.index("--solver")
    assert cmd[solver_idx + 1] == "cp-sat"
    timeout_idx = cmd.index("--time-limit")
    assert cmd[timeout_idx + 1] == "30000"
    # Solve runs request statistics and the json-stream transport so the result
    # can surface structured solutions, a driver-authenticated status, and stats.
    assert "--statistics" in cmd
    assert "--json-stream" in cmd
    assert "--output-objective" in cmd
    assert cmd[cmd.index("--output-mode") + 1] == "json"
    assert "--solution-checker" not in cmd
    assert result.checker is None

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


_CHECKER_SRC = (
    'int: x;\nint: y;\noutput [ if x < y then "CORRECT\\n" else "INCORRECT\\n" endif ];\n'
)


def test_solve_model_with_checker_adds_solution_checker_flag(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    calls = _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model(
        "var 1..3: x;\nvar 1..3: y;\nconstraint x < y;\nsolve satisfy;", checker=_CHECKER_SRC
    )

    cmd = calls[0]["args"][0]
    checker_arg = cmd[cmd.index("--solution-checker") + 1]
    assert Path(checker_arg).name == "checker.mzc.mzn"
    assert calls[0]["checker_contents"] == _CHECKER_SRC
    assert cmd.index("--solution-checker") < cmd.index(calls[0]["model_path"])
    assert result.checker is not None
    assert result.checker.status == "completed"


def test_solve_model_with_checker_incorrect_text_is_not_a_violation(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("INCORRECT\n"),
        solution_obj("x=3 y=1\n", {"x": 3, "y": 1}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "completed"
    assert result.checker.checks[0].violation is False
    assert result.checker.checks[0].output == "INCORRECT\n"


def test_solve_model_with_checker_violation_keeps_rejected_solution(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_violation(),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "violation"
    assert result.checker.checks[0].violation is True
    assert result.solutions == [{"x": 1, "y": 2}]


def test_solve_model_with_checker_missing_verdict_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
        {"type": "status", "status": "SATISFIED"},
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_stream_error_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream({"type": "status", "status": "ERROR"})
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("solve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "error"
    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_top_level_error_and_nested_unknown_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        {
            "type": "error",
            "what": "result of evaluation is undefined",
            "message": "array access out of bounds",
        },
        {"type": "checker", "messages": [{"type": "status", "status": "UNKNOWN"}]},
        solution_obj_json_only({"x": 2}),
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nconstraint x = 2;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "error"
    assert result.return_code == 0
    assert result.checker is not None
    assert result.checker.status == "error"
    assert len(result.checker.checks) == 1
    assert result.checker.checks[0].violation is False
    assert result.solutions == [{"x": 2}]


def test_solve_model_with_checker_nonzero_rc_is_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="boom", returncode=1))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "error"


def test_solve_model_with_checker_no_solution_status(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream({"type": "status", "status": "UNSATISFIABLE"})
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("constraint false;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.status == "unsatisfiable"
    assert result.checker is not None
    assert result.checker.status == "no_solution"


def test_solve_model_with_checker_timeout_status(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout="", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = solve_model("solve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.status == "timeout"
    assert result.timed_out is True


def test_solve_model_with_checker_transcript_is_raw_stdout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=1 y=2\n", {"x": 1, "y": 2}),
    )
    _record_subprocess(monkeypatch, child_result(stdout=stdout, stderr="", returncode=0))

    result = solve_model("var 1..3: x;\nvar 1..3: y;\nsolve satisfy;", checker=_CHECKER_SRC)

    assert result.checker is not None
    assert result.checker.transcript == stdout
    assert "checker" in result.checker.transcript
    assert "checker" not in result.stdout


# --- Phase 2: solver/search-control flags ----------------------------------


def _solve_cmd_with_flags(
    monkeypatch: pytest.MonkeyPatch,
    *,
    solver: str = DEFAULT_SOLVER,
    checker: str | None = None,
    **flags: Any,
) -> list[str]:
    """Solve a trivial model with the given flags; return the argv it built.

    Reports every gated control as supported for the solve's solver — the unit
    under test here is argv assembly, not capability rejection — so a requested
    ``-a/-f/-p/-r`` resolves and appends its flag instead of raising.
    """
    full = SolverCapabilities(
        supports_all_solutions=True,
        supports_free_search=True,
        supports_parallel=True,
        supports_random_seed=True,
    )
    _patch_capabilities(monkeypatch, {DEFAULT_SOLVER: full, solver: full})
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )
    solve_model(
        "var 1..5: x;\nsolve satisfy;",
        solver=solver,
        checker=checker,
        controls=SolveControls(**flags),
    )
    return calls[0]["args"][0]


def test_solve_model_free_search_adds_f_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "-f" in _solve_cmd_with_flags(monkeypatch, free_search=True)


def test_solve_model_all_solutions_adds_a_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "-a" in _solve_cmd_with_flags(monkeypatch, all_solutions=True)


def test_solve_model_checker_composes_with_all_solutions(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(monkeypatch, checker=_CHECKER_SRC, all_solutions=True)
    assert "-a" in cmd
    assert "--solution-checker" in cmd


def test_solve_model_parallel_adds_valued_p_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(monkeypatch, parallel=4)
    assert cmd[cmd.index("-p") + 1] == "4"


@pytest.mark.parametrize("seed", [42, 0, -5])
def test_solve_model_random_seed_adds_valued_r_flag_for_any_int(
    seed: int, fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `random_seed` accepts any int (including 0 and negatives — no validation).
    cmd = _solve_cmd_with_flags(monkeypatch, random_seed=seed)
    assert cmd[cmd.index("-r") + 1] == str(seed)


def test_solve_model_default_flags_reproduce_phase1_argv(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no flags set, the solve argv carries only the json-stream transport
    # args — none of the search-control tokens — so a default solve is identical
    # to the Phase-1 invocation.
    cmd = _solve_cmd_with_flags(monkeypatch)
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))


def test_solve_model_combined_flags_all_appear_alongside_transport(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cmd = _solve_cmd_with_flags(
        monkeypatch,
        free_search=True,
        parallel=2,
        random_seed=7,
        all_solutions=True,
    )
    assert "-f" in cmd and "-a" in cmd
    assert cmd[cmd.index("-p") + 1] == "2"
    assert cmd[cmd.index("-r") + 1] == "7"
    # The transport args are untouched by the search-control flags.
    assert "--json-stream" in cmd and "--statistics" in cmd


@pytest.mark.parametrize("bad", [0, -1])
def test_solve_model_rejects_non_positive_parallel(
    bad: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad parallel")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    with pytest.raises(ValueError, match="parallel"):
        solve_model("solve satisfy;", controls=SolveControls(parallel=bad))


# --- num_solutions (solver-gated) ------------------------------------------


@pytest.mark.parametrize("solver", ["org.chuffed.chuffed", "org.gecode.gecode"])
def test_solve_model_num_solutions_adds_valued_n_flag_for_supported_solver(
    solver: str, fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A canonical -n-supporting solver gets `-n N` appended alongside the transport.
    cmd = _solve_cmd_with_flags(monkeypatch, num_solutions=2, solver=solver)
    assert cmd[cmd.index("-n") + 1] == "2"


def test_solve_model_num_solutions_rejects_short_alias(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The allowlist is canonical-ids-only: a short alias like "gecode" degrades to
    # the actionable error rather than a broken -n command (Decision 2).
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for an unsupported solver")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", controls=SolveControls(num_solutions=2), solver="gecode")
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


def test_solve_model_num_solutions_rejected_for_default_solver(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The default solver (cp-sat) does not support -n; the gate raises before any
    # subprocess so the doomed command is never built.
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", controls=SolveControls(num_solutions=2))
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


def test_solve_model_num_solutions_rejected_before_checker_run_for_default_solver(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for cp-sat + num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions") as exc_info:
        solve_model("solve satisfy;", checker=_CHECKER_SRC, controls=SolveControls(num_solutions=2))
    message = str(exc_info.value)
    assert "org.chuffed.chuffed" in message
    assert "org.gecode.gecode" in message


@pytest.mark.parametrize("bad", [0, -1])
def test_solve_model_rejects_non_positive_num_solutions(
    bad: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad num_solutions")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)
    with pytest.raises(ValueError, match="num_solutions"):
        solve_model(
            "solve satisfy;",
            controls=SolveControls(num_solutions=bad),
            solver="org.chuffed.chuffed",
        )


def test_solve_model_default_omits_num_solutions_flag(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With num_solutions unset, no -n token appears (Phase-1 argv preserved).
    assert "-n" not in _solve_cmd_with_flags(monkeypatch)


@pytest.mark.parametrize(
    ("solver", "expected"),
    [
        ("org.gecode.gecode", True),
        ("org.chuffed.chuffed", True),
        ("cp-sat", False),
        ("gecode", False),
        ("org.gecode.gist", False),
    ],
)
def test_solver_supports_num_solutions_truth_table(solver: str, expected: bool) -> None:
    assert solver_supports_num_solutions(solver) is expected


# --- capability enforcement (-a/-f/-p/-r, runtime-local) --------------------


def _patch_capabilities(
    monkeypatch: pytest.MonkeyPatch, caps_by_id: dict[str, SolverCapabilities]
) -> list[int]:
    """Patch ``core.list_solvers`` to report ``caps_by_id``; return a call counter.

    The resolver reads ``list_solvers()``; patching it here lets a test mock the
    runtime-local capability map without a real ``--solvers-json`` subprocess. The
    returned single-element list counts how many times the resolver invoked it, so
    a test can assert a default solve never resolves (count stays 0).
    """
    calls = [0]
    solvers = [
        SolverInfo(id=solver_id, name=solver_id, capabilities=caps)
        for solver_id, caps in caps_by_id.items()
    ]

    def _fake_list_solvers() -> SolverList:
        calls[0] += 1
        return SolverList(solvers=solvers)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.list_solvers", _fake_list_solvers)
    return calls


def _fail_if_solve_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("solve subprocess must not run when a control is unsupported")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail)


@pytest.mark.parametrize(
    ("control", "flag", "kwargs"),
    [
        ("all_solutions", "-a", {"all_solutions": True}),
        ("free_search", "-f", {"free_search": True}),
        ("parallel", "-p", {"parallel": 2}),
        ("random_seed", "-r", {"random_seed": 7}),
    ],
)
def test_solve_model_rejects_unsupported_control(
    control: str,
    flag: str,
    kwargs: dict[str, Any],
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The resolved default solver declares no stdFlags, so each gated control is
    # rejected before the solve runs (D4 case a), naming the solver, the control,
    # and its MiniZinc flag.
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _fail_if_solve_runs(monkeypatch)
    with pytest.raises(ValueError, match=control) as exc_info:
        solve_model("solve satisfy;", controls=SolveControls(**kwargs))
    message = str(exc_info.value)
    assert "cp-sat" in message
    assert flag in message


def test_solve_model_unresolved_solver_passes_capability_check(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A solver string that resolves to no entry id (a short alias) is NOT rejected
    # and gets no "missing capability" message — it passes through to the solve so
    # MiniZinc resolves the alias, exactly as before (D4 case c).
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0))
    result = solve_model(
        "solve satisfy;", solver="gecode", controls=SolveControls(free_search=True)
    )
    assert result.status == "satisfied"


def test_solve_model_default_controls_skip_capability_resolution(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no gated control requested, the lazy resolver is never invoked, so a
    # default solve pays no --solvers-json cost (D2).
    resolve_calls = _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0))
    result = solve_model("solve satisfy;")
    assert result.status == "satisfied"
    assert resolve_calls[0] == 0


def test_solve_model_supported_control_resolves_once_and_solves(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A declared control resolves the capability map exactly once (D2/D3) and the
    # solve proceeds (D4 case b).
    resolve_calls = _patch_capabilities(
        monkeypatch, {"cp-sat": SolverCapabilities(supports_free_search=True)}
    )
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0))
    result = solve_model("solve satisfy;", controls=SolveControls(free_search=True))
    assert result.status == "satisfied"
    assert resolve_calls[0] == 1


def test_prepare_solve_args_range_error_precedes_capability_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    with pytest.raises(ValueError, match="parallel must be >= 1"):
        prepare_solve_args("cp-sat", SolveControls(parallel=0))
    assert resolve_calls[0] == 0


def test_prepare_solve_args_default_controls_return_transport_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_calls = _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    assert prepare_solve_args("cp-sat", SolveControls()) == _SOLVE_STREAM_ARGS
    assert resolve_calls[0] == 0


def test_prepare_solve_args_rejects_unsupported_free_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    with pytest.raises(UnsupportedFeatureError, match="-f"):
        prepare_solve_args("cp-sat", SolveControls(free_search=True))


def test_solve_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model(model, data="n = 3;")

    cmd = calls[0]["args"][0]
    # Canonical MiniZinc order: model file, then data file as the last argument.
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["model_contents"] == model
    assert calls[0]["data_contents"] == "n = 3;"
    assert result.status == "satisfied"


def test_solve_model_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("var 1..5: x;\nsolve satisfy;")

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_solve_model_returns_structured_error_for_malformed_data(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostic = (
        "Error: syntax error, unexpected ';' in file '/tmp/openconstraint-mcp-xxxx/data.dzn'\n"
    )
    _record_subprocess(
        monkeypatch,
        child_result(stdout="", stderr=diagnostic, returncode=1),
    )

    result = solve_model("int: n;\nvar 1..n: x;\nsolve satisfy;", data="n = ;")

    assert result.status == "error"
    assert "data.dzn" in result.stderr
    assert "syntax error" in result.stderr


def test_solve_model_custom_solver_is_passed_through(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    result = solve_model("solve satisfy;", solver="gecode")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == "gecode"
    assert result.solver == "gecode"


def test_solve_model_custom_timeout_drives_outer_grace(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("solve satisfy;", timeout_ms=5000)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == "5000"
    # The executor gets the model's --time-limit (5000 ms) plus the 5 s outer grace,
    # so MiniZinc normally self-stops before the executor's wall-clock cap fires.
    assert calls[0]["timeout_ms"] == 10000


def test_solve_model_defaults_match_module_constants(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0)
    )

    solve_model("solve satisfy;")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == DEFAULT_SOLVER
    assert cmd[cmd.index("--time-limit") + 1] == str(DEFAULT_SOLVE_TIMEOUT_MS)


_EMPTY_OR_WHITESPACE_MODEL_CASES = [
    pytest.param(solve_model, "", id="solve_model-empty"),
    pytest.param(solve_model, "\n\n  \t\n", id="solve_model-whitespace"),
    pytest.param(check_model, "", id="check_model-empty"),
    pytest.param(check_model, "\n\n  \t\n", id="check_model-whitespace"),
    pytest.param(find_unsat_core, "", id="find_unsat_core-empty"),
    pytest.param(find_unsat_core, "\n  \t", id="find_unsat_core-whitespace"),
    pytest.param(inspect_model, "", id="inspect_model-empty"),
    pytest.param(inspect_model, "\n\n  \t\n", id="inspect_model-whitespace"),
]


@pytest.mark.parametrize(("entry_point", "bad_model"), _EMPTY_OR_WHITESPACE_MODEL_CASES)
def test_rejects_empty_or_whitespace_model(
    entry_point: Callable[..., Any],
    bad_model: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for empty model")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    with pytest.raises(ValueError, match="empty"):
        entry_point(bad_model)


_NON_POSITIVE_TIMEOUT_CASES = [
    pytest.param(solve_model, "solve satisfy;", 0, id="solve_model-zero"),
    pytest.param(solve_model, "solve satisfy;", -1, id="solve_model-negative"),
    pytest.param(check_model, "solve satisfy;", 0, id="check_model-zero"),
    pytest.param(check_model, "solve satisfy;", -1, id="check_model-negative"),
    pytest.param(find_unsat_core, UNSAT_CORE_MODEL, 0, id="find_unsat_core-zero"),
    pytest.param(find_unsat_core, UNSAT_CORE_MODEL, -1, id="find_unsat_core-negative"),
    pytest.param(inspect_model, "solve satisfy;", 0, id="inspect_model-zero"),
]


@pytest.mark.parametrize(("entry_point", "model", "bad_timeout"), _NON_POSITIVE_TIMEOUT_CASES)
def test_rejects_non_positive_timeout(
    entry_point: Callable[..., Any],
    model: str,
    bad_timeout: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for bad timeout")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail_if_called)

    with pytest.raises(ValueError, match="positive"):
        entry_point(model, timeout_ms=bad_timeout)


@pytest.mark.parametrize(
    "entry_point",
    [
        pytest.param(solve_model, id="solve_model"),
        pytest.param(check_model, id="check_model"),
    ],
)
def test_timeout_validation_precedes_runtime_check(
    entry_point: Callable[..., Any],
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(ValueError, match="positive"):
        entry_point("solve satisfy;", timeout_ms=0)


_MISSING_RUNTIME_CASES = [
    pytest.param(solve_model, "solve satisfy;", ("install-runtime", "MiniZinc"), id="solve_model"),
    pytest.param(check_model, "solve satisfy;", ("install-runtime", "MiniZinc"), id="check_model"),
    pytest.param(
        find_unsat_core,
        UNSAT_CORE_MODEL,
        ("install-runtime", "MiniZinc"),
        id="find_unsat_core",
    ),
    pytest.param(inspect_model, "solve satisfy;", ("install-runtime",), id="inspect_model"),
]


@pytest.mark.parametrize(("entry_point", "model", "expected_fragments"), _MISSING_RUNTIME_CASES)
def test_raises_when_runtime_missing(
    entry_point: Callable[..., Any],
    model: str,
    expected_fragments: tuple[str, ...],
    fake_runtime_dir: Path,
) -> None:
    with pytest.raises(RuntimeMissingError) as exc_info:
        entry_point(model)
    message = str(exc_info.value)
    for fragment in expected_fragments:
        assert fragment in message


def test_solve_model_returns_structured_result_for_minizinc_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(
            stdout="",
            stderr="MiniZinc: syntax error: unexpected token\n",
            returncode=1,
        ),
    )

    result = solve_model("solv satisfy;")

    assert result.status == "error"
    assert result.return_code == 1
    assert result.timed_out is False
    assert "syntax error" in result.stderr


def test_solve_model_returns_structured_result_for_unsatisfiable(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(stdout=STREAM_UNSAT, stderr="", returncode=0),
    )

    result = solve_model("constraint false;\nsolve satisfy;")

    assert result.status == "unsatisfiable"
    assert result.solution is None
    assert result.solutions == []
    assert result.objective is None
    # The driver routes its inconsistency warning into the stdout stream; it is
    # surfaced into the diagnostic stderr channel.
    assert "model inconsistency detected" in result.stderr


def test_solve_model_satisfy_all_returns_ordered_solutions(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A `satisfy` enumeration ends in ALL_SOLUTIONS, which maps to `satisfied`
    # (not `optimal`); `solutions` keeps emission order and `solution` is the
    # last found.
    _record_subprocess(
        monkeypatch, child_result(stdout=STREAM_SATISFY_ALL, stderr="", returncode=0)
    )

    result = solve_model("solve satisfy;")

    assert result.status == "satisfied"
    assert result.solutions == [{"x": 1, "y": 2}, {"x": 1, "y": 3}, {"x": 2, "y": 3}]
    assert result.solution == {"x": 2, "y": 3}
    assert result.objective is None
    assert result.stdout == "x=1 y=2\nx=1 y=3\nx=2 y=3\n"


def test_solve_model_surfaces_stream_error_into_stderr(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `--json-stream` reports a syntax error as an `{"type":"error"}` object on
    # stdout with an empty real stderr; the runner classifies `error` and lifts
    # the message into the diagnostic stderr channel so the client can repair.
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_ERROR, stderr="", returncode=1))

    result = solve_model("var 1..3: x\nsolve satisfy;")

    assert result.status == "error"
    assert result.solution is None
    assert "syntax error" in result.stderr
    assert "unexpected item" in result.stderr


def test_solve_model_timeout_keeps_partial_stream(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hard timeout surfaces a partial json-stream (a truncated trailing object);
    # the fully-received solution is kept and parsed, but the verdict is forced to
    # `timeout` with no real return code.
    partial = stream(solution_obj("x=3\n", {"x": 3})) + '{"type": "stat'  # truncated tail

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout=partial, stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = solve_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.return_code is None
    assert result.timed_out is True
    assert result.solution == {"x": 3}
    assert result.stdout == "x=3\n"  # reconstructed from the partial stream
    assert result.stderr == ""
    assert result.elapsed_ms >= 0


def test_solve_model_truncated_with_solutions_keeps_partial_and_nulls_return_code(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An output-cap tree-kill: partial parsed solutions are kept, the status is the
    # stream's verdict (here `satisfied`, never the rc-derived `error`), return_code
    # is null (the nonzero exit is ours), and the fixed cap-notice rides on stderr.
    _record_subprocess(
        monkeypatch,
        child_result(
            stdout=STREAM_SATISFY,
            stderr="",
            returncode=-15,
            truncated=True,
            truncation_killed=True,
        ),
    )

    result = solve_model("solve satisfy;")

    assert result.truncated is True
    assert result.status == "satisfied"
    assert result.solution == {"x": 1, "y": 2}
    assert result.return_code is None
    assert result.timed_out is False
    assert "output exceeded the 1 MiB cap; process stopped" in result.stderr


def test_solve_model_truncated_without_solutions_is_unknown() -> None:
    outcome = _RunOutcome(
        timed_out=False,
        returncode=-15,
        stdout="",
        stderr="",
        elapsed_ms=3,
        truncated=True,
        truncation_killed=True,
    )
    result = _build_solve_result(outcome, solver="cp-sat")

    assert result.truncated is True
    assert result.status == "unknown"  # never the rc-derived "error"
    assert result.return_code is None


def test_solve_model_truncated_clean_exit_keeps_real_nonzero_return_code() -> None:
    # A burst writer can overrun the cap and still exit ON ITS OWN with a genuine
    # failure code — the executor never killed it, so rc=1 is the model's own
    # verdict: it must stay visible and drive the rc-fallback "error" status
    # instead of being masked to unknown/None.
    outcome = _RunOutcome(
        timed_out=False, returncode=1, stdout="", stderr="boom", elapsed_ms=3, truncated=True
    )
    result = _build_solve_result(outcome, solver="cp-sat")

    assert result.truncated is True
    assert result.status == "error"
    assert result.return_code == 1


def test_solve_model_truncated_and_timed_out_keeps_timeout_verdict() -> None:
    # Both flags can be true (a burst overrun after a deadline kill); the timeout
    # branch wins for status, and truncated rides along.
    outcome = _RunOutcome(
        timed_out=True,
        returncode=-1,
        stdout=STREAM_SATISFY,
        stderr="",
        elapsed_ms=3,
        truncated=True,
    )
    result = _build_solve_result(outcome, solver="cp-sat")

    assert result.status == "timeout"
    assert result.truncated is True
    assert result.return_code is None


_OSERROR_TRANSLATION_CASES = [
    pytest.param(solve_model, "solve satisfy;", id="solve_model"),
    pytest.param(check_model, "solve satisfy;", id="check_model"),
    pytest.param(find_unsat_core, UNSAT_CORE_MODEL, id="find_unsat_core"),
]


@pytest.mark.parametrize(("entry_point", "model"), _OSERROR_TRANSLATION_CASES)
def test_wraps_oserror_as_execution_error(
    entry_point: Callable[..., Any],
    model: str,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> None:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        entry_point(model)
    assert "install-runtime" in str(exc_info.value)


def test_solve_model_writes_model_file_as_utf8(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    original_write_text = Path.write_text

    def _spy_write_text(self: Path, data: str, **kwargs: Any) -> int:
        captured["encoding"] = kwargs.get("encoding")
        return original_write_text(self, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", _spy_write_text)
    _record_subprocess(monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="", returncode=0))

    solve_model("% café λ\nsolve satisfy;")

    assert captured["encoding"] == "utf-8"


def test_check_model_happy_path_returns_ok(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    result = check_model("var 1..5: x;\nconstraint x > 2;\nsolve satisfy;")

    assert isinstance(result, CheckResult)
    assert result.status == "ok"
    assert result.solver == "cp-sat"
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_check_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    check_model(model)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]

    assert cmd[0] == str(fake_minizinc_binary)
    solver_idx = cmd.index("--solver")
    assert cmd[solver_idx + 1] == "cp-sat"
    timeout_idx = cmd.index("--time-limit")
    assert cmd[timeout_idx + 1] == "30000"
    assert "-c" in cmd
    # Statistics, the json-stream transport, and the solve-only search-control
    # flags are never requested by a compile-check.
    assert "--statistics" not in cmd
    assert "--json-stream" not in cmd
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))

    model_path = Path(cmd[-1])
    assert model_path.suffix == ".mzn"
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == model
    assert kwargs["cwd"] == str(model_path.parent)


def test_check_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    model = "int: n;\nvar 1..n: x;\nconstraint x = n;\nsolve satisfy;"
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    result = check_model(model, data="n = 3;")

    cmd = calls[0]["args"][0]
    # `-c` stays present; canonical model-then-data positional order.
    assert "-c" in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "n = 3;"
    assert result.status == "ok"


def test_check_model_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    check_model("var 1..5: x;\nsolve satisfy;")

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_check_model_custom_solver_is_passed_through(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    result = check_model("solve satisfy;", solver="gecode")

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--solver") + 1] == "gecode"
    assert result.solver == "gecode"


def test_check_model_custom_timeout_drives_outer_grace(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(monkeypatch, child_result(stdout="", stderr="", returncode=0))

    check_model("solve satisfy;", timeout_ms=5000)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == "5000"
    # The executor gets the model's --time-limit (5000 ms) plus the 5 s outer grace,
    # so MiniZinc normally self-stops before the executor's wall-clock cap fires.
    assert calls[0]["timeout_ms"] == 10000


def test_check_model_clean_exit_truncation_keeps_ok_and_flags_cap(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A burst writer can overrun the 1 MiB cap and exit 0 before the executor's
    # poll loop sees it. The rc-driven "ok" verdict stands, but the truncation is
    # surfaced structurally (field + output_truncated diagnostic), not just as
    # the raw stderr cap notice.
    _record_subprocess(monkeypatch, child_result(returncode=0, truncated=True))

    result = check_model("solve satisfy;")

    assert result.status == "ok"
    assert result.truncated is True
    assert result.diagnostic is not None
    assert result.diagnostic.category == "output_truncated"
    assert "output exceeded the 1 MiB cap; process stopped" in result.stderr


def test_check_model_returns_structured_result_for_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        ),
    )

    result = check_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert "type error" in result.stderr


def test_check_model_timeout_with_bytes_payload_decodes(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout="partial", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = check_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.stdout == "partial"
    assert result.stderr == ""
    assert result.elapsed_ms >= 0


def test_find_unsat_core_mus_found_preserves_raw_output(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "mus_found"
    assert len(result.core) == 2
    assert result.stdout == UNSAT_CORE_STDOUT
    assert "minimal unsatisfiable subset" in result.message


def test_find_unsat_core_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    kwargs = calls[0]["kwargs"]
    solver_idx = cmd.index("--solver")
    timeout_idx = cmd.index("--time-limit")
    model_path = Path(cmd[-1])

    assert cmd[solver_idx + 1] == FINDMUS_SOLVER
    assert cmd[timeout_idx + 1] == str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)
    assert "-c" not in cmd
    # Statistics, the json-stream transport, and the solve-only search-control
    # flags are never requested by the findMUS path.
    assert "--statistics" not in cmd
    assert "--json-stream" not in cmd
    assert not ({"-f", "-p", "-r", "-a"} & set(cmd))
    assert calls[0]["model_path_existed"]
    assert calls[0]["model_contents"] == UNSAT_CORE_MODEL
    assert kwargs["cwd"] == str(model_path.parent)


def test_find_unsat_core_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL, data="lo = 5;")

    cmd = calls[0]["args"][0]
    # findMUS only accepts a positional data file, never the --data flag.
    assert "--data" not in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "lo = 5;"
    assert cmd[cmd.index("--solver") + 1] == FINDMUS_SOLVER
    assert "-c" not in cmd
    # The structured core still resolves from the model text (model-only spans).
    assert result.status == "mus_found"
    assert len(result.core) == 2
    assert "x + y > 5" in result.core[0].source


def test_find_unsat_core_without_data_passes_only_model(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    assert not any(str(arg).endswith(".dzn") for arg in cmd)
    assert Path(cmd[-1]).suffix == ".mzn"
    assert calls[0]["data_contents"] is None


def test_find_unsat_core_no_core_clears_structured_core(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(stdout="=====UNKNOWN=====\n", stderr="", returncode=0),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "no_core"
    assert result.core == []


def test_find_unsat_core_error_clears_structured_core_and_preserves_stderr(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stderr = "Error: cannot load solver org.minizinc.findmus\n"
    _record_subprocess(
        monkeypatch,
        child_result(stdout="", stderr=stderr, returncode=1),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "error"
    assert result.core == []
    assert result.stderr == stderr


def test_find_unsat_core_truncation_kill_is_no_core_not_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An output-cap tree-kill: the -15 exit is the executor's artifact, not a
    # findMUS failure, so with no MUS in the capped transcript the verdict is
    # `no_core` (no verdict) — never the rc-derived `error` — mirroring the
    # solve path's masking policy.
    _record_subprocess(
        monkeypatch,
        child_result(
            stdout="FznSubProblem: partial preamble\n",
            stderr="",
            returncode=-15,
            truncated=True,
            truncation_killed=True,
        ),
    )

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "no_core"
    assert "stopped at the 1 MiB output cap" in result.message


def test_find_unsat_core_timeout_with_bytes_payload_decodes(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout="partial", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = find_unsat_core(UNSAT_CORE_MODEL)

    assert result.status == "timeout"
    assert result.core == []
    assert result.stdout == "partial"


def test_find_unsat_core_uses_default_timeout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=UNSAT_CORE_STDOUT, stderr="", returncode=0),
    )

    find_unsat_core(UNSAT_CORE_MODEL)

    cmd = calls[0]["args"][0]
    assert cmd[cmd.index("--time-limit") + 1] == str(DEFAULT_UNSAT_CORE_TIMEOUT_MS)


# --- inspect_model (--model-interface-only) --------------------------------

_INSPECT_KNAPSACK_MODEL = (
    "int: n;\narray[1..n] of int: weight;\narray[1..n] of var bool: take;\n"
    "solve maximize sum(i in 1..n)(weight[i] * take[i]);\n"
)
# A single-line interface object as the managed binary emits it (captured shape).
_INSPECT_KNAPSACK_STDOUT = (
    '{"type": "interface", "input": {"n": {"type": "int"}, "weight": '
    '{"type": "int", "dim": 1}}, "output": {"take": {"type": "bool", "dim": 1}}, '
    '"method": "max", "has_output_item": false, "included_files": [], "globals": []}'
)


def test_inspect_model_happy_path_returns_ok(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=_INSPECT_KNAPSACK_STDOUT, stderr="", returncode=0),
    )

    result = inspect_model(_INSPECT_KNAPSACK_MODEL)

    assert isinstance(result, ModelInspectionResult)
    assert result.status == "ok"
    assert result.solver == "cp-sat"
    assert result.interface is not None
    assert result.interface.method == "max"
    assert set(result.interface.required_parameters) == {"n", "weight"}
    assert result.interface.required_parameters["weight"].dim == 1
    assert result.elapsed_ms >= 0
    assert len(calls) == 1


def test_inspect_model_command_shape(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=_INSPECT_KNAPSACK_STDOUT, stderr="", returncode=0),
    )

    inspect_model(_INSPECT_KNAPSACK_MODEL)

    cmd = calls[0]["args"][0]
    assert "--model-interface-only" in cmd
    # Inspection is read-only type analysis: never the solve transport, the
    # statistics flag, or any solve-only search control.
    assert "--json-stream" not in cmd
    assert "--statistics" not in cmd
    assert not ({"-a", "-f", "-p", "-r", "-n"} & set(cmd))
    # The interface flag precedes the model file (extra_args go before the model).
    assert cmd.index("--model-interface-only") < cmd.index(
        next(arg for arg in cmd if str(arg).endswith(".mzn"))
    )


def test_inspect_model_returns_structured_error_for_compile_error(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _record_subprocess(
        monkeypatch,
        child_result(
            stdout="",
            stderr="Error: type error: undefined identifier 'xz'\n",
            returncode=1,
        ),
    )

    result = inspect_model("var 1..3: x;\nconstraint xz > 2;\nsolve satisfy;")

    assert result.status == "error"
    assert result.interface is None
    assert "type error" in result.stderr


def test_inspect_model_degrades_to_error_on_unparseable_stdout(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # rc 0 but stdout is not a parseable interface object: rather than mis-report a
    # partial interface, the result degrades to status="error" with interface None.
    _record_subprocess(
        monkeypatch,
        child_result(stdout="not an interface line\n", stderr="", returncode=0),
    )

    result = inspect_model("var 1..5: x;\nsolve satisfy;")

    assert result.status == "error"
    assert result.interface is None
    # The raw stdout is preserved so the failure is diagnosable.
    assert "not an interface line" in result.stdout


def test_inspect_model_returns_timeout_when_run_times_out(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hard timeout during interface extraction classifies as status="timeout" with
    # no interface, completing the status matrix alongside ok / error-compile /
    # error-unparseable. Driven by a raised TimeoutExpired, the same idiom check uses.
    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        return child_result(stdout="partial", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)

    result = inspect_model("solve satisfy;")

    assert result.status == "timeout"
    assert result.interface is None
    assert result.stdout == "partial"
    assert result.stderr == ""


def test_inspect_model_forwards_inline_data_positionally(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty_interface = (
        '{"type": "interface", "input": {}, "output": {"x": {"type": "int"}}, '
        '"method": "sat", "has_output_item": false}'
    )
    calls = _record_subprocess(
        monkeypatch,
        child_result(stdout=empty_interface, stderr="", returncode=0),
    )

    result = inspect_model("int: n;\nvar 1..n: x;\nsolve satisfy;", data="n = 3;")

    cmd = calls[0]["args"][0]
    assert "--model-interface-only" in cmd
    assert Path(cmd[-2]).suffix == ".mzn"
    assert Path(cmd[-1]).suffix == ".dzn"
    assert calls[0]["data_contents"] == "n = 3;"
    # Data supplied -> the interface reports nothing still required (completeness).
    assert result.status == "ok"
    assert result.interface is not None
    assert result.interface.required_parameters == {}


def test_inspect_default_timeout_aliases_check_budget() -> None:
    # Decision 7: inspection is a preflight like check, so it shares the check
    # budget rather than an independent literal that could drift.
    assert DEFAULT_INSPECT_TIMEOUT_MS == DEFAULT_CHECK_TIMEOUT_MS


# --- save_verified_model -----------------------------------------------------


_SAVE_MODEL = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;\n"


def _fake_check_then_solve(
    monkeypatch: pytest.MonkeyPatch,
    *,
    check: ChildExecutionResult,
    solve: ChildExecutionResult,
) -> list[list[str]]:
    """Route the save's two managed runs by argv: the ``-c`` compile gate gets
    ``check``, the json-stream solve gets ``solve``. Returns the captured argvs."""
    cmds: list[list[str]] = []

    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        cmd = [str(part) for part in args[0]]
        cmds.append(cmd)
        return check if "-c" in cmd else solve

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    return cmds


def test_save_verified_model_rejects_unsupported_control_before_check(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # An unsupported -a/-f/-p/-r is rejected before the compile check, the solve,
    # or any write — and the capability map is resolved at most once for the whole
    # save (D1). subprocess.run would run the check/solve, so it must not fire.
    _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _fail_if_solve_runs(monkeypatch)
    target = tmp_path / "project"

    with pytest.raises(ValueError, match="free_search"):
        save_verified_model(
            _SAVE_MODEL, target_dir=target, controls=SolveControls(free_search=True)
        )

    assert not target.exists()


def test_save_verified_model_supported_control_resolves_capabilities_once(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The save's compile check and internal solve trust the one up-front
    # enforcement: a gated control resolves the capability map exactly once (D1).
    resolve_calls = _patch_capabilities(
        monkeypatch, {"cp-sat": SolverCapabilities(supports_free_search=True)}
    )
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )

    result = save_verified_model(
        _SAVE_MODEL, target_dir=tmp_path / "project", controls=SolveControls(free_search=True)
    )

    assert result.status == "saved"
    assert resolve_calls[0] == 1


def test_save_verified_model_manifest_solve_controls_keys_and_order(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # The manifest's solve_controls is timeout_ms followed by the five controls in
    # a fixed order; saved manifests must stay byte-identical, key order included.
    solver = "org.chuffed.chuffed"
    _patch_capabilities(
        monkeypatch,
        {
            solver: SolverCapabilities(
                supports_all_solutions=True,
                supports_free_search=True,
                supports_parallel=True,
                supports_random_seed=True,
            )
        },
    )
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    save_verified_model(
        _SAVE_MODEL,
        target_dir=target,
        solver=solver,
        timeout_ms=5_000,
        controls=SolveControls(
            free_search=True,
            parallel=2,
            random_seed=7,
            all_solutions=True,
            num_solutions=3,
        ),
    )

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert list(manifest["solve_controls"].items()) == [
        ("timeout_ms", 5_000),
        ("free_search", True),
        ("parallel", 2),
        ("random_seed", 7),
        ("all_solutions", True),
        ("num_solutions", 3),
    ]


def test_save_verified_model_satisfied_solve_writes_project(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmds = _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target)

    assert result.status == "saved"
    assert result.target_dir == str(target)
    assert str(target) in result.message
    assert result.check.status == "ok"
    assert result.solve is not None
    assert result.solve.status == "satisfied"
    assert (target / MODEL_FILENAME).read_text() == _SAVE_MODEL
    assert (target / SOLVE_RESULT_FILENAME).is_file()
    assert (target / MANIFEST_FILENAME).is_file()
    # Optional artifacts were not supplied, so their files do not appear.
    assert not (target / DATA_FILENAME).exists()
    assert not (target / CHECKER_FILENAME).exists()
    assert not (target / PROBLEM_FILENAME).exists()
    # The compile gate ran first, then the one solve.
    assert len(cmds) == 2
    assert "-c" in cmds[0]
    assert "--json-stream" in cmds[1]


def test_save_verified_model_compile_error_writes_nothing(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmds = _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="type error: undefined 'xz'", returncode=1),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target)

    assert result.status == "not_verified"
    assert result.check.status == "error"
    assert result.solve is None
    assert "nothing was written" in result.message
    assert not target.exists()
    # The check gate failed, so the solve never ran.
    assert len(cmds) == 1


def test_save_verified_model_unsatisfiable_solve_writes_nothing(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_UNSAT, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target)

    assert result.status == "not_verified"
    assert result.solve is not None
    assert result.solve.status == "unsatisfiable"
    assert "unsatisfiable" in result.message
    assert not target.exists()


def test_save_verified_model_compile_error_surfaces_gate_diagnostic(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="type error: undefined 'xz'", returncode=1),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )

    result = save_verified_model(_SAVE_MODEL, target_dir=tmp_path / "project")

    # The failed compile gate's own classification is surfaced, not a bare
    # not_verified, so the client can branch on the specific modeling error.
    assert result.diagnostic is not None
    assert result.diagnostic.category == "type_error"


def test_save_verified_model_unsatisfiable_surfaces_infeasible_diagnostic(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_UNSAT, stderr="", returncode=0),
    )

    result = save_verified_model(_SAVE_MODEL, target_dir=tmp_path / "project")

    assert result.diagnostic is not None
    assert result.diagnostic.category == "infeasible"


def test_save_verified_model_saved_has_no_diagnostic(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )

    result = save_verified_model(_SAVE_MODEL, target_dir=tmp_path / "project")

    assert result.status == "saved"
    assert result.diagnostic is None


def test_save_verified_model_solve_timeout_blocks_saving(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def _fake_run(*args: Any, **kwargs: Any) -> ChildExecutionResult:
        cmd = [str(part) for part in args[0]]
        if "-c" in cmd:
            return child_result(stdout="", stderr="", returncode=0)
        return child_result(stdout="", stderr="", returncode=0, timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake_run)
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target, checker=_CHECKER_SRC)

    assert result.status == "not_verified"
    assert result.solve is not None
    assert result.solve.timed_out is True
    # With a checker supplied, the nested report degrades to timeout too.
    assert result.solve.checker is not None
    assert result.solve.checker.status == "timeout"
    assert "timed out" in result.message
    assert not target.exists()


def test_save_verified_model_nonzero_exit_blocks_saving(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # STREAM_SATISFY carries no status object, so a solution with rc 1 still
    # classifies `satisfied` — the gate must catch the dirty exit on its own.
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="boom", returncode=1),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target)

    assert result.status == "not_verified"
    assert "exited with code 1" in result.message
    assert not target.exists()


def test_save_verified_model_truncated_solve_blocks_saving_with_truncation_message(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A truncation tree-kill leaves status "satisfied" (stream verdict) with
    # return_code None; the gate must name the truncation — not fall through to
    # the rc check and report "exited with code None".
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(
            stdout=STREAM_SATISFY,
            stderr="",
            returncode=-15,
            truncated=True,
            truncation_killed=True,
        ),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target)

    assert result.status == "not_verified"
    assert "truncated" in result.message
    assert "code None" not in result.message
    assert not target.exists()


def test_save_verified_model_checker_completed_allows_saving(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    solve_stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=3\n", {"x": 3}),
        {"type": "status", "status": "SATISFIED"},
    )
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=solve_stdout, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target, checker=_CHECKER_SRC)

    assert result.status == "saved"
    assert result.solve is not None
    assert result.solve.checker is not None
    assert result.solve.checker.status == "completed"
    assert (target / CHECKER_FILENAME).read_text() == _CHECKER_SRC


@pytest.mark.parametrize(
    "solve_stdout",
    [
        # Constraint-style rejection: checker status "violation".
        stream(
            checker_violation(),
            solution_obj("x=1\n", {"x": 1}),
            {"type": "status", "status": "SATISFIED"},
        ),
        # Missing per-solution verdict: checker status "error".
        stream(
            solution_obj("x=1\n", {"x": 1}),
            {"type": "status", "status": "SATISFIED"},
        ),
        # No solution produced: checker status "no_solution" (and the solve
        # status gate fails first on "unsatisfiable").
        stream({"type": "status", "status": "UNSATISFIABLE"}),
    ],
)
def test_save_verified_model_unverified_checker_blocks_saving(
    solve_stdout: str,
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=solve_stdout, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(_SAVE_MODEL, target_dir=target, checker=_CHECKER_SRC)

    assert result.status == "not_verified"
    assert not target.exists()


def test_save_verified_model_saves_data_and_problem_when_supplied(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"

    result = save_verified_model(
        _SAVE_MODEL, target_dir=target, data="n = 3;\n", problem="Pick x above 2.\n"
    )

    assert result.status == "saved"
    assert (target / DATA_FILENAME).read_text() == "n = 3;\n"
    assert (target / PROBLEM_FILENAME).read_text() == "Pick x above 2.\n"
    assert [artifact.role for artifact in result.files] == [
        "model",
        "data",
        "problem",
        "solve_result",
        "manifest",
    ]


def test_save_verified_model_passes_same_data_to_check_and_solve(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cmds = _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )

    save_verified_model(_SAVE_MODEL, target_dir=tmp_path / "project", data="n = 3;\n")

    # Both runs carry the positional data file, so the verified instance is the
    # instance that was checked.
    assert cmds[0][-1].endswith(".dzn")
    assert cmds[1][-1].endswith(".dzn")


def test_save_verified_model_overwrite_replaces_prior_save(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    save_verified_model(_SAVE_MODEL, target_dir=target)

    new_model = "var 1..9: y;\nsolve satisfy;\n"
    result = save_verified_model(new_model, target_dir=target, overwrite=True)

    assert result.status == "saved"
    assert (target / MODEL_FILENAME).read_text() == new_model


def _fail_if_subprocess_called(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be invoked for invalid save args")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fail)


def test_save_verified_model_rejects_empty_model_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    with pytest.raises(ValueError, match="empty"):
        save_verified_model("  \n", target_dir=tmp_path / "project")


def test_save_verified_model_rejects_relative_target_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    with pytest.raises(ValueError, match="absolute"):
        save_verified_model(_SAVE_MODEL, target_dir=Path("relative/project"))


def test_save_verified_model_rejects_missing_parent_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    with pytest.raises(ValueError, match="parent"):
        save_verified_model(_SAVE_MODEL, target_dir=tmp_path / "missing" / "project")


def test_save_verified_model_rejects_file_target_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    occupied = tmp_path / "occupied"
    occupied.write_text("not a directory")
    with pytest.raises(ValueError, match="not a directory"):
        save_verified_model(_SAVE_MODEL, target_dir=occupied)


def test_save_verified_model_rejects_unmanaged_target_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    target = tmp_path / "project"
    target.mkdir()
    (target / "thesis.tex").write_text("important")
    with pytest.raises(ValueError, match="not empty"):
        save_verified_model(_SAVE_MODEL, target_dir=target, overwrite=True)


def test_save_verified_model_rejects_gated_num_solutions_before_any_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The solve controls are validated with the exact solve_model rules before
    # the compile check spends a subprocess on a doomed request.
    _fail_if_subprocess_called(monkeypatch)
    with pytest.raises(ValueError, match="num_solutions"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "project", controls=SolveControls(num_solutions=2)
        )


# --- save_verified_model(portfolio_result=...) ------------------------------


def _portfolio_winner_solve_result(*, status: str = "optimal") -> SolveResult:
    return SolveResult(
        status=status,
        solver=DEFAULT_SOLVER,
        return_code=0,
        timed_out=status == "timeout",
        stdout="",
        stderr="",
        elapsed_ms=100,
        solution={"x": 3},
        solutions=[{"x": 3}],
        objective=None,
    )


def _portfolio_attempt(
    *,
    solver: str = DEFAULT_SOLVER,
    seed: int | None = None,
    result_status: str = "optimal",
    checker_status: str | None = None,
) -> PortfolioAttempt:
    return PortfolioAttempt(
        index=0,
        model_index=0,
        solver=solver,
        seed=seed,
        timeout_ms=5000,
        state="succeeded",
        job_id="job-0",
        job_state="succeeded",
        result_status=result_status,
        objective=None,
        elapsed_ms=100,
        checker_status=checker_status,
    )


# The shared solve controls save_verified_model defaults to; a race with these
# controls is eagerly consistent with a default save call.
_DEFAULT_PORTFOLIO_CONTROLS = PortfolioSolveControls(
    free_search=False, parallel=None, all_solutions=False, num_solutions=None
)


def _portfolio_result(
    *,
    model: str = _SAVE_MODEL,
    solver: str = DEFAULT_SOLVER,
    seed: int | None = None,
    result_status: str = "optimal",
    checker_status: str | None = None,
    models_sha256: list[str] | None = None,
    data_sha256: str | None = None,
    checker_sha256: str | None = None,
    solve_controls: PortfolioSolveControls | None = None,
) -> PortfolioSolveResult:
    """Build a minimal, self-consistent winning ``PortfolioSolveResult``.

    The sole attempt (index 0, model_index 0) is eagerly consistent with a
    save of ``model`` using ``solver``/``seed`` by default; pass
    ``data_sha256=text_sha256(data)`` to match a save that also supplies
    ``data`` (the default ``None`` matches a dataless save). Callers force a
    specific mismatch by overriding the corresponding keyword.
    """
    return PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=_portfolio_winner_solve_result(status=result_status),
        attempts=[
            _portfolio_attempt(
                solver=solver,
                seed=seed,
                result_status=result_status,
                checker_status=checker_status,
            )
        ],
        elapsed_ms=150,
        selection_policy="first-decisive-result",
        models_sha256=models_sha256 if models_sha256 is not None else [text_sha256(model)],
        data_sha256=data_sha256,
        checker_sha256=checker_sha256,
        solve_controls=(
            solve_controls if solve_controls is not None else _DEFAULT_PORTFOLIO_CONTROLS
        ),
    )


def test_save_verified_model_with_portfolio_result_writes_experiment_log(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result()

    result = save_verified_model(_SAVE_MODEL, target_dir=target, portfolio_result=portfolio_result)

    assert result.status == "saved"
    log_path = target / EXPERIMENT_LOG_FILENAME
    assert log_path.is_file()

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    artifact_roles = {a["role"]: a["path"] for a in manifest["artifacts"]}
    assert artifact_roles["experiment_log"] == EXPERIMENT_LOG_FILENAME

    summary = manifest["verification"]["experiment_log"]
    assert summary["exploration_type"] == "minizinc_portfolio"
    assert summary["winner_index"] == 0
    assert summary["winner_seed"] is None
    assert summary["winner_solver"] == DEFAULT_SOLVER
    assert summary["winner_model_index"] == 0
    assert summary["attempt_count"] == 1
    assert summary["terminal_attempt_count"] == 1
    assert summary["cancelled_attempt_count"] == 0
    assert summary["statuses_seen"] == ["optimal"]
    assert summary["attempt_states_seen"] == ["succeeded"]
    assert summary["selection_policy"] == "first-decisive-result"


def test_save_verified_model_portfolio_result_rejected_attempt_counts_as_terminal(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A `rejected` attempt was never admitted, so it is final — the manifest
    # summary must count it in `terminal_attempt_count` alongside the registry's
    # terminal states (see PORTFOLIO_ATTEMPT_TERMINAL_STATES).
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    rejected_attempt = PortfolioAttempt(
        index=1,
        model_index=0,
        solver=DEFAULT_SOLVER,
        timeout_ms=5000,
        state="rejected",
    )
    portfolio_result = PortfolioSolveResult(
        status="winner",
        winner_index=0,
        winner=_portfolio_winner_solve_result(),
        attempts=[_portfolio_attempt(), rejected_attempt],
        elapsed_ms=150,
        selection_policy="first-decisive-result",
        models_sha256=[text_sha256(_SAVE_MODEL)],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_DEFAULT_PORTFOLIO_CONTROLS,
    )

    save_verified_model(_SAVE_MODEL, target_dir=target, portfolio_result=portfolio_result)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    summary = manifest["verification"]["experiment_log"]
    assert summary["terminal_attempt_count"] == 2
    assert summary["statuses_seen"] == ["optimal"]
    assert summary["attempt_states_seen"] == ["rejected", "succeeded"]


def test_save_verified_model_portfolio_result_log_hashes_match_saved_artifacts(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    data = "n = 3;\n"
    portfolio_result = _portfolio_result(data_sha256=text_sha256(data), checker_sha256="c" * 64)

    save_verified_model(
        _SAVE_MODEL, target_dir=target, data=data, portfolio_result=portfolio_result
    )

    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert log["managed_by"] == "openconstraint-mcp"
    assert log["exploration_type"] == "minizinc_portfolio"
    assert log["models_sha256"] == [text_sha256(_SAVE_MODEL)]
    assert log["data_sha256"] == text_sha256(data)
    assert log["checker_sha256"] == "c" * 64
    assert log["solve_controls"] == {
        "free_search": False,
        "parallel": None,
        "all_solutions": False,
        "num_solutions": None,
    }
    assert len(log["attempts"]) == 1
    assert log["attempts"][0] == {
        "index": 0,
        "model_index": 0,
        "solver": DEFAULT_SOLVER,
        "seed": None,
        "timeout_ms": 5000,
        "state": "succeeded",
        "job_state": "succeeded",
        "result_status": "optimal",
        "checker_status": None,
        "objective": None,
        "elapsed_ms": 100,
        "message": None,
    }


def test_save_verified_model_portfolio_result_log_row_surfaces_checker_status(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # A checker-rejected attempt is purely observational (see PortfolioAttempt's
    # docstring) — it does not affect winner selection — but its verdict must
    # still make it into the persisted experiment-log.json row, not just onto
    # the in-memory PortfolioAttempt (see test_portfolio_attempt_surfaces_checker_status).
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result(checker_status="violation")

    save_verified_model(_SAVE_MODEL, target_dir=target, portfolio_result=portfolio_result)

    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert log["attempts"][0]["checker_status"] == "violation"


def test_save_verified_model_portfolio_result_failed_save_writes_nothing(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # Same pattern as the existing no-portfolio failure tests: a failed fresh
    # solve writes nothing, portfolio_result attached or not.
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_UNSAT, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result()

    result = save_verified_model(_SAVE_MODEL, target_dir=target, portfolio_result=portfolio_result)

    assert result.status == "not_verified"
    assert not target.exists()


def test_save_verified_model_portfolio_result_no_winner_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    portfolio_result = PortfolioSolveResult(
        status="no_winner",
        winner_index=None,
        winner=None,
        attempts=[_portfolio_attempt()],
        elapsed_ms=150,
        selection_policy="first-decisive-result",
        models_sha256=[text_sha256(_SAVE_MODEL)],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_DEFAULT_PORTFOLIO_CONTROLS,
    )

    with pytest.raises(ValueError, match="no_winner"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "project", portfolio_result=portfolio_result
        )


def test_save_verified_model_portfolio_result_winner_index_out_of_bounds_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # PortfolioSolveResult's own validators check attempt.model_index against
    # models_sha256, but nothing checks winner_index against attempts — so this
    # constructs cleanly and only core.py's own defensive bounds check catches it.
    _fail_if_subprocess_called(monkeypatch)
    portfolio_result = PortfolioSolveResult(
        status="winner",
        winner_index=5,
        winner=_portfolio_winner_solve_result(),
        attempts=[_portfolio_attempt()],
        elapsed_ms=150,
        selection_policy="first-decisive-result",
        models_sha256=[text_sha256(_SAVE_MODEL)],
        data_sha256=None,
        checker_sha256=None,
        solve_controls=_DEFAULT_PORTFOLIO_CONTROLS,
    )

    with pytest.raises(ValueError, match="winner_index"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "project", portfolio_result=portfolio_result
        )


def test_save_verified_model_portfolio_result_solver_mismatch_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    portfolio_result = _portfolio_result(solver="org.gecode.gecode")

    with pytest.raises(ValueError, match="solver"):
        save_verified_model(
            _SAVE_MODEL,
            target_dir=tmp_path / "project",
            solver=DEFAULT_SOLVER,
            portfolio_result=portfolio_result,
        )


def test_save_verified_model_portfolio_result_seed_mismatch_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A non-None random_seed is itself a gated control (-r), so it is allowlisted
    # here (matching test_save_verified_model_rejects_unsupported_control_before_check's
    # pattern) purely to reach the portfolio_result check; _fail_if_solve_runs still
    # proves neither the compile check nor the solve ever runs.
    _patch_capabilities(
        monkeypatch, {DEFAULT_SOLVER: SolverCapabilities(supports_random_seed=True)}
    )
    _fail_if_solve_runs(monkeypatch)
    portfolio_result = _portfolio_result(seed=7)

    with pytest.raises(ValueError, match="winning attempt's seed"):
        save_verified_model(
            _SAVE_MODEL,
            target_dir=tmp_path / "project",
            controls=SolveControls(random_seed=8),
            portfolio_result=portfolio_result,
        )


def test_save_verified_model_portfolio_result_model_hash_mismatch_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    # Built for a different model text than the one actually saved below.
    portfolio_result = _portfolio_result(model="solve satisfy;\n")

    with pytest.raises(ValueError, match="models_sha256"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "project", portfolio_result=portfolio_result
        )


def test_save_verified_model_portfolio_result_data_hash_mismatch_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fail_if_subprocess_called(monkeypatch)
    # data_sha256 defaults to None (a dataless race) but this save supplies data.
    portfolio_result = _portfolio_result()

    with pytest.raises(ValueError, match="data_sha256"):
        save_verified_model(
            _SAVE_MODEL,
            target_dir=tmp_path / "project",
            data="n = 3;\n",
            portfolio_result=portfolio_result,
        )


@pytest.mark.parametrize(
    ("control", "race_value"),
    [
        ("free_search", True),
        ("parallel", 4),
        ("all_solutions", True),
        ("num_solutions", 3),
    ],
)
def test_save_verified_model_portfolio_result_solve_controls_mismatch_raises_before_runtime_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, control: str, race_value: object
) -> None:
    # The race ran with a non-default shared search control, so a default save is
    # not a replay of the winning attempt's configuration — rejected eagerly,
    # before any subprocess (timeout_ms, a budget, is deliberately not gated).
    _fail_if_subprocess_called(monkeypatch)
    controls = _DEFAULT_PORTFOLIO_CONTROLS.model_copy(update={control: race_value})
    portfolio_result = _portfolio_result(solve_controls=controls)

    with pytest.raises(ValueError, match=f"solve_controls.{control}"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "project", portfolio_result=portfolio_result
        )


def test_save_verified_model_portfolio_result_unseeded_winner_matches_unseeded_save(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # seed=None on both sides is a VALID match (an unseeded portfolio winner
    # attached to an unseeded save) — paired with a fresh check/solve mock that
    # lets the save actually succeed, so this reaches the write step.
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_SATISFY, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result(seed=None)

    result = save_verified_model(
        _SAVE_MODEL,
        target_dir=target,
        controls=SolveControls(random_seed=None),
        portfolio_result=portfolio_result,
    )

    assert result.status == "saved"
    assert (target / EXPERIMENT_LOG_FILENAME).is_file()


def test_empty_string_data_does_not_match_a_dataless_portfolio_result_in_either_direction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # An empty-string `data` hashes to text_sha256(""), distinct from the None
    # sentinel a dataless race records — neither direction may match the other.
    _fail_if_subprocess_called(monkeypatch)

    # portfolio ran dataless (data_sha256=None); save supplies empty-string data.
    portfolio_result = _portfolio_result(data_sha256=None)
    with pytest.raises(ValueError, match="data_sha256"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "a", data="", portfolio_result=portfolio_result
        )

    # portfolio recorded empty-string data's hash; save is dataless.
    portfolio_result = _portfolio_result(data_sha256=text_sha256(""))
    with pytest.raises(ValueError, match="data_sha256"):
        save_verified_model(
            _SAVE_MODEL, target_dir=tmp_path / "b", data=None, portfolio_result=portfolio_result
        )


def test_save_verified_model_portfolio_result_checker_hash_mismatch_is_not_rejected(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # checker_sha256 is informational-only: the fresh checker gate decides, so a
    # mismatched hash does not block a save that otherwise verifies.
    solve_stdout = stream(
        checker_pass("CORRECT\n"),
        solution_obj("x=3\n", {"x": 3}),
        {"type": "status", "status": "SATISFIED"},
    )
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=solve_stdout, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result(checker_sha256="mismatched-hash".ljust(64, "0"))

    result = save_verified_model(
        _SAVE_MODEL, target_dir=target, checker=_CHECKER_SRC, portfolio_result=portfolio_result
    )

    assert result.status == "saved"
    assert result.solve is not None
    assert result.solve.checker is not None
    assert result.solve.checker.status == "completed"


def test_save_verified_model_portfolio_result_optimal_winner_does_not_bypass_fresh_failure(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The winning attempt's own ``result_status`` is ``"optimal"`` (so it is
    clearly NOT the thing gating the save), but the *fresh* mocked solve
    reports unsatisfiable. If the gate ever read ``portfolio_result.winner``
    instead of the fresh solve, this save would incorrectly succeed — so this
    is the case that actually proves the gate reads the fresh result."""
    _fake_check_then_solve(
        monkeypatch,
        check=child_result(stdout="", stderr="", returncode=0),
        solve=child_result(stdout=STREAM_UNSAT, stderr="", returncode=0),
    )
    target = tmp_path / "project"
    portfolio_result = _portfolio_result(result_status="optimal")

    result = save_verified_model(_SAVE_MODEL, target_dir=target, portfolio_result=portfolio_result)

    assert result.status == "not_verified"
    assert not target.exists()


# --- cancellable Popen runner (Capability 1, Task 1.2) ---------------------


class _FakeHandle:
    """Opaque process-handle stand-in the mocked executor hands to on_start."""

    def __init__(self) -> None:
        self.pid = 4321


def _patch_cancellable_executor(
    monkeypatch: pytest.MonkeyPatch, result: ChildExecutionResult
) -> list[dict[str, Any]]:
    """Patch ``minizinc.core.execute_child`` for the cancellable runner.

    Records each call and returns ``result``. When the runner passes ``on_start``
    (publish-for-cancellation), invokes it with a fake handle — mirroring the real
    executor, which calls on_start with the live ``Popen`` right after launch. The
    timeout / output-cap / tree-kill loop itself is covered once, in
    ``tests/shared/test_childrun.py``.
    """
    calls: list[dict[str, Any]] = []

    def _fake(
        argv: list[str],
        cwd: Any,
        *,
        timeout_ms: int,
        tracker: Any = None,
        on_start: Any = None,
    ) -> ChildExecutionResult:
        handle = _FakeHandle()
        calls.append(
            {
                "argv": list(argv),
                "cwd": str(cwd),
                "timeout_ms": timeout_ms,
                "tracker": tracker,
                "on_start": on_start,
                "handle": handle,
            }
        )
        if on_start is not None:
            on_start(handle)
        return result

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _fake)
    return calls


def test_run_prepared_solve_does_not_resolve_capabilities(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The job worker trusts admission: the cancellable solve must NOT resolve
    # capabilities even with a gated control set (the patched cp-sat declares no
    # stdFlags, so a re-resolve would also wrongly reject). A gated-control job
    # thus runs --solvers-json at most once — at admission, never in the worker.
    resolve_calls = _patch_capabilities(monkeypatch, {"cp-sat": SolverCapabilities()})
    _patch_cancellable_executor(monkeypatch, child_result(stdout=STREAM_SATISFY, returncode=0))
    result = run_prepared_solve(
        "var 1..5: x;\nsolve satisfy;",
        solver=DEFAULT_SOLVER,
        data=None,
        checker=None,
        timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
        extra_args=build_solve_extra_args(DEFAULT_SOLVER, SolveControls(free_search=True)),
        on_start=lambda _proc: None,
    )
    assert result.status == "satisfied"
    assert resolve_calls[0] == 0


def test_cancellable_runner_invokes_on_start_with_the_handle(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cancellable runner forwards on_start to the executor, which calls it with
    # the live handle so the caller can publish it for targeted cancellation.
    calls = _patch_cancellable_executor(
        monkeypatch, child_result(stdout=STREAM_SATISFY, returncode=0)
    )
    seen: list[Any] = []

    _run_managed_minizinc(
        "var 1..5: x;\nsolve satisfy;",
        solver=DEFAULT_SOLVER,
        timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
        extra_args=(),
        on_start=seen.append,
    )

    assert seen == [calls[0]["handle"]]


def test_cancellable_runner_clean_run_returns_outcome(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_cancellable_executor(
        monkeypatch, child_result(stdout=STREAM_SATISFY, stderr="warn\n", returncode=0)
    )

    outcome = _run_managed_minizinc(
        "var 1..5: x;\nsolve satisfy;",
        solver=DEFAULT_SOLVER,
        timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
        extra_args=(),
        on_start=lambda _proc: None,
    )

    assert outcome.timed_out is False
    assert outcome.returncode == 0
    assert outcome.stdout == STREAM_SATISFY
    assert outcome.stderr == "warn\n"


def test_cancellable_runner_timeout_adapts_to_timed_out_outcome(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The executor reports timed_out=True after its own tree-kill (covered in
    # tests/shared/test_childrun.py); the cancellable runner's adapter must surface
    # that as a timed_out outcome carrying the drained partial output and the -1
    # returncode sentinel (never read while timed out).
    _patch_cancellable_executor(monkeypatch, child_result(stdout="partial\n", timed_out=True))

    outcome = _run_managed_minizinc(
        "var 1..5: x;\nsolve satisfy;",
        solver=DEFAULT_SOLVER,
        timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
        extra_args=(),
        on_start=lambda _proc: None,
    )

    assert outcome.timed_out is True
    assert outcome.stdout == "partial\n"
    assert outcome.returncode == -1


def test_cancellable_runner_wraps_oserror_as_execution_error(
    fake_minizinc_binary: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError(8, "Exec format error")

    monkeypatch.setattr("openconstraint_mcp.minizinc.core.execute_child", _boom)

    with pytest.raises(MiniZincExecutionError) as exc_info:
        _run_managed_minizinc(
            "solve satisfy;",
            solver=DEFAULT_SOLVER,
            timeout_ms=DEFAULT_SOLVE_TIMEOUT_MS,
            extra_args=(),
            on_start=lambda _proc: None,
        )
    assert "install-runtime" in str(exc_info.value)
