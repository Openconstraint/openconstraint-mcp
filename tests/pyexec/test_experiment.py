"""Unit tests for pyexec/experiment.py — run_cpsat_python and run_checker mocked."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from openconstraint_mcp.pyexec import experiment
from openconstraint_mcp.pyexec.experiment import (
    MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS,
    run_cpsat_python_experiment,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatCheckerReport,
    CpsatPythonExperimentAttempt,
    CpsatPythonResult,
)
from openconstraint_mcp.shared.hashing import path_sha256
from openconstraint_mcp.shared.save_target import text_sha256


def _result(
    *,
    status: str = "optimal",
    solution: dict | None = None,
    objective: float | int | None = 1.0,
    best_objective_bound: float | int | None = None,
    timed_out: bool = False,
    truncated: bool = False,
    stderr: str = "",
    duration_ms: int = 5,
    stdout: str = "",
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,  # type: ignore[arg-type]
        solution=solution if solution is not None else {"x": 1},
        objective=objective,
        best_objective_bound=best_objective_bound,
        stdout=stdout,
        stderr=stderr,
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=truncated,
        duration_ms=duration_ms,
    )


def _result_without_solution() -> CpsatPythonResult:
    return _result(objective=None).model_copy(update={"solution": None})


def _checker_report(status: str = "accepted") -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=[],
        stdout="",
        stderr="",
        duration_ms=1,
        timed_out=False,
        truncated=False,
    )


def _patch_runner(
    monkeypatch: pytest.MonkeyPatch,
    results_by_source: dict[str, CpsatPythonResult],
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        if calls is not None:
            calls.append(
                {
                    "source": source,
                    "env": env,
                    "tracker": tracker,
                    "script_timeout_ms": script_timeout_ms,
                }
            )
        return results_by_source[source]

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)


def _patch_checker(
    monkeypatch: pytest.MonkeyPatch,
    status_by_source: dict[str, str] | str = "accepted",
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(
        *, checker: str, run_result: CpsatPythonResult, problem: Any, timeout_ms: int, tracker: Any
    ) -> CpsatCheckerReport:
        if calls is not None:
            calls.append(
                {
                    "checker": checker,
                    "run_result": run_result,
                    "problem": problem,
                    "timeout_ms": timeout_ms,
                    "tracker": tracker,
                }
            )
        if isinstance(status_by_source, str):
            return _checker_report(status_by_source)
        marker = run_result.solution.get("marker") if run_result.solution else None
        return _checker_report(status_by_source.get(marker, "accepted"))  # type: ignore[arg-type]

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_checker", _fake)


def _attempt(source: str, **kwargs: Any) -> CpsatPythonExperimentAttempt:
    return CpsatPythonExperimentAttempt(source=source, **kwargs)


# --- winner selection --------------------------------------------------------


def test_minimize_picks_smallest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=10), "b": _result(objective=3), "c": _result(objective=7)},
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="minimize"
    )

    assert result.status == "winner"
    assert result.winner_index == 1
    assert result.winner_name == "attempt-1"
    assert result.winner.objective == 3  # type: ignore[union-attr]


def test_maximize_picks_largest_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=10), "b": _result(objective=3), "c": _result(objective=7)},
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="maximize"
    )

    assert result.winner_index == 0
    assert result.winner.objective == 10  # type: ignore[union-attr]


def test_feasibility_accepts_solution_without_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")])

    assert result.status == "winner"
    assert result.objective_sense is None
    assert result.winner.objective is None  # type: ignore[union-attr]
    assert result.attempts[0].accepted is True


@pytest.mark.parametrize(
    "run_result",
    [_result_without_solution(), _result(solution={}, objective=None)],
    ids=["missing", "empty"],
)
def test_feasibility_rejects_missing_or_empty_solution(
    monkeypatch: pytest.MonkeyPatch, run_result: CpsatPythonResult
) -> None:
    _patch_runner(monkeypatch, {"a": run_result})

    result = run_cpsat_python_experiment([_attempt("a")])

    assert result.status == "no_winner"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].message == "solution is missing or empty"


def test_feasibility_tie_break_prefers_status_then_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="feasible", objective=None),
            "b": _result(status="optimal", objective=None),
            "c": _result(status="optimal", objective=None),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b"), _attempt("c")])

    assert result.winner_index == 1
    assert result.selection_policy == "accepted_status_then_duration_then_attempt_order"


def test_tie_break_prefers_stronger_status_then_attempt_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="timeout", objective=5, timed_out=True),
            "b": _result(status="optimal", objective=5),
            "c": _result(status="feasible", objective=5),
        },
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")], objective_sense="minimize"
    )

    assert result.winner_index == 1  # the optimal one, despite later attempt order


def test_tie_break_by_attempt_order_when_status_equal(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="feasible", objective=5),
            "b": _result(status="feasible", objective=5),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.winner_index == 0


def test_feasibility_tie_break_prefers_faster_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="optimal", objective=None, duration_ms=50),
            "b": _result(status="optimal", objective=None, duration_ms=10),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")])

    assert result.winner_index == 1  # faster attempt wins despite later attempt order


def test_tie_break_prefers_faster_duration_when_status_and_objective_equal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="feasible", objective=5, duration_ms=50),
            "b": _result(status="feasible", objective=5, duration_ms=10),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.winner_index == 1  # faster attempt wins despite later attempt order


def test_winner_name_uses_explicit_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})

    result = run_cpsat_python_experiment(
        [_attempt("a", name="baseline")], objective_sense="minimize"
    )

    assert result.winner_name == "baseline"
    assert result.attempts[0].name == "baseline"


def test_unnamed_attempt_defaults_to_attempt_index(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3), "b": _result(objective=9)})

    result = run_cpsat_python_experiment(
        [_attempt("a", name="baseline"), _attempt("b")], objective_sense="minimize"
    )

    assert result.attempts[1].name == "attempt-1"


# --- include_winner_stdout ----------------------------------------------------


def test_include_winner_stdout_false_suppresses_winner_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_stdout = '{"x": 1}'
    _patch_runner(monkeypatch, {"a": _result(objective=3, stdout=raw_stdout)})

    result = run_cpsat_python_experiment(
        [_attempt("a")], objective_sense="minimize", include_winner_stdout=False
    )

    assert result.winner is not None
    assert result.winner.stdout == experiment._WINNER_STDOUT_OMITTED_SENTINEL
    assert result.winner.solution == {"x": 1}
    assert result.winner.objective == 3


@pytest.mark.parametrize("include_winner_stdout_kwargs", [{}, {"include_winner_stdout": True}])
def test_include_winner_stdout_default_or_explicit_true_keeps_stdout(
    monkeypatch: pytest.MonkeyPatch, include_winner_stdout_kwargs: dict[str, Any]
) -> None:
    raw_stdout = '{"x": 1}'
    _patch_runner(monkeypatch, {"a": _result(objective=3, stdout=raw_stdout)})

    result = run_cpsat_python_experiment(
        [_attempt("a")], objective_sense="minimize", **include_winner_stdout_kwargs
    )

    assert result.winner is not None
    assert result.winner.stdout == raw_stdout


def test_include_winner_stdout_false_with_no_winner_is_a_no_op(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="infeasible", solution=None, objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")], include_winner_stdout=False)

    assert result.status == "no_winner"
    assert result.winner is None


# --- name uniqueness ----------------------------------------------------------


def test_duplicate_explicit_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attempt name"):
        run_cpsat_python_experiment(
            [_attempt("a", name="x"), _attempt("b", name="x")], objective_sense="minimize"
        )


def test_explicit_name_colliding_with_defaulted_label_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate attempt name"):
        run_cpsat_python_experiment(
            [_attempt("a", name="attempt-1"), _attempt("b")], objective_sense="minimize"
        )


# --- partial failures ----------------------------------------------------------


def test_failed_attempts_recorded_but_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="error", solution=None, objective=None),
            "b": _result(status="optimal", solution={}, objective=4),  # empty solution
            "c": _result(status="optimal", objective=9),
        },
    )

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")],
        objective_sense="minimize",
        default_script_timeout_ms=1000,
    )

    assert result.winner_index == 2
    assert len(result.attempts) == 3
    rejected = {a.name for a in result.attempts if not a.accepted}
    assert rejected == {"attempt-0", "attempt-1"}


def test_errored_attempt_message_includes_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    traceback = 'Traceback (most recent call last):\n  File "script.py", line 1\nValueError: boom'
    _patch_runner(
        monkeypatch,
        {"a": _result(status="error", solution=None, objective=None, stderr=traceback)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].message is not None
    assert result.attempts[0].message.startswith("status='error':")
    assert "ValueError: boom" in result.attempts[0].message


def test_errored_attempt_with_empty_stderr_keeps_bare_status_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch, {"a": _result(status="error", solution=None, objective=None, stderr="")}
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].message == "status='error'"


def test_stderr_snippet_preserves_exception_type_prefix_on_long_line() -> None:
    long_message = "x" * 600
    stderr = f"Traceback (most recent call last):\nValueError: {long_message}"

    snippet = experiment._stderr_snippet(stderr)

    assert snippet is not None
    assert "ValueError:" in snippet
    # The prefix must survive truncation, not just the tail of the long line.
    assert snippet.index("ValueError:") < 50
    assert len(snippet) < len(long_message)


def test_errored_attempt_message_is_single_line_for_multiline_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traceback = 'Traceback (most recent call last):\n  File "script.py", line 1\nValueError: boom'
    _patch_runner(
        monkeypatch,
        {"a": _result(status="error", solution=None, objective=None, stderr=traceback)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].message is not None
    assert "\n" not in result.attempts[0].message


def test_errored_attempt_stderr_tail_matches_full_stderr_when_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traceback = 'Traceback (most recent call last):\n  File "script.py", line 1\nValueError: boom'
    _patch_runner(
        monkeypatch,
        {"a": _result(status="error", solution=None, objective=None, stderr=traceback)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].stderr_tail == traceback


def test_errored_attempt_with_empty_stderr_has_no_stderr_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch, {"a": _result(status="error", solution=None, objective=None, stderr="")}
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].stderr_tail is None


def test_non_error_status_never_populates_stderr_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="optimal", stderr="noisy diagnostic output"),
            "b": _result(status="timeout", timed_out=True, stderr="noisy diagnostic output"),
        },
    )

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.attempts[0].status == "optimal"
    assert result.attempts[0].stderr_tail is None
    assert result.attempts[1].status == "timeout"
    assert result.attempts[1].stderr_tail is None


def test_errored_attempt_stderr_tail_is_truncated_tail_not_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = "UNIQUE_TAIL_MARKER_END"
    long_stderr = ("x" * (experiment._ATTEMPT_STDERR_TAIL_MAX_CHARS + 500)) + marker
    _patch_runner(
        monkeypatch,
        {"a": _result(status="error", solution=None, objective=None, stderr=long_stderr)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    tail = result.attempts[0].stderr_tail
    assert tail is not None
    assert len(tail) == experiment._ATTEMPT_STDERR_TAIL_MAX_CHARS
    assert tail == long_stderr[-experiment._ATTEMPT_STDERR_TAIL_MAX_CHARS :]
    assert marker in tail


def test_optimization_rejects_missing_objective_with_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="optimal", solution={"x": 1}, objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].message == "objective is missing or non-numeric"


def test_timeout_with_recovered_solution_can_win(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="timeout", objective=2, timed_out=True)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "winner"
    assert result.winner.status == "timeout"  # type: ignore[union-attr]


def test_all_rejected_yields_no_winner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(status="infeasible", solution=None, objective=None)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.winner_index is None
    assert result.winner_name is None
    assert result.winner is None


# An "unknown" attempt carries no objective/solution, but best_objective_bound is
# still diagnostically useful — the whole point of the field — and must survive
# onto the attempt row even though the attempt is rejected (not accepted).
def test_unknown_attempt_row_carries_best_objective_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(status="unknown", solution=None, objective=None, best_objective_bound=7)},
    )

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.attempts[0].accepted is False
    assert result.attempts[0].best_objective_bound == 7


# --- seed env propagation ------------------------------------------------------


def test_seed_forwarded_as_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment([_attempt("a", seed=7)], objective_sense="minimize")

    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": "7",
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_no_seed_no_config_explicitly_clears_both_protocol_vars(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    # Both keys are present but explicitly None, not omitted — this is what tells
    # execute_child to clear any stale value the server process inherited, rather
    # than passing env=None (full, unfiltered inheritance).
    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }


def test_bool_seed_rejected_at_attempt_construction() -> None:
    # CpsatPythonExperimentAttempt.seed is StrictInt, so a bool is rejected by
    # pydantic before it ever reaches the orchestrator's own range check.
    with pytest.raises(ValidationError, match="int_type"):
        _attempt("a", seed=True)


def test_seed_out_of_range_rejected() -> None:
    with pytest.raises(ValueError, match="CP-SAT random_seed range"):
        run_cpsat_python_experiment([_attempt("a", seed=2_147_483_648)], objective_sense="minimize")


# --- config env + temp-file injection -----------------------------------------


def test_config_written_to_temp_file_and_env_points_at_it(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a", config={"num_workers": 2})], objective_sense="minimize"
    )

    env = calls[0]["env"]
    config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
    # The file existed while run_cpsat_python was invoked; by the time this
    # assertion runs (after the call returns) the TemporaryDirectory is already
    # cleaned up, proving the config file does not outlive its attempt.
    assert not config_path.exists()
    # Key is present but explicitly None (clears any stale inherited value),
    # not omitted from the overlay.
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] is None


def test_config_file_contents_are_the_supplied_json(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
        captured["contents"] = json.loads(config_path.read_text())
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment(
        [_attempt("a", config={"restart_strategy": "luby"})], objective_sense="minimize"
    )

    assert captured["contents"] == {"restart_strategy": "luby"}


def test_config_temp_file_cleaned_up_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        config_path = Path(env["OPENCONSTRAINT_MCP_CPSAT_CONFIG"])
        captured["existed_during_call"] = config_path.exists()
        captured["path"] = config_path
        return _result(status="timeout", timed_out=True)

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment([_attempt("a", config={"x": 1})], objective_sense="minimize")

    assert captured["existed_during_call"] is True
    assert not captured["path"].exists()


def test_empty_config_dict_normalizes_to_no_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    result = run_cpsat_python_experiment([_attempt("a", config={})], objective_sense="minimize")

    assert calls[0]["env"] == {
        "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
        "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
    }
    assert result.attempts[0].config_sha256 is None


def test_seed_and_config_combine_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a", seed=3, config={"k": "v"})], objective_sense="minimize"
    )

    env = calls[0]["env"]
    assert env["OPENCONSTRAINT_MCP_CPSAT_SEED"] == "3"
    assert "OPENCONSTRAINT_MCP_CPSAT_CONFIG" in env


# --- canonical config hashing --------------------------------------------------


def test_config_hash_independent_of_key_order(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(), "b": _result()})

    result = run_cpsat_python_experiment(
        [
            _attempt("a", config={"a": 1, "b": 2}),
            _attempt("b", config={"b": 2, "a": 1}),
        ],
        objective_sense="minimize",
    )

    hash_a = result.attempts[0].config_sha256
    hash_b = result.attempts[1].config_sha256
    assert hash_a is not None
    assert hash_a == hash_b


def test_different_config_values_hash_differently(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(), "b": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("a", config={"k": 1}), _attempt("b", config={"k": 2})],
        objective_sense="minimize",
    )

    assert result.attempts[0].config_sha256 != result.attempts[1].config_sha256


def test_oversized_config_rejected() -> None:
    huge_config = {"blob": "x" * (experiment.MAX_EXPERIMENT_CONFIG_BYTES + 1)}
    with pytest.raises(ValueError, match="MAX_EXPERIMENT_CONFIG_BYTES"):
        run_cpsat_python_experiment([_attempt("a", config=huge_config)], objective_sense="minimize")


# --- checker gate ---------------------------------------------------------------


def test_checker_rejection_removes_best_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="optimal", solution={"marker": "a"}, objective=2),
            "b": _result(status="optimal", solution={"marker": "b"}, objective=9),
        },
    )
    _patch_checker(monkeypatch, status_by_source={"a": "rejected", "b": "accepted"})

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1000,
    )

    assert result.winner_index == 1
    rejected_attempt = result.attempts[0]
    assert rejected_attempt.accepted is False
    assert rejected_attempt.checker_status == "rejected"


def test_checker_runs_only_on_base_eligible_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(
        monkeypatch,
        {
            "a": _result(status="error", solution=None, objective=None),
            "b": _result(status="optimal", objective=3),
        },
    )
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    result = run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1000,
    )

    assert len(calls) == 1
    assert result.winner_index == 1
    assert result.attempts[0].checker_status is None


def test_problem_reaches_checker_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        problem="my problem",
    )

    assert calls[0]["problem"] == "my problem"


def test_unsupplied_checker_timeout_resolves_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1234,
    )

    assert calls[0]["timeout_ms"] == 1234


def test_unsupplied_checker_timeout_uses_attempt_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(
        monkeypatch,
        {"a": _result(objective=3), "b": _result(objective=4)},
    )
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b", script_timeout_ms=321)],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1234,
    )

    assert [call["timeout_ms"] for call in calls] == [1234, 321]


def test_supplied_checker_timeout_used_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    calls: list[dict[str, Any]] = []
    _patch_checker(monkeypatch, "accepted", calls=calls)

    run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1234,
        checker_timeout_ms=999,
    )

    assert calls[0]["timeout_ms"] == 999


# --- per-attempt timeout override -----------------------------------------------


def test_per_attempt_timeout_overrides_default(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=calls)

    result = run_cpsat_python_experiment(
        [_attempt("a", script_timeout_ms=1500)],
        objective_sense="minimize",
        default_script_timeout_ms=5000,
    )

    assert calls[0]["script_timeout_ms"] == 1500
    assert result.attempts[0].script_timeout_ms == 1500


# --- parallel scheduling ---------------------------------------------------------


def test_results_are_ordered_by_attempt_order_not_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        order.append(source)
        # "slow" finishes after "fast" despite running first, to prove that
        # result ordering follows attempt (input) order, not completion order.
        if source == "slow":
            return _result(objective=1)
        return _result(objective=2)

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    result = run_cpsat_python_experiment(
        [_attempt("slow"), _attempt("fast")],
        objective_sense="minimize",
        max_parallel_attempts=2,
    )

    assert [a.name for a in result.attempts] == ["attempt-0", "attempt-1"]
    assert result.attempts[0].objective == 1
    assert result.attempts[1].objective == 2


def test_max_parallel_attempts_runs_concurrently(monkeypatch: pytest.MonkeyPatch) -> None:
    concurrent_count = 0
    max_seen = 0
    lock = threading.Lock()

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal concurrent_count, max_seen
        with lock:
            concurrent_count += 1
            max_seen = max(max_seen, concurrent_count)
        time.sleep(0.05)
        with lock:
            concurrent_count -= 1
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b"), _attempt("c")],
        objective_sense="minimize",
        max_parallel_attempts=3,
    )

    assert max_seen >= 2


def test_max_parallel_attempts_defaults_to_serial(monkeypatch: pytest.MonkeyPatch) -> None:
    concurrent_count = 0
    max_seen = 0
    lock = threading.Lock()

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal concurrent_count, max_seen
        with lock:
            concurrent_count += 1
            max_seen = max(max_seen, concurrent_count)
        time.sleep(0.02)
        with lock:
            concurrent_count -= 1
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert max_seen == 1


def test_max_parallel_attempts_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    with pytest.raises(ValueError, match="exceeds the server cap"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=5
        )


def test_max_parallel_attempts_non_positive_rejected() -> None:
    with pytest.raises(ValueError, match="max_parallel_attempts must be >= 1"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=0
        )


def test_max_parallel_attempts_bool_rejected() -> None:
    with pytest.raises(ValueError, match="non-bool positive integer"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", max_parallel_attempts=True
        )


# --- admission wall-clock budget ------------------------------------------------


def test_projected_budget_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        run_cpsat_python_experiment(
            [_attempt("a", script_timeout_ms=MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS)],
            objective_sense="minimize",
        )

    message = str(exc_info.value)
    for field in (
        "attempt_count=1",
        "max_parallel_attempts=",
        "batches=",
        "script_timeout_ms=",
        "checker_timeout_ms=",
        "attempt_budget_ms=",
        "checker_budget_ms=",
        "overhead_ms=",
        "total_budget_ms=",
        "max_budget_ms=",
    ):
        assert field in message, f"missing {field!r} in: {message}"
    # A single attempt this slow already exceeds the cap by itself; no
    # attempt-count/parallelism change can fit it.
    assert "use run_cpsat_python instead" in message


def test_budget_rejection_reuses_overhead_from_breakdown_without_recomputing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rejection message's overhead_ms must come from the already-computed
    attempt budget breakdown, not a second call to cpsat_child_timeout_overhead_ms()."""
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    original_overhead = experiment.cpsat_child_timeout_overhead_ms()
    calls = 0
    real = experiment.cpsat_child_timeout_overhead_ms

    def _counting() -> int:
        nonlocal calls
        calls += 1
        return real()

    monkeypatch.setattr(experiment, "cpsat_child_timeout_overhead_ms", _counting)

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        run_cpsat_python_experiment(
            [_attempt("a", script_timeout_ms=MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS)],
            objective_sense="minimize",
        )

    assert calls == 1, "overhead should be computed once (in the breakdown), not re-derived"
    assert f"overhead_ms={original_overhead}" in str(exc_info.value)


def test_budget_rejection_from_batching_hints_at_concrete_fit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When only the batching (not any single attempt) is over budget, the hint
    gives concrete single-lever fit values instead of a bare "adjust these
    knobs" suggestion — a caller can act on them without inverting the budget
    formula by hand."""
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    per_attempt_timeout = (MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // 2) - 1000
    attempts = [_attempt("a", script_timeout_ms=per_attempt_timeout) for _ in range(3)]

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        experiment._check_wall_clock_budget(
            attempts,
            default_script_timeout_ms=30_000,
            max_parallel_attempts=1,
            checker_present=False,
            checker_timeout_ms=None,
        )

    message = str(exc_info.value)
    assert "attempt_count=3" in message
    # attempts=3, max_parallel_attempts=1, per-attempt total_ms=110250 (104000 +
    # 6000 process-tree grace + 250 executor slack) => batches_max=1,
    # max_attempts_to_fit=1*1=1, min_parallel_to_fit=ceil(3/1)=3,
    # max_slowest_total_ms=210000//3=70000.
    assert "reduce attempt count to <= 1" in message
    assert "increase max_parallel_attempts to >= 3" in message
    assert (
        "reduce the slowest attempt's script_timeout_ms + overhead + checker budget "
        "to <= 70000 ms total" in message
    )
    assert "exceeds this machine's max_parallel_attempts cap" not in message
    assert "use run_cpsat_python instead" not in message


def test_budget_rejection_hint_flags_unfittable_parallelism_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When even the min parallelism needed to fit exceeds this machine's own
    cap, the hint says so instead of silently suggesting an unreachable value."""
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 2)
    per_attempt_timeout = (MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // 2) - 1000
    attempts = [_attempt("a", script_timeout_ms=per_attempt_timeout) for _ in range(3)]

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS") as exc_info:
        experiment._check_wall_clock_budget(
            attempts,
            default_script_timeout_ms=30_000,
            max_parallel_attempts=1,
            checker_present=False,
            checker_timeout_ms=None,
        )

    message = str(exc_info.value)
    assert "increase max_parallel_attempts to >= 3" in message
    assert "exceeds this machine's max_parallel_attempts cap of 2" in message


def test_budget_gate_fires_before_any_child_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS"):
        run_cpsat_python_experiment(
            [_attempt("a", script_timeout_ms=MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS)],
            objective_sense="minimize",
        )

    assert called is False


def test_projected_budget_exactly_at_cap_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    # A single attempt whose projected total lands exactly on the cap is
    # admitted (the gate uses <=, not <).
    overhead = experiment.cpsat_child_timeout_overhead_ms()
    script_timeout_ms = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS - overhead
    _patch_runner(monkeypatch, {"a": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("a", script_timeout_ms=script_timeout_ms)], objective_sense="minimize"
    )

    assert result.status == "winner"


def test_projected_budget_one_ms_over_cap_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    # One ms past the same boundary is rejected before any child runs.
    overhead = experiment.cpsat_child_timeout_overhead_ms()
    script_timeout_ms = MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS - overhead + 1
    called = False

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS"):
        run_cpsat_python_experiment(
            [_attempt("a", script_timeout_ms=script_timeout_ms)], objective_sense="minimize"
        )

    assert called is False


def test_higher_max_parallel_attempts_can_fit_a_budget_that_serial_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment, "_max_parallel_attempts_cap", lambda: 4)
    per_attempt_timeout = (MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS // 2) - 1000
    attempts = [_attempt("a", script_timeout_ms=per_attempt_timeout) for _ in range(3)]

    # Serial (implicit batches=3) is over budget...
    with pytest.raises(ValueError, match="MAX_CPSAT_EXPERIMENT_WALL_CLOCK_MS"):
        experiment._check_wall_clock_budget(
            attempts,
            default_script_timeout_ms=30_000,
            max_parallel_attempts=1,
            checker_present=False,
            checker_timeout_ms=None,
        )
    # ...but with 3-way parallelism (batches=1) it fits.
    experiment._check_wall_clock_budget(
        attempts,
        default_script_timeout_ms=30_000,
        max_parallel_attempts=3,
        checker_present=False,
        checker_timeout_ms=None,
    )


# --- other validation ------------------------------------------------------------


def test_empty_attempts_rejected() -> None:
    with pytest.raises(ValueError, match="attempts must not be empty"):
        run_cpsat_python_experiment([], objective_sense="minimize")


def test_empty_source_rejected() -> None:
    with pytest.raises(ValueError, match="source must be non-empty"):
        run_cpsat_python_experiment([_attempt("   ")], objective_sense="minimize")


def test_non_positive_attempt_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="script_timeout_ms must be positive"):
        run_cpsat_python_experiment(
            [_attempt("a", script_timeout_ms=0)], objective_sense="minimize"
        )


def test_non_positive_default_timeout_rejected() -> None:
    with pytest.raises(ValueError, match="default_script_timeout_ms must be positive"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", default_script_timeout_ms=0
        )


def test_checker_timeout_without_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker_timeout_ms supplied without checker"):
        run_cpsat_python_experiment(
            [_attempt("a")], objective_sense="minimize", checker_timeout_ms=100
        )


def test_whitespace_only_checker_rejected() -> None:
    with pytest.raises(ValueError, match="checker must be non-empty"):
        run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize", checker="   ")


def test_invalid_objective_sense_rejected_before_running(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def _fake(source: str, *, script_timeout_ms: int, tracker: Any = None, env: Any = None) -> Any:
        nonlocal called
        called = True
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python", _fake)

    with pytest.raises(ValueError, match="objective_sense"):
        run_cpsat_python_experiment(
            [_attempt("a")],
            objective_sense="max",  # type: ignore[arg-type]
        )

    assert not called


# --- source hashing ---------------------------------------------------------------


def test_source_sha256_index_aligned_with_attempts(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"aaa": _result(), "bbb": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("aaa"), _attempt("bbb")], objective_sense="minimize"
    )

    assert result.source_sha256 == [
        result.attempts[0].source_sha256,
        result.attempts[1].source_sha256,
    ]
    assert result.attempts[0].source_sha256 != result.attempts[1].source_sha256


def test_checker_and_problem_hashes_present_when_supplied(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})
    _patch_checker(monkeypatch, "accepted")

    result = run_cpsat_python_experiment(
        [_attempt("a")],
        objective_sense="minimize",
        checker="print('x')",
        problem="my problem",
    )

    assert result.checker_sha256 is not None
    assert result.problem_sha256 is not None


def test_checker_and_problem_hashes_none_when_omitted(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.checker_sha256 is None
    assert result.problem_sha256 is None


# --- tracker forwarding -----------------------------------------------------------


def test_tracker_forwarded_to_runner_and_checker(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = object()
    run_calls: list[dict[str, Any]] = []
    checker_calls: list[dict[str, Any]] = []
    _patch_runner(
        monkeypatch, {"a": _result(objective=3), "b": _result(objective=4)}, calls=run_calls
    )
    _patch_checker(monkeypatch, "accepted", calls=checker_calls)

    run_cpsat_python_experiment(
        [_attempt("a"), _attempt("b")],
        objective_sense="minimize",
        checker="print('x')",
        default_script_timeout_ms=1000,
        tracker=sentinel,  # type: ignore[arg-type]
    )

    assert all(c["tracker"] is sentinel for c in run_calls)
    assert all(c["tracker"] is sentinel for c in checker_calls)


# --- oversubscription warning -----------------------------------------------------


def test_oversubscription_warning_none_when_no_attempt_sets_num_workers() -> None:
    attempts = [_attempt("a"), _attempt("b", config={"other": 1})]
    names = ["a", "b"]

    assert experiment._oversubscription_warning(attempts, names, 4) is None


def test_oversubscription_warning_none_at_or_below_cpu_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment.os, "cpu_count", lambda: 4)
    attempts = [_attempt("a", config={"num_workers": 2})]
    names = ["a"]

    assert experiment._oversubscription_warning(attempts, names, 2) is None


def test_oversubscription_warning_flags_offending_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment.os, "cpu_count", lambda: 4)
    attempts = [_attempt("a", config={"num_workers": 8})]
    names = ["a"]

    warning = experiment._oversubscription_warning(attempts, names, 2)

    assert warning is not None
    assert "'a'" in warning
    assert "num_workers=8" in warning


@pytest.mark.parametrize(
    "ignored_num_workers",
    [pytest.param(True, id="bool"), pytest.param("eight", id="non-int")],
)
def test_oversubscription_warning_ignores_non_int_num_workers(
    ignored_num_workers: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment.os, "cpu_count", lambda: 1)
    attempts = [_attempt("a", config={"num_workers": ignored_num_workers})]
    names = ["a"]

    # Would otherwise trip (max_parallel_attempts=4 * num_workers=1 > cpu_count=1)
    # if the value were mistakenly treated as an int.
    assert experiment._oversubscription_warning(attempts, names, 4) is None


def test_oversubscription_warning_names_all_offenders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment.os, "cpu_count", lambda: 4)
    attempts = [
        _attempt("a", config={"num_workers": 8}),
        _attempt("b", config={"num_workers": 6}),
        _attempt("c", config={"num_workers": 1}),
    ]
    names = ["a", "b", "c"]

    warning = experiment._oversubscription_warning(attempts, names, 2)

    assert warning is not None
    assert "'a'" in warning
    assert "num_workers=8" in warning
    assert "'b'" in warning
    assert "num_workers=6" in warning
    assert "'c'" not in warning


def test_run_cpsat_python_experiment_warnings_populated_when_oversubscribed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(experiment.os, "cpu_count", lambda: 4)
    _patch_runner(monkeypatch, {"a": _result()})

    result = run_cpsat_python_experiment(
        [_attempt("a", config={"num_workers": 8})],
        objective_sense="minimize",
        max_parallel_attempts=2,
    )

    assert len(result.warnings) == 2
    assert "num_workers=8" in result.warnings[0]
    assert result.warnings[1] == experiment._REPRODUCIBILITY_WARNING


def test_run_cpsat_python_experiment_warnings_only_reproducibility_when_no_num_workers_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result()})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.warnings == [experiment._REPRODUCIBILITY_WARNING]


def test_reproducibility_warning_states_the_cause_and_the_remedy() -> None:
    # The tool description used to restate all of this; it now points here
    # instead, so this warning is the only carrier. Identity assertions above
    # prove it is ATTACHED — this one proves it still says something a client
    # can act on: why a winner may not replay, and what to set for a stronger
    # one.
    warning = experiment._REPRODUCIBILITY_WARNING

    assert "one observed run, not a reproducibility guarantee" in warning
    assert "save_verified_cpsat_python re-runs this script fresh" in warning
    assert "random_seed" in warning
    assert "num_workers = 1" in warning


def test_run_cpsat_python_experiment_warnings_empty_when_no_winner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result_without_solution()})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.status == "no_winner"
    assert result.warnings == []


# --- script_path attempts ----------------------------------------------------

_SCRIPT_BODY = "print('{}')\n"


def _patch_file_runner(
    monkeypatch: pytest.MonkeyPatch,
    result: CpsatPythonResult | None = None,
    calls: list[dict[str, Any]] | None = None,
) -> None:
    def _fake(
        script_path: Path,
        *,
        script_timeout_ms: int,
        args: list[str] | None = None,
        tracker: Any = None,
        env: Any = None,
    ) -> CpsatPythonResult:
        if calls is not None:
            calls.append(
                {
                    "script_path": script_path,
                    "args": args,
                    "script_timeout_ms": script_timeout_ms,
                    "tracker": tracker,
                    "env": env,
                }
            )
        return result if result is not None else _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python_file", _fake)


def _write_script(tmp_path: Path, name: str = "model.py", body: str | None = None) -> Path:
    path = tmp_path / name
    path.write_text(body if body is not None else _SCRIPT_BODY.format(name), encoding="utf-8")
    return path


def test_attempt_with_neither_source_nor_script_path_rejected() -> None:
    with pytest.raises(ValueError, match=r"attempts\[0\] must set exactly one of source"):
        run_cpsat_python_experiment([CpsatPythonExperimentAttempt()])


def test_attempt_with_both_source_and_script_path_rejected(tmp_path: Path) -> None:
    script = _write_script(tmp_path)
    attempt = CpsatPythonExperimentAttempt(source="print('hi')", script_path=str(script))

    with pytest.raises(ValueError, match=r"attempts\[0\] must set exactly one of source"):
        run_cpsat_python_experiment([attempt])


def test_empty_source_alongside_script_path_is_the_both_set_rejection(tmp_path: Path) -> None:
    # Presence is `is not None`: an explicitly supplied source="" must NOT read
    # as "only script_path was set" and silently discard the source parameter.
    script = _write_script(tmp_path)
    attempt = CpsatPythonExperimentAttempt(source="", script_path=str(script))

    with pytest.raises(ValueError, match=r"attempts\[0\] must set exactly one of source"):
        run_cpsat_python_experiment([attempt])


def test_args_without_script_path_rejected(tmp_path: Path) -> None:
    attempt = CpsatPythonExperimentAttempt(source="print('hi')", args=["--seed", "7"])

    with pytest.raises(ValueError, match=r"attempts\[0\]\.args supplied without script_path"):
        run_cpsat_python_experiment([attempt])


def test_empty_args_list_alongside_source_is_still_rejected() -> None:
    # Presence is `is not None`: args=[] must NOT read as "args omitted".
    attempt = CpsatPythonExperimentAttempt(source="print('hi')", args=[])

    with pytest.raises(ValueError, match=r"attempts\[0\]\.args supplied without script_path"):
        run_cpsat_python_experiment([attempt])


def test_missing_script_path_surfaces_indexed_validation_error(tmp_path: Path) -> None:
    attempt = CpsatPythonExperimentAttempt(script_path=str(tmp_path / "nope.py"))

    with pytest.raises(ValueError, match=r"attempts\[0\]\.script_path does not exist"):
        run_cpsat_python_experiment([attempt])


def test_empty_script_path_file_surfaces_indexed_validation_error(tmp_path: Path) -> None:
    script = _write_script(tmp_path, body="   \n")
    attempt = CpsatPythonExperimentAttempt(script_path=str(script))

    with pytest.raises(ValueError, match=r"attempts\[0\]\.script_path file is empty"):
        run_cpsat_python_experiment([attempt])


def test_non_utf8_script_path_surfaces_indexed_validation_error(tmp_path: Path) -> None:
    script = tmp_path / "latin1.py"
    script.write_bytes(b"print('\xff\xfe caf\xe9')\n")
    attempt = CpsatPythonExperimentAttempt(script_path=str(script))

    with pytest.raises(ValueError, match=r"attempts\[0\]\.script_path is not valid UTF-8"):
        run_cpsat_python_experiment([attempt])


def test_error_message_names_the_offending_attempt_index(tmp_path: Path) -> None:
    attempts = [
        _attempt("a"),
        CpsatPythonExperimentAttempt(script_path=str(tmp_path / "nope.py")),
    ]

    with pytest.raises(ValueError, match=r"attempts\[1\]\.script_path does not exist"):
        run_cpsat_python_experiment(attempts)


def test_invalid_script_path_rejects_before_any_earlier_attempt_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Validation is a full upfront pass: a bad index-1 path spawns no index-0 child."""
    inline_calls: list[dict[str, Any]] = []
    file_calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=inline_calls)
    _patch_file_runner(monkeypatch, calls=file_calls)
    attempts = [
        _attempt("a"),
        CpsatPythonExperimentAttempt(script_path=str(tmp_path / "nope.py")),
    ]

    with pytest.raises(ValueError, match=r"attempts\[1\]\.script_path does not exist"):
        run_cpsat_python_experiment(attempts)

    assert inline_calls == []
    assert file_calls == []


def test_args_containing_nul_rejects_before_any_earlier_attempt_runs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A NUL arg is caught in preflight, not by Popen after index 0 already ran."""
    inline_calls: list[dict[str, Any]] = []
    file_calls: list[dict[str, Any]] = []
    _patch_runner(monkeypatch, {"a": _result()}, calls=inline_calls)
    _patch_file_runner(monkeypatch, calls=file_calls)
    attempts = [
        _attempt("a"),
        CpsatPythonExperimentAttempt(
            script_path=str(_write_script(tmp_path)), args=["ok", "bad\0arg"]
        ),
    ]

    with pytest.raises(ValueError, match=r"attempts\[1\]\.args\[1\] contains a NUL character"):
        run_cpsat_python_experiment(attempts)

    assert inline_calls == []
    assert file_calls == []


def test_script_path_attempt_row_records_used_script_path_and_file_hash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(tmp_path)
    _patch_file_runner(monkeypatch, _result(objective=3))

    result = run_cpsat_python_experiment(
        [CpsatPythonExperimentAttempt(name="from_file", script_path=str(script))],
        objective_sense="minimize",
    )

    row = result.attempts[0]
    assert row.used_script_path is True
    assert row.source_sha256 == path_sha256(script)


def test_inline_source_attempt_row_does_not_set_used_script_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_runner(monkeypatch, {"a": _result(objective=3)})

    result = run_cpsat_python_experiment([_attempt("a")], objective_sense="minimize")

    assert result.attempts[0].used_script_path is False


def test_script_path_hash_is_unnormalized_raw_bytes_for_crlf_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A read_text()-based hash would normalize CRLF to \\n; path_sha256 must not."""
    script = tmp_path / "crlf.py"
    script.write_bytes(b"import sys\r\nprint('hi')\r\n")
    _patch_file_runner(monkeypatch, _result(objective=3))

    result = run_cpsat_python_experiment(
        [CpsatPythonExperimentAttempt(script_path=str(script))], objective_sense="minimize"
    )

    assert result.attempts[0].source_sha256 == path_sha256(script)
    assert result.attempts[0].source_sha256 != text_sha256(script.read_text(encoding="utf-8"))


def test_script_path_attempt_forwards_args_and_resolved_path_to_the_file_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(tmp_path)
    calls: list[dict[str, Any]] = []
    _patch_file_runner(monkeypatch, _result(objective=3), calls=calls)

    run_cpsat_python_experiment(
        [
            CpsatPythonExperimentAttempt(
                script_path=str(script), args=["--seed", "7"], script_timeout_ms=1234
            )
        ],
        objective_sense="minimize",
    )

    assert len(calls) == 1
    assert calls[0]["args"] == ["--seed", "7"]
    assert calls[0]["script_path"] == script.resolve()
    assert calls[0]["script_timeout_ms"] == 1234


def test_mixed_inline_and_script_path_attempts_race_in_one_experiment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = _write_script(tmp_path)
    _patch_runner(monkeypatch, {"a": _result(objective=9)})
    _patch_file_runner(monkeypatch, _result(objective=2))

    result = run_cpsat_python_experiment(
        [
            _attempt("a", name="inline"),
            CpsatPythonExperimentAttempt(name="from_file", script_path=str(script)),
        ],
        objective_sense="minimize",
    )

    assert result.winner_name == "from_file"
    assert [row.used_script_path for row in result.attempts] == [False, True]


# --- spawn failure: one attempt cannot start ---------------------------------


def _patch_execute_child_failing_on(monkeypatch: pytest.MonkeyPatch, failing_source: str) -> None:
    """Fail the spawn for one attempt's script, run the rest normally.

    Patched at ``core.execute_child`` rather than at ``experiment.run_cpsat_python``
    so the real spawn-failure handling in ``core`` is what's under test.
    """
    from openconstraint_mcp.shared.childrun import ChildExecutionResult, ChildSpawnError

    def _fake(argv: Any, cwd: Any, **kw: Any) -> ChildExecutionResult:
        source = (Path(cwd) / "script.py").read_text(encoding="utf-8")
        if source == failing_source:
            raise ChildSpawnError(7, "Argument list too long")
        return ChildExecutionResult(
            stdout=json.dumps({"status": "optimal", "objective": 5, "solution": {"x": 1}}),
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.execute_child", _fake)


def test_experiment_survives_an_attempt_that_cannot_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_execute_child_failing_on(monkeypatch, "b")

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.status == "winner"


def test_spawn_failure_does_not_discard_the_other_attempts_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-fix behaviour raised OSError out of pool.map, losing every row —
    # including attempts whose children had already run to completion.
    _patch_execute_child_failing_on(monkeypatch, "b")

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert len(result.attempts) == 2


def test_unspawnable_attempt_is_reported_as_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_execute_child_failing_on(monkeypatch, "b")

    result = run_cpsat_python_experiment([_attempt("a"), _attempt("b")], objective_sense="minimize")

    assert result.attempts[1].accepted is False


def test_file_attempt_post_launch_oserror_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _write_script(tmp_path)

    def _raise(_script_path: Path, **_kwargs: Any) -> CpsatPythonResult:
        raise OSError(5, "post-launch cleanup failed")

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python_file", _raise)

    with pytest.raises(OSError, match="post-launch cleanup failed"):
        run_cpsat_python_experiment([CpsatPythonExperimentAttempt(script_path=str(script))])


# --- script_path invalidated mid-experiment ----------------------------------


def _patch_execute_child_mutating(monkeypatch: pytest.MonkeyPatch, mutate: Any) -> None:
    """Run every attempt successfully, but let attempt "a" mutate the filesystem.

    Models the reviewer's scenario directly: an EARLIER serial attempt changes a
    LATER attempt's script_path out from under the upfront validation pass.
    """
    from openconstraint_mcp.shared.childrun import ChildExecutionResult

    def _fake(argv: Any, cwd: Any, **kw: Any) -> ChildExecutionResult:
        if Path(argv[-1]).read_text(encoding="utf-8") == "a":
            mutate()
        return ChildExecutionResult(
            stdout=json.dumps({"status": "optimal", "objective": 5, "solution": {"x": 1}}),
            stderr="",
            return_code=0,
            timed_out=False,
            truncated=False,
            duration_ms=1,
        )

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.execute_child", _fake)


def _script_attempt_pair(tmp_path: Path) -> tuple[Path, list[CpsatPythonExperimentAttempt]]:
    script = tmp_path / "victim.py"
    script.write_text("print('original')", encoding="utf-8")
    return script, [
        CpsatPythonExperimentAttempt(name="a", source="a"),
        CpsatPythonExperimentAttempt(name="b", script_path=str(script)),
    ]


def test_script_deleted_by_an_earlier_attempt_does_not_abort_the_experiment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, attempts = _script_attempt_pair(tmp_path)
    _patch_execute_child_mutating(monkeypatch, script.unlink)

    result = run_cpsat_python_experiment(attempts, max_parallel_attempts=1)

    assert len(result.attempts) == 2


def test_deleted_script_attempt_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, attempts = _script_attempt_pair(tmp_path)
    _patch_execute_child_mutating(monkeypatch, script.unlink)

    result = run_cpsat_python_experiment(attempts, max_parallel_attempts=1)

    assert result.attempts[1].accepted is False


def test_script_deleted_during_its_run_is_not_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _write_script(tmp_path)

    def _delete(script_path: Path, **_kwargs: Any) -> CpsatPythonResult:
        script_path.unlink()
        return _result()

    monkeypatch.setattr("openconstraint_mcp.pyexec.experiment.run_cpsat_python_file", _delete)

    result = run_cpsat_python_experiment([CpsatPythonExperimentAttempt(script_path=str(script))])

    assert result.attempts[0].accepted is False


def test_script_rewritten_mid_experiment_invalidates_only_that_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, attempts = _script_attempt_pair(tmp_path)
    _patch_execute_child_mutating(
        monkeypatch, lambda: script.write_text("print('swapped')", encoding="utf-8")
    )

    result = run_cpsat_python_experiment(attempts, max_parallel_attempts=1)

    assert result.attempts[0].accepted is True
    assert result.attempts[1].accepted is False


def test_rewritten_script_attempt_says_the_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script, attempts = _script_attempt_pair(tmp_path)
    _patch_execute_child_mutating(
        monkeypatch, lambda: script.write_text("print('swapped')", encoding="utf-8")
    )

    result = run_cpsat_python_experiment(attempts, max_parallel_attempts=1)

    assert "changed on disk" in (result.attempts[1].message or "")


def test_reported_digest_is_the_validated_content_not_the_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The digest must describe the bytes the request was admitted against; the
    # attempt is failed rather than re-labelled with the replacement's hash.
    script, attempts = _script_attempt_pair(tmp_path)
    original_digest = path_sha256(script)
    _patch_execute_child_mutating(
        monkeypatch, lambda: script.write_text("print('swapped')", encoding="utf-8")
    )

    result = run_cpsat_python_experiment(attempts, max_parallel_attempts=1)

    assert result.attempts[1].source_sha256 == original_digest
