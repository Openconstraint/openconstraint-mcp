"""In-process runs of examples/assembly_line_balancing/largest_candidate.py, the
largest-candidate rule that serves as the example's comparison."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "assembly_line_balancing"


def _load(filename: str, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, _EXAMPLE_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_rule = _load("largest_candidate.py", "assembly_line_balancing_largest_candidate")
_model = _load("model.py", "assembly_line_balancing_model_for_rule")


def _raw(name: str) -> dict[str, Any]:
    raw: dict[str, Any] = json.loads((_EXAMPLE_DIR / "parsed" / name).read_text(encoding="utf-8"))
    return raw


@pytest.mark.parametrize("name", ["JACKSON_c10", "four_task_line"])
def test_matches_the_committed_result(name: str) -> None:
    committed = json.loads(
        (_EXAMPLE_DIR / "results" / f"{name}_largest_candidate.json").read_text(encoding="utf-8")
    )
    solution = _rule.solve(_rule.parse_input(_raw(f"{name}.json")))
    assert _rule.serialize_solution(solution) == committed


def test_five_task_line_reaches_its_optimum() -> None:
    solution = _rule.solve(_rule.parse_input(_raw("five_task_line.json")))
    assert (solution.status, solution.stations) == ("feasible", [[1, 2], [3], [4, 5]])


def test_four_task_line_reaches_its_optimum_without_its_precedence() -> None:
    # problem.txt: the one relation 3->4 is what makes the rule need 3 stations.
    raw = _raw("four_task_line.json") | {"precedences": []}
    solution = _rule.solve(_rule.parse_input(raw))
    assert (solution.status, solution.stations) == ("feasible", [[2, 4], [1, 3]])


@pytest.mark.parametrize("path", sorted(_EXAMPLE_DIR.glob("parsed/*.json")), ids=lambda p: p.stem)
def test_station_count_matches_the_models_greedy_upper_bound(path: Path) -> None:
    # The docstring's claim: this is the rule greedy_station_count runs.
    raw = _raw(path.name)
    solution = _rule.solve(_rule.parse_input(raw))
    assert solution.objective == _model.greedy_station_count(_model.parse_input(raw))


def test_task_longer_than_the_cycle_time_is_infeasible() -> None:
    raw = {
        "cycle_time": 3,
        "tasks": [{"id": 1, "time": 2}, {"id": 2, "time": 4}],
        "precedences": [],
    }
    solution = _rule.solve(_rule.parse_input(raw))
    assert (solution.status, solution.stations) == ("infeasible", None)


def test_precedence_cycle_is_rejected() -> None:
    raw = {
        "cycle_time": 5,
        "tasks": [{"id": 1, "time": 1}, {"id": 2, "time": 1}],
        "precedences": [[1, 2], [2, 1]],
    }
    with pytest.raises(ValueError, match="cycle"):
        _rule.solve(_rule.parse_input(raw))
