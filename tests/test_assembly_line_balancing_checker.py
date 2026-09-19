from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "assembly_line_balancing"
_CHECKER_PATH = _EXAMPLE_DIR / "checker.py"
_INSTANCE_TEXT = (_EXAMPLE_DIR / "parsed" / "five_task_line.json").read_text(encoding="utf-8")


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("assembly_line_balancing_checker", _CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _committed_payload() -> dict[str, Any]:
    """The checker payload run_cpsat_python_file_checked builds from the committed
    script stdout: the script's `status` becomes `solver_status`, and `problem`
    is the instance JSON text."""
    envelope = json.loads((_EXAMPLE_DIR / "results" / "optimal.json").read_text(encoding="utf-8"))
    return {
        "problem": _INSTANCE_TEXT,
        "solution": envelope["solution"],
        "objective": envelope["objective"],
        "solver_status": envelope["status"],
    }


def _payload(stations: list[list[int]], objective: object) -> dict[str, Any]:
    """A claim against the five-task instance: times (3, 2, 4, 3, 2), ct = 6,
    precedences 1->2, 1->3, 2->4, 3->4, 4->5."""
    return {
        "problem": _INSTANCE_TEXT,
        "solution": {"stations": stations},
        "objective": objective,
        "solver_status": "feasible",
    }


def test_accepts_committed_optimal_payload() -> None:
    result = _checker.check_payload(_committed_payload())
    assert (result["status"], result["errors"]) == ("accepted", [])


def test_optimal_payload_details_report_loads_and_lower_bound() -> None:
    details = _checker.check_payload(_committed_payload())["details"]
    assert details == {
        "station_count": 3,
        "station_loads": [3, 6, 5],
        "cycle_time": 6,
        "total_work_lower_bound": 3,
    }


def test_rejects_station_over_cycle_time() -> None:
    result = _checker.check_payload(_payload([[1, 2, 3], [4, 5]], 2))
    assert result["errors"] == ["station 1 load 9 exceeds cycle time 6"]


def test_rejects_task_before_its_predecessor() -> None:
    result = _checker.check_payload(_payload([[1, 4], [2, 3], [5]], 3))
    assert result["errors"] == [
        "task 2 (station 2) must not follow its successor 4 (station 1)",
        "task 3 (station 2) must not follow its successor 4 (station 1)",
    ]


def test_rejects_missing_task() -> None:
    result = _checker.check_payload(_payload([[1, 2], [3], [4]], 3))
    assert result["errors"] == ["tasks [5] are on no station"]


def test_rejects_task_on_two_stations() -> None:
    result = _checker.check_payload(_payload([[1, 2], [3], [4, 5], [5]], 4))
    assert result["errors"] == ["task 5 is on stations 3 and 4"]


def test_rejects_unknown_task() -> None:
    result = _checker.check_payload(_payload([[1, 2], [3], [4, 5, 6]], 3))
    assert result["errors"] == ["station 3 holds unknown task 6"]


def test_rejects_objective_that_differs_from_station_count() -> None:
    result = _checker.check_payload(_payload([[1, 2], [3], [4, 5]], 2))
    assert result["errors"] == ["objective 2 does not match station count 3"]


def test_malformed_payload_is_an_error_not_a_rejection() -> None:
    payload = _payload([], 0)
    payload["solution"] = {"assignment": {}}
    result = _checker.check_payload(payload)
    assert (result["status"], result["errors"]) == ("error", ["solution.stations must be a list"])
