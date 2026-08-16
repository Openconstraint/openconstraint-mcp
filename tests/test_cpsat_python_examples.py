from __future__ import annotations

import copy
import json
import runpy
import sys
from pathlib import Path
from typing import Any

import pytest

_EXAMPLES = Path(__file__).parent / "fixtures" / "cpsat_python"

_VALID_CLINIC_ROSTER_SOLUTION: dict[str, object] = {
    "assignments": {
        "mon_day": "elliot",
        "mon_night": "blair",
        "tue_day": "casey",
        "tue_night": "alex",
        "wed_day": "blair",
        "wed_night": "devon",
        "thu_day": "elliot",
        "thu_night": "alex",
        "fri_day": "elliot",
        "fri_night": "alex",
        "sat_day": "casey",
        "sat_night": "devon",
        "sun_day": "casey",
        "sun_night": "devon",
    },
    "shift_counts": {"alex": 3, "blair": 2, "casey": 3, "devon": 3, "elliot": 3},
    "total_preference_penalty": 14,
    "fairness_penalty": 2,
    "total_penalty": 16,
}


def _payload(solution: dict[str, object], objective: int) -> dict[str, object]:
    return {
        "problem": "urgent-care clinic nurse roster",
        "solver_status": "optimal",
        "objective": objective,
        "solution": solution,
    }


def _run_checker(
    checker_name: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> dict[str, Any]:
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    checker_path = _EXAMPLES / checker_name
    old_argv = sys.argv
    try:
        sys.argv = [str(checker_path), str(payload_path)]
        runpy.run_path(str(checker_path), run_name="__main__")
    finally:
        sys.argv = old_argv

    output = capsys.readouterr().out.strip().splitlines()[-1]
    result = json.loads(output)
    assert isinstance(result, dict)
    return result


def _run_clinic_roster_checker(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> dict[str, Any]:
    return _run_checker("clinic_roster_checker.py", tmp_path, capsys, payload)


def test_clinic_roster_checker_accepts_valid_solution(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _run_clinic_roster_checker(
        tmp_path,
        capsys,
        _payload(copy.deepcopy(_VALID_CLINIC_ROSTER_SOLUTION), 16),
    )

    assert result["status"] == "accepted"
    assert result["errors"] == []


def test_clinic_roster_checker_rejects_unqualified_night_shift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    solution = copy.deepcopy(_VALID_CLINIC_ROSTER_SOLUTION)
    assignments = solution["assignments"]
    assert isinstance(assignments, dict)
    assignments["sun_night"] = "elliot"
    solution["shift_counts"] = {"alex": 3, "blair": 2, "casey": 3, "devon": 2, "elliot": 4}
    solution["total_preference_penalty"] = 18
    solution["fairness_penalty"] = 4
    solution["total_penalty"] = 22

    result = _run_clinic_roster_checker(tmp_path, capsys, _payload(solution, 22))

    assert result["status"] == "rejected"
    assert any("not eligible for sun_night" in error for error in result["errors"])


def test_clinic_roster_checker_rejects_missing_rest_after_night(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    solution = copy.deepcopy(_VALID_CLINIC_ROSTER_SOLUTION)
    assignments = solution["assignments"]
    assert isinstance(assignments, dict)
    assignments["wed_day"] = "alex"
    assignments["fri_night"] = "blair"
    solution["total_preference_penalty"] = 15
    solution["total_penalty"] = 17

    result = _run_clinic_roster_checker(tmp_path, capsys, _payload(solution, 17))

    assert result["status"] == "rejected"
    assert any("tue_night then wed_day without rest" in error for error in result["errors"])


# Pentagon graph 0-1-2-3-4-0 plus diagonal 0-2 (see graph_coloring.py's EDGES).
_VALID_GRAPH_COLORING = {"color_0": 0, "color_1": 1, "color_2": 2, "color_3": 0, "color_4": 1}


def test_graph_coloring_checker_accepts_valid_coloring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = _run_checker(
        "graph_coloring_checker.py",
        tmp_path,
        capsys,
        {"solution": _VALID_GRAPH_COLORING},
    )

    assert result["status"] == "accepted"
    assert result["errors"] == []


def test_graph_coloring_checker_rejects_plausible_looking_coloring(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Correct on every edge except the first: vertices 0 and 1 both get color 0.
    # A checker enforcing the adjacency rule the model already encodes still
    # catches a wrong-but-plausible reported result, e.g. from a script bug
    # that emits stale/corrupted variable values instead of the solver's own.
    solution = dict(_VALID_GRAPH_COLORING, color_1=0)

    result = _run_checker(
        "graph_coloring_checker.py",
        tmp_path,
        capsys,
        {"solution": solution},
    )

    assert result["status"] == "rejected"
    assert any("vertices 0 and 1 share color 0" in error for error in result["errors"])
