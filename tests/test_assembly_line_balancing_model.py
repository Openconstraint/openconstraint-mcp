"""In-process solves of examples/assembly_line_balancing/model.py on instances small
enough to prove optimality instantly, plus the tightening quantities (7), (8),
(10) and the (4'') pruning on hand-checkable cases."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

_MODEL_PATH = Path(__file__).parent.parent / "examples" / "assembly_line_balancing" / "model.py"


def _load_model() -> Any:
    spec = importlib.util.spec_from_file_location("assembly_line_balancing_model", _MODEL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_model = _load_model()


def _chain(times: list[int], cycle_time: int) -> Any:
    """Tasks 1 -> 2 -> ... -> n with the given times."""
    return _model.parse_input(
        {
            "cycle_time": cycle_time,
            "tasks": [{"id": index + 1, "time": time} for index, time in enumerate(times)],
            "precedences": [[index, index + 1] for index in range(1, len(times))],
        }
    )


@pytest.mark.parametrize("formulation", ["base", "bounds", "full"])
def test_five_task_line_proves_three_stations(formulation: str) -> None:
    raw = json.loads((_MODEL_PATH.parent / "parsed" / "five_task_line.json").read_text())
    solution = _model.solve(_model.parse_input(raw), formulation)
    assert (solution.status, solution.objective) == ("optimal", 3)


@pytest.mark.parametrize(("name", "published_optimum"), [("GUNTHER_c41", 14), ("KILBRID_c62", 9)])
def test_small_benchmark_proves_its_published_optimum(name: str, published_optimum: int) -> None:
    # The two small-scale Scholl instances prove optimal in under a second.
    raw = json.loads((_MODEL_PATH.parent / "parsed" / f"{name}.json").read_text())
    solution = _model.solve(_model.parse_input(raw))
    assert (solution.status, solution.objective) == ("optimal", published_optimum)


def test_distances_match_the_papers_three_task_chain() -> None:
    # p. 60: ct = 10, t = 4, 4, 4 in a chain gives D12 = D23 = 0 but D13 = 1, and
    # D13 > D12 + D23 keeps (1, 3) through the (4'') pruning.
    instance = _chain([4, 4, 4], 10)
    gaps = _model.station_gaps(instance, _model.precompute(instance))
    assert gaps == {(1, 2): 0, (2, 3): 0, (1, 3): 1}


def test_distance_implied_through_an_intermediate_task_is_pruned() -> None:
    # ct = 5: D12 = D23 = floor(7 / 5) = 1 and D13 = floor(11 / 5) = 2 <= 1 + 1.
    instance = _chain([4, 4, 4], 5)
    gaps = _model.station_gaps(instance, _model.precompute(instance))
    assert gaps == {(1, 2): 1, (2, 3): 1}


def test_tail_does_not_count_an_extra_station_at_an_exact_multiple() -> None:
    # Task 1 and its successors total exactly 20 = 2 * ct: one station after
    # task 1's, not two (the -1 in (8)).
    # Lists are indexed by task id; [1:] skips the index-0 placeholder.
    precomputed = _model.precompute(_chain([10, 10], 10))
    assert (precomputed.earliest_station[1:], precomputed.stations_after[1:]) == ([1, 2], [1, 0])


def test_task_longer_than_the_cycle_time_is_infeasible() -> None:
    solution = _model.solve(_chain([3, 7], 6))
    assert (solution.status, solution.stations) == ("infeasible", None)


def test_precedence_cycle_is_refused() -> None:
    instance = _model.parse_input(
        {
            "cycle_time": 5,
            "tasks": [{"id": 1, "time": 1}, {"id": 2, "time": 1}],
            "precedences": [[1, 2], [2, 1]],
        }
    )
    with pytest.raises(ValueError, match="cycle"):
        _model.solve(instance)
