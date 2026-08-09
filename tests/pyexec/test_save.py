"""Unit tests for pyexec/save.py — executor mocked for speed."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.core import VERIFIED_STATUSES, config_sha256
from openconstraint_mcp.pyexec.save import (
    CHECKER_FILENAME,
    SOLUTION_FILENAME,
    save_verified_cpsat_python,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerReport,
    CpsatExpectation,
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from openconstraint_mcp.shared.save_target import (
    EXPERIMENT_LOG_FILENAME,
    MANIFEST_FILENAME,
    text_sha256,
)

_SCRIPT = "print('hi')"
_OPTIMAL_RESULT = CpsatPythonResult(
    status="optimal",
    solution={"x": 3},
    objective=3.0,
    stdout='{"status":"optimal","objective":3,"solution":{"x":3}}',
    stderr="",
    return_code=0,
    timed_out=False,
    truncated=False,
    duration_ms=42,
)
_INFEASIBLE_RESULT = CpsatPythonResult(
    status="infeasible",
    solution=None,
    objective=None,
    stdout='{"status":"infeasible","objective":null,"solution":{}}',
    stderr="",
    return_code=0,
    timed_out=False,
    truncated=False,
    duration_ms=10,
)
_ERROR_RESULT = CpsatPythonResult(
    status="error",
    solution=None,
    objective=None,
    stdout="",
    stderr="NameError: name 'x' is not defined",
    return_code=1,
    timed_out=False,
    truncated=False,
    duration_ms=5,
)


def _patch_executor(monkeypatch: pytest.MonkeyPatch, result: CpsatPythonResult) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: result,
    )


def _patch_executor_counting(
    monkeypatch: pytest.MonkeyPatch, result: CpsatPythonResult
) -> list[bool]:
    """Like ``_patch_executor``, but returns a list that grows on every call."""
    calls: list[bool] = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: calls.append(True) or result,
    )
    return calls


# (a) solving script → saved=True, file on disk, manifest written
def test_save_verified_cpsat_python_optimal_saves_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is True
    assert result.status in VERIFIED_STATUSES
    assert (target / "model.py").is_file()
    assert (target / MANIFEST_FILENAME).is_file()
    assert (target / "model.py").read_text() == _SCRIPT


def test_save_verified_cpsat_python_preserves_model_source_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_text_newlines: None,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    source = "# café\r\nprint('x')\n"
    target = tmp_path / "exact_source"

    save_verified_cpsat_python(source, target_dir=target)

    assert (target / "model.py").read_bytes() == source.encode("utf-8")


def test_save_verified_cpsat_python_preserves_checker_source_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    windows_text_newlines: None,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    checker = "# café\r\nprint('x')\n"
    target = tmp_path / "exact_checker"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=checker)

    assert (target / CHECKER_FILENAME).read_bytes() == checker.encode("utf-8")


# (a2) manifest has correct structure
def test_save_verified_cpsat_python_manifest_structure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"
    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["managed_by"] == "openconstraint-mcp"
    assert isinstance(manifest["artifacts"], list)
    artifact_names = [a["path"] for a in manifest["artifacts"]]
    assert "model.py" in artifact_names


# (a2b) manifest names the backend so a client can choose replay tooling without guessing
def test_save_verified_cpsat_python_manifest_records_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"
    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["backend"] == "cpsat_python"


# (a2c) manifest records the save-time run timeout, always
def test_save_verified_cpsat_python_manifest_records_timeout_ms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"
    save_verified_cpsat_python(_SCRIPT, target_dir=target, script_timeout_ms=12_345)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["verification"]["script_timeout_ms"] == 12_345


# (a2d) manifest records an explicit checker_timeout_ms
def test_save_verified_cpsat_python_manifest_records_explicit_checker_timeout_ms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    target = tmp_path / "my_solution"

    save_verified_cpsat_python(
        _SCRIPT,
        target_dir=target,
        checker=_CHECKER_SOURCE,
        checker_timeout_ms=9_999,
    )

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["verification"]["checker_timeout_ms"] == 9_999


# (a2e) no explicit checker_timeout_ms -> manifest omits the field, matching replay_seed's contract
def test_save_verified_cpsat_python_manifest_omits_checker_timeout_ms_when_not_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    target = tmp_path / "my_solution"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert "checker_timeout_ms" not in manifest["verification"]


# (a3) problem.txt written when problem supplied
def test_save_verified_cpsat_python_writes_problem_txt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "my_solution"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, problem="Assign workers to tasks."
    )

    assert result.saved is True
    assert (target / "problem.txt").is_file()
    assert (target / "problem.txt").read_text() == "Assign workers to tasks."


# (a4, a5, b, b2) unsaved result shapes → saved=False, reason set, nothing written
_UNSAVED_RESULT_CASES = [
    pytest.param(
        CpsatPythonResult(
            status="optimal",
            solution=None,
            objective=None,
            stdout='{"status":"optimal","objective":null,"solution":null}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=7,
        ),
        "no_solution",
        id="optimal-no-solution",
    ),
    pytest.param(
        CpsatPythonResult(
            status="optimal",
            solution={},
            objective=None,
            stdout='{"status":"optimal","objective":null,"solution":{}}',
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=7,
        ),
        "empty_solution",
        id="optimal-empty-solution",
    ),
    pytest.param(_INFEASIBLE_RESULT, "infeas", id="infeasible"),
    pytest.param(_ERROR_RESULT, "err", id="error"),
]


@pytest.mark.parametrize(("executor_result", "target_name"), _UNSAVED_RESULT_CASES)
def test_save_verified_cpsat_python_unsaved_result_does_not_save(
    executor_result: CpsatPythonResult,
    target_name: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, executor_result)
    target = tmp_path / target_name

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is False
    assert result.reason is not None
    assert result.target_dir is None
    assert not target.exists()


# (c) relative target_dir → ValueError before executor runs
def test_save_verified_cpsat_python_relative_path_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_cpsat_python",
        lambda source, **kw: called.append(True) or _OPTIMAL_RESULT,
    )

    with pytest.raises(ValueError, match="absolute"):
        save_verified_cpsat_python(_SCRIPT, target_dir=Path("relative/path"))

    assert not called, "executor must not be called before path validation"


# (d) non-empty unmanaged dir → ValueError
def test_save_verified_cpsat_python_unmanaged_nonempty_dir_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "some_other_file.txt").write_text("not ours")

    with pytest.raises(ValueError, match="not empty"):
        save_verified_cpsat_python(_SCRIPT, target_dir=target)


# (e) existing managed save without overwrite → refusal
def test_save_verified_cpsat_python_existing_managed_no_overwrite_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "managed"

    # First save
    save_verified_cpsat_python(_SCRIPT, target_dir=target)
    assert target.is_dir()

    # Second save without overwrite
    with pytest.raises(ValueError, match="overwrite"):
        save_verified_cpsat_python(_SCRIPT, target_dir=target, overwrite=False)


# (f) overwrite=True replaces managed directory
def test_save_verified_cpsat_python_overwrite_replaces_managed_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "managed"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)
    new_script = "# updated script"
    save_verified_cpsat_python(new_script, target_dir=target, overwrite=True)

    assert (target / "model.py").read_text() == new_script
    assert (target / MANIFEST_FILENAME).is_file()


# --- Expectation gate tests -------------------------------------------------


def test_save_no_expectation_saves_with_reported_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert result.saved is True
    assert result.verification_level == "reported"
    assert result.reported_passed is True
    assert result.expectation_passed is None
    assert result.checker is None


def test_save_maximize_expectation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=2.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is True
    assert result.verification_level == "expectation"
    assert result.expectation_passed is True
    assert result.expectation is not None
    assert result.expectation.objective_sense == "maximize"


def test_save_maximize_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.reported_passed is True
    assert result.expectation_passed is False
    assert result.checker is None
    assert not target.exists()


def test_save_minimize_expectation_passes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=100.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is True
    assert result.verification_level == "expectation"
    assert result.expectation_passed is True


def test_save_minimize_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="minimize", objective_threshold=1.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert not target.exists()


def test_save_expectation_fails_when_objective_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    no_obj = CpsatPythonResult(
        status="optimal",
        solution={"x": 1},
        objective=None,
        stdout='{"status":"optimal","objective":null,"solution":{"x":1}}',
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=5,
    )
    _patch_executor(monkeypatch, no_obj)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert result.reason is not None
    assert "objective" in result.reason


def test_save_reported_gate_fails_with_expectation_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, expectation=exp)

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation_passed is None
    assert result.checker is None
    assert not target.exists()


def test_save_reported_gate_fails_with_expectation_and_checker_supplied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=10.0)

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker="import sys; sys.exit(0)"
    )

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.reported_passed is False
    assert result.expectation_passed is None
    assert result.checker is None
    assert not target.exists()


def test_save_checker_timeout_ms_without_checker_raises() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms"):
        save_verified_cpsat_python(
            _SCRIPT,
            target_dir=Path("/tmp/x"),
            checker_timeout_ms=5000,
        )


def test_save_checker_timeout_ms_zero_raises() -> None:
    with pytest.raises(ValueError, match="positive"):
        save_verified_cpsat_python(
            _SCRIPT,
            target_dir=Path("/tmp/x"),
            checker="print('ok')",
            checker_timeout_ms=0,
        )


@pytest.mark.parametrize(
    "bad_checker",
    [pytest.param("", id="empty"), pytest.param("  \n", id="whitespace")],
)
def test_save_empty_or_whitespace_checker_raises(bad_checker: str) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        save_verified_cpsat_python(_SCRIPT, target_dir=Path("/tmp/x"), checker=bad_checker)


def test_save_expectation_gate_fails_with_checker_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=100.0)

    result = save_verified_cpsat_python(
        _SCRIPT,
        target_dir=target,
        expectation=exp,
        checker="import sys; sys.exit(0)",
    )

    # Expectation failed, so checker never ran — checker must be None, not an error report.
    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.expectation_passed is False
    assert result.checker is None
    assert not target.exists()


# --- Checker gate tests -----------------------------------------------------

_CHECKER_SOURCE = "# checker"


def _patch_checker(
    monkeypatch: pytest.MonkeyPatch,
    report: CpsatCheckerReport,
) -> None:
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda checker, run_result, *, problem, timeout_ms, tracker: report,
    )


def _accepted_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _rejected_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="rejected",
        errors=["golfer 3 appears twice"],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _error_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="error",
        errors=["checker crashed"],
        stdout="",
        stderr="error output",
        duration_ms=5,
        timed_out=False,
        truncated=False,
    )


def _timeout_report() -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status="timeout",
        errors=["checker timed out"],
        stdout="",
        stderr="",
        duration_ms=100,
        timed_out=True,
        truncated=False,
    )


def test_save_accepted_checker_saves_with_checked_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is True
    assert result.verification_level == "checked"
    assert result.checker is not None
    assert result.checker.status == "accepted"
    assert (target / CHECKER_FILENAME).is_file()
    assert (target / CHECKER_FILENAME).read_text() == _CHECKER_SOURCE
    assert (target / MANIFEST_FILENAME).is_file()
    assert (target / SOLUTION_FILENAME).is_file()
    assert json.loads((target / SOLUTION_FILENAME).read_text()) == _OPTIMAL_RESULT.solution


def test_save_accepted_checker_manifest_has_scalar_summary_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Accepted checker: manifest carries only scalar summary, never free-text fields."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    sensitive_report = CpsatCheckerReport(
        status="accepted",
        errors=[],
        stdout="/tmp/secret_path/output.txt: done",
        stderr="/tmp/secret_path/err.txt: ok",
        details={"path": "/tmp/secret_path/details.json"},
        duration_ms=42,
        timed_out=False,
        truncated=False,
    )
    _patch_checker(monkeypatch, sensitive_report)
    target = tmp_path / "s"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    manifest_text = (target / MANIFEST_FILENAME).read_text()
    manifest = json.loads(manifest_text)
    checker_summary = manifest["verification"]["checker"]

    assert set(checker_summary.keys()) == {
        "status",
        "error_count",
        "duration_ms",
        "timed_out",
        "truncated",
    }
    assert checker_summary["status"] == "accepted"
    assert checker_summary["error_count"] == 0
    assert checker_summary["duration_ms"] == 42
    assert checker_summary["timed_out"] is False
    assert checker_summary["truncated"] is False
    # No free-text leakage
    assert "secret_path" not in manifest_text
    assert "stdout" not in str(checker_summary)
    assert "stderr" not in str(checker_summary)
    assert "details" not in str(checker_summary)
    assert "errors" not in str(checker_summary)


@pytest.mark.parametrize(
    ("report_fn", "expected_checker_status"),
    [
        (_rejected_report, "rejected"),
        (_error_report, "error"),
        (_timeout_report, "timeout"),
    ],
)
def test_save_non_accepted_checker_does_not_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    report_fn: Callable[[], CpsatCheckerReport],
    expected_checker_status: str,
) -> None:
    report = report_fn()
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, report)
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.checker is not None
    assert result.checker.status == expected_checker_status
    assert not target.exists()


def test_save_non_accepted_checker_with_expectation_reports_expectation_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _rejected_report())
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=2.0)

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker=_CHECKER_SOURCE
    )

    assert result.saved is False
    assert result.verification_level == "expectation"
    assert result.checker is not None
    assert result.checker.status == "rejected"
    assert not target.exists()


def test_save_checker_not_run_when_reported_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When reported gate fails, checker is skipped (checker=None, not an error report)."""
    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    checker_called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda *a, **kw: checker_called.append(True) or _accepted_report(),
    )
    target = tmp_path / "s"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, checker=_CHECKER_SOURCE)

    assert result.saved is False
    assert result.verification_level == "none"
    assert result.checker is None
    assert not checker_called


def test_save_checker_not_run_when_expectation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When expectation gate fails, checker is skipped (checker=None)."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    checker_called = []
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.save.run_checker",
        lambda *a, **kw: checker_called.append(True) or _accepted_report(),
    )
    target = tmp_path / "s"
    exp = CpsatExpectation(objective_sense="maximize", objective_threshold=1000.0)

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, expectation=exp, checker=_CHECKER_SOURCE
    )

    assert result.saved is False
    assert result.verification_level == "reported"
    assert result.checker is None
    assert not checker_called


@pytest.mark.integration
def test_save_verified_cpsat_python_integration(tmp_path: Path) -> None:
    """Run a real script end-to-end and verify it saves."""
    examples = Path(__file__).parent.parent / "fixtures" / "cpsat_python"
    source = (examples / "assignment.py").read_text()
    target = tmp_path / "assignment_save"

    result = save_verified_cpsat_python(source, target_dir=target)

    assert result.saved is True
    assert (target / "model.py").is_file()
    assert (target / MANIFEST_FILENAME).is_file()


# --- seed replay -------------------------------------------------------------


_TIMEOUT_RESULT = CpsatPythonResult(
    status="timeout",
    solution={"x": 3},
    objective=3.0,
    stdout="",
    stderr="",
    return_code=None,
    timed_out=True,
    truncated=False,
    duration_ms=99,
)


def test_save_with_seed_reruns_with_seed_env_and_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7)

    assert result.saved is True
    assert captured["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    verification = manifest["verification"]
    assert verification["replay_seed"] == 7
    # The reproducibility note tells a manual re-runner to set the env var.
    assert "OPENCONSTRAINT_MCP_CPSAT_SEED" in verification["reproducibility_note"]


def test_save_without_seed_passes_no_env_and_omits_seed_from_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "unseeded"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert captured["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert "replay_seed" not in manifest["verification"]


def test_save_with_seed_timeout_winner_still_fails_reported_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _TIMEOUT_RESULT)
    target = tmp_path / "timeout_seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=7)

    # A timeout replay is unproven: replaying its seed does not make it savable.
    assert result.saved is False
    assert result.verification_level == "none"
    assert not target.exists()


def test_save_with_bool_seed_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    with pytest.raises(ValueError, match="seed must be a non-bool integer"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=True)


def test_save_with_negative_seed_reruns_with_seed_env_and_records_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "negative_seeded"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=-1)

    assert result.saved is True
    assert captured["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "-1",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["verification"]["replay_seed"] == -1


@pytest.mark.parametrize(
    "bad_seed",
    [pytest.param(-2_147_483_649, id="below"), pytest.param(2_147_483_648, id="above")],
)
def test_save_with_seed_outside_int32_range_is_rejected(
    bad_seed: int,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        save_verified_cpsat_python(_SCRIPT, target_dir=tmp_path / "x", seed=bad_seed)


# --- config replay ------------------------------------------------------------


def test_save_with_config_reruns_with_config_env_and_persists_replay_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        env = kw.get("env")
        assert env is not None
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])  # type: ignore[index]
        captured["contents"] = json.loads(config_path.read_text())
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "configured"

    result = save_verified_cpsat_python(_SCRIPT, target_dir=target, config={"num_workers": 2})

    assert result.saved is True
    assert captured["contents"] == {"num_workers": 2}
    assert (target / "replay-config.json").read_text() == json.dumps(
        {"num_workers": 2}, indent=2
    ) + "\n"

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert manifest["verification"]["replay_config_sha256"] == config_sha256({"num_workers": 2})


def test_save_without_config_passes_no_config_env_and_writes_no_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "unconfigured"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert captured["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert not (target / "replay-config.json").exists()
    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    assert "replay_config_sha256" not in manifest["verification"]


def test_save_with_empty_config_dict_equivalent_to_omitted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "empty_config"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, config={})

    assert captured["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert not (target / "replay-config.json").exists()


def test_save_with_seed_and_config_combine_in_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    def _spy(source: str, **kw: object) -> CpsatPythonResult:
        captured["env"] = kw.get("env")
        return _OPTIMAL_RESULT

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.run_cpsat_python", _spy)
    target = tmp_path / "seeded_and_configured"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, seed=5, config={"a": 1})

    env = captured["env"]
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "5"
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" in env


# --- experiment_result provenance ---------------------------------------------


def _winning_attempt_row(**overrides: object) -> CpsatPythonExperimentAttemptResult:
    defaults: dict[str, object] = {
        "index": 0,
        "name": "baseline",
        "seed": None,
        "config_sha256": None,
        "source_sha256": text_sha256(_SCRIPT),
        "script_timeout_ms": 5000,
        "status": "optimal",
        "objective": 3.0,
        "accepted": True,
        "checker_status": None,
        "message": None,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 42,
    }
    defaults.update(overrides)
    return CpsatPythonExperimentAttemptResult(**defaults)  # type: ignore[arg-type]


def _losing_attempt_row(**overrides: object) -> CpsatPythonExperimentAttemptResult:
    defaults: dict[str, object] = {
        "index": 1,
        "name": "worse",
        "seed": None,
        "config_sha256": None,
        "source_sha256": "some-other-hash",
        "script_timeout_ms": 5000,
        "status": "feasible",
        "objective": 1.0,
        "accepted": True,
        "checker_status": None,
        "message": None,
        "timed_out": False,
        "truncated": False,
        "duration_ms": 10,
    }
    defaults.update(overrides)
    return CpsatPythonExperimentAttemptResult(**defaults)  # type: ignore[arg-type]


def _experiment_result(
    *, winning_attempt: CpsatPythonExperimentAttemptResult, status: str = "winner"
) -> CpsatPythonExperimentResult:
    kwargs: dict[str, object] = {
        "status": status,
        "attempts": [winning_attempt, _losing_attempt_row()],
        "elapsed_ms": 100,
        "objective_sense": "maximize",
        "selection_policy": (
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        "source_sha256": [winning_attempt.source_sha256, "some-other-hash"],
        "checker_sha256": None,
        "problem_sha256": None,
    }
    if status == "winner":
        kwargs["winner_index"] = winning_attempt.index
        kwargs["winner_name"] = winning_attempt.name
        kwargs["winner"] = _OPTIMAL_RESULT
    return CpsatPythonExperimentResult(**kwargs)  # type: ignore[arg-type]


def test_save_with_matching_experiment_result_writes_experiment_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "with_experiment_log"
    experiment_result = _experiment_result(winning_attempt=_winning_attempt_row())

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, experiment_result=experiment_result
    )

    assert result.saved is True
    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert log["exploration_type"] == "cpsat_python_experiment"
    assert log["winner_index"] == 0
    assert log["winner_name"] == "baseline"
    assert len(log["attempts"]) == 2
    # Provenance summary only: hashes and scalars, never a full config object.
    for attempt in log["attempts"]:
        assert "config" not in attempt
        assert set(attempt) == {
            "index",
            "name",
            "seed",
            "source_sha256",
            "config_sha256",
            "used_script_path",
            "script_timeout_ms",
            "status",
            "objective",
            "accepted",
            "checker_status",
            "message",
            "timed_out",
            "truncated",
            "duration_ms",
        }

    manifest = json.loads((target / MANIFEST_FILENAME).read_text())
    exp_summary = manifest["verification"]["experiment_log"]
    assert exp_summary["winner_index"] == 0
    assert exp_summary["attempt_count"] == 2


def test_save_without_experiment_result_writes_no_experiment_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    target = tmp_path / "no_experiment_log"

    save_verified_cpsat_python(_SCRIPT, target_dir=target)

    assert not (target / EXPERIMENT_LOG_FILENAME).exists()


def test_save_rejects_no_winner_experiment_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result(
        winning_attempt=_winning_attempt_row(accepted=False), status="no_winner"
    )

    with pytest.raises(ValueError, match="status must be 'winner'"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []  # rejected before any re-run


def test_save_rejects_experiment_result_with_source_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result(
        winning_attempt=_winning_attempt_row(source_sha256="wrong-hash")
    )

    with pytest.raises(ValueError, match="source_sha256"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


def test_save_rejects_experiment_result_with_seed_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result(winning_attempt=_winning_attempt_row(seed=7))

    with pytest.raises(ValueError, match="seed"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


def test_save_rejects_experiment_result_config_supplied_but_winner_had_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result(winning_attempt=_winning_attempt_row(config_sha256=None))

    with pytest.raises(ValueError, match="config_sha256"):
        save_verified_cpsat_python(
            _SCRIPT,
            target_dir=tmp_path / "x",
            config={"unexpected": True},
            experiment_result=experiment_result,
        )
    assert called == []


def test_save_rejects_experiment_result_config_omitted_but_winner_used_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    winning_config_hash = config_sha256({"num_workers": 4})
    experiment_result = _experiment_result(
        winning_attempt=_winning_attempt_row(config_sha256=winning_config_hash)
    )

    with pytest.raises(ValueError, match="config_sha256"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


def test_save_accepts_experiment_result_config_matching_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    config = {"num_workers": 4}
    winning_config_hash = config_sha256(config)
    experiment_result = _experiment_result(
        winning_attempt=_winning_attempt_row(config_sha256=winning_config_hash)
    )
    target = tmp_path / "matching_config"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, config=config, experiment_result=experiment_result
    )

    assert result.saved is True
    assert (target / "replay-config.json").is_file()
    assert (target / EXPERIMENT_LOG_FILENAME).is_file()


def test_save_with_matching_experiment_result_still_fails_a_bad_fresh_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """experiment_result is provenance only — it never substitutes for verification.

    A self-consistent experiment_result claiming a winner is not enough: the
    fresh re-run below is mocked to return infeasible, so the save must still
    fail the reported gate and write nothing, exactly as it would with no
    experiment_result at all.
    """
    _patch_executor(monkeypatch, _INFEASIBLE_RESULT)
    experiment_result = _experiment_result(winning_attempt=_winning_attempt_row())
    target = tmp_path / "provenance_is_not_evidence"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, experiment_result=experiment_result
    )

    assert result.saved is False
    assert result.verification_level == "none"
    assert not target.exists()


def test_save_accepts_experiment_result_matching_a_non_winning_accepted_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    matching_but_not_winner = _losing_attempt_row(
        source_sha256=text_sha256(_SCRIPT), name="faster_alt"
    )
    experiment_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=_OPTIMAL_RESULT,
        attempts=[_winning_attempt_row(source_sha256="different-hash"), matching_but_not_winner],
        elapsed_ms=100,
        objective_sense="maximize",
        selection_policy=(
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        source_sha256=["different-hash", text_sha256(_SCRIPT)],
        checker_sha256=None,
        problem_sha256=None,
    )
    target = tmp_path / "save_non_winner"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, experiment_result=experiment_result
    )

    assert result.saved is True


def test_save_rejects_experiment_result_when_only_source_match_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A source-matching attempt that was rejected by the experiment cannot be provenance."""
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    not_accepted = _losing_attempt_row(
        source_sha256=text_sha256(_SCRIPT),
        name="tried_but_failed",
        accepted=False,
        status="infeasible",
    )
    experiment_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=_OPTIMAL_RESULT,
        attempts=[_winning_attempt_row(source_sha256="different-hash"), not_accepted],
        elapsed_ms=100,
        objective_sense="maximize",
        selection_policy=(
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        source_sha256=["different-hash", text_sha256(_SCRIPT)],
        checker_sha256=None,
        problem_sha256=None,
    )

    with pytest.raises(ValueError, match="was not accepted"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


def test_save_rejects_experiment_result_non_winner_accepted_attempt_seed_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The error must name the actual matching (non-winning) attempt, not the winner."""
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    matching_but_not_winner = _losing_attempt_row(
        source_sha256=text_sha256(_SCRIPT), name="faster_alt", seed=7
    )
    experiment_result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=_OPTIMAL_RESULT,
        attempts=[_winning_attempt_row(source_sha256="different-hash"), matching_but_not_winner],
        elapsed_ms=100,
        objective_sense="maximize",
        selection_policy=(
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        source_sha256=["different-hash", text_sha256(_SCRIPT)],
        checker_sha256=None,
        problem_sha256=None,
    )

    with pytest.raises(ValueError, match=r"attempt 'faster_alt' has seed \(7\)"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


# --- experiment_result provenance: script_path attempts -----------------------


def _experiment_result_over(
    attempts: list[CpsatPythonExperimentAttemptResult],
) -> CpsatPythonExperimentResult:
    """An experiment result whose winner is ``attempts[0]``, over arbitrary rows."""
    return CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name=attempts[0].name,
        winner=_OPTIMAL_RESULT,
        attempts=attempts,
        elapsed_ms=100,
        objective_sense="maximize",
        selection_policy=(
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        source_sha256=[a.source_sha256 for a in attempts],
        checker_sha256=None,
        problem_sha256=None,
    )


def test_save_rejects_experiment_result_when_every_match_used_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A script_path attempt is unreplayable provenance: its args/cwd are unavailable."""
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result_over(
        [
            _winning_attempt_row(name="from_file", used_script_path=True),
            _losing_attempt_row(name="unrelated"),
        ]
    )

    with pytest.raises(ValueError, match="ran via script_path"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "x", experiment_result=experiment_result
        )
    assert called == []


def test_source_mismatch_names_script_path_drift_when_every_attempt_ran_from_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pasted copy of a script_path attempt's file drifts by a byte; say so."""
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result_over(
        [
            _winning_attempt_row(
                name="from_file", used_script_path=True, source_sha256="on-disk-bytes"
            ),
            _losing_attempt_row(name="also_from_file", used_script_path=True),
        ]
    )

    with pytest.raises(ValueError, match="every attempt in this experiment ran via script_path"):
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "drift", experiment_result=experiment_result
        )
    assert called == []


def test_source_mismatch_omits_script_path_hint_when_an_inline_attempt_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With an inline attempt in the set, a mismatch really is the wrong script."""
    _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result_over(
        [
            _winning_attempt_row(
                name="from_file", used_script_path=True, source_sha256="on-disk-bytes"
            ),
            _losing_attempt_row(name="inline", used_script_path=False),
        ]
    )

    with pytest.raises(ValueError) as excinfo:
        save_verified_cpsat_python(
            _SCRIPT, target_dir=tmp_path / "mixed", experiment_result=experiment_result
        )
    assert "script_path" not in str(excinfo.value)


@pytest.mark.parametrize("script_path_first", [True, False])
def test_save_accepts_experiment_result_when_any_match_is_inline_regardless_of_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script_path_first: bool
) -> None:
    """One inline match is enough, wherever it sits in ``attempts``."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    from_file = _winning_attempt_row(index=0, name="from_file", used_script_path=True)
    inline = _winning_attempt_row(index=1, name="inline", used_script_path=False)
    rows = [from_file, inline] if script_path_first else [inline, from_file]
    target = tmp_path / f"mixed_{script_path_first}"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, experiment_result=_experiment_result_over(rows)
    )

    assert result.saved is True


def test_save_accepts_experiment_result_whose_only_match_is_inline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result_over(
        [
            _winning_attempt_row(name="inline", used_script_path=False),
            _losing_attempt_row(name="unrelated"),
        ]
    )
    target = tmp_path / "inline_only"

    result = save_verified_cpsat_python(
        _SCRIPT, target_dir=target, experiment_result=experiment_result
    )

    assert result.saved is True


def test_experiment_log_attempt_rows_record_used_script_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result_over(
        [
            _winning_attempt_row(index=0, name="inline", used_script_path=False),
            _winning_attempt_row(index=1, name="from_file", used_script_path=True),
        ]
    )
    target = tmp_path / "used_script_path_log"

    save_verified_cpsat_python(_SCRIPT, target_dir=target, experiment_result=experiment_result)

    log = json.loads((target / EXPERIMENT_LOG_FILENAME).read_text())
    assert {a["name"]: a["used_script_path"] for a in log["attempts"]} == {
        "inline": False,
        "from_file": True,
    }


# --- verify_only mode ---------------------------------------------------------


def _forbid_target_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any save-target validation or commit an immediate test failure."""

    def _no_validate(*args: object, **kwargs: object) -> Path:
        raise AssertionError("validate_save_target must not run in verify_only mode")

    def _no_commit(*args: object, **kwargs: object) -> object:
        raise AssertionError("commit_staged_dir must not run in verify_only mode")

    monkeypatch.setattr("openconstraint_mcp.pyexec.save.validate_save_target", _no_validate)
    monkeypatch.setattr("openconstraint_mcp.pyexec.save.commit_staged_dir", _no_commit)


def test_verify_only_without_target_dir_returns_passing_checked_verdict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())

    result = save_verified_cpsat_python(
        _SCRIPT,
        verify_only=True,
        expectation=CpsatExpectation(objective_sense="maximize", objective_threshold=2.0),
        checker=_CHECKER_SOURCE,
    )

    assert result.reason is None
    assert result.verification_level == "checked"
    assert result.reported_passed is True
    assert result.expectation_passed is True
    assert result.checker is not None and result.checker.status == "accepted"
    assert result.saved is False
    assert result.target_dir is None
    assert result.files == []


def test_verify_only_never_validates_target_or_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())
    _forbid_target_io(monkeypatch)

    result = save_verified_cpsat_python(_SCRIPT, verify_only=True, checker=_CHECKER_SOURCE)

    assert result.reason is None


def test_verify_only_ignores_a_target_dir_that_would_fail_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Deliberately UNSTUBBED: the real validate_save_target rejects a relative
    # path, so a passing verdict here proves the validator was never reached.
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)

    result = save_verified_cpsat_python(_SCRIPT, target_dir=Path("relative/path"), verify_only=True)

    assert result.reason is None
    assert result.verification_level == "reported"
    assert result.saved is False


def test_verify_only_writes_nothing_under_a_real_target_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem-level no-write proof, independent of any monkeypatched call site."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    _patch_checker(monkeypatch, _accepted_report())

    result = save_verified_cpsat_python(
        _SCRIPT,
        target_dir=tmp_path / "out",
        verify_only=True,
        checker=_CHECKER_SOURCE,
    )

    assert result.reason is None
    assert list(tmp_path.iterdir()) == []


def test_save_without_target_dir_raises_before_the_solver_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = _patch_executor_counting(monkeypatch, _OPTIMAL_RESULT)

    with pytest.raises(ValueError, match="target_dir is required unless verify_only"):
        save_verified_cpsat_python(_SCRIPT)

    assert called == [], "executor must not be called before the target check"


def test_missing_target_dir_is_checked_after_experiment_consistency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The target check sits behind all three eager validations, not in front of them."""
    _patch_executor(monkeypatch, _OPTIMAL_RESULT)
    experiment_result = _experiment_result(
        winning_attempt=_winning_attempt_row(accepted=False), status="no_winner"
    )

    with pytest.raises(ValueError, match="status must be 'winner'"):
        save_verified_cpsat_python(_SCRIPT, experiment_result=experiment_result)
