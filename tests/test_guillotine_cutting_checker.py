from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

_EXAMPLE_DIR = Path(__file__).parent.parent / "examples" / "guillotine_cutting"
_CHECKER_PATH = _EXAMPLE_DIR / "checker.py"
_INSTANCE_TEXT = (_EXAMPLE_DIR / "parsed" / "polarizing_film.json").read_text(encoding="utf-8")


def _load_checker() -> Any:
    spec = importlib.util.spec_from_file_location("guillotine_cutting_checker", _CHECKER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_checker = _load_checker()


def _committed_payload(results_name: str) -> dict[str, Any]:
    """The checker payload run_cpsat_python_file_checked builds from a committed
    script stdout: the script's `status` becomes `solver_status`, and `problem`
    is the instance JSON text."""
    envelope = json.loads((_EXAMPLE_DIR / "results" / results_name).read_text(encoding="utf-8"))
    return {
        "problem": _INSTANCE_TEXT,
        "solution": envelope["solution"],
        "objective": envelope["objective"],
        "solver_status": envelope["status"],
    }


# A 4x2 sheet: product A is 2x2, product B is 1x2 (so a rotated B reads 2x1).
_SMALL_INSTANCE = json.dumps(
    {
        "sheet": {"width": 4, "height": 2},
        "products": [
            {"id": "A", "width": 2, "height": 2, "max_quantity": 1, "profit": 5},
            {"id": "B", "width": 1, "height": 2, "max_quantity": 2, "profit": 2},
        ],
    }
)


def _piece(product: str, x: int, y: int, width: int, height: int) -> dict[str, Any]:
    return {"product": product, "x": x, "y": y, "width": width, "height": height}


def _payload(
    pieces: list[dict[str, Any]],
    objective: object,
    problem: str = _SMALL_INSTANCE,
    cuts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    solution: dict[str, Any] = {"pieces": pieces}
    if cuts is not None:
        solution["cuts"] = cuts
    return {
        "problem": problem,
        "solution": solution,
        "objective": objective,
        "solver_status": "feasible",
    }


def test_accepts_committed_optimal_payload() -> None:
    result = _checker.check_payload(_committed_payload("optimal.json"))
    assert (result["status"], result["errors"]) == ("accepted", [])


def test_optimal_payload_details_report_profit_counts_and_areas() -> None:
    details = _checker.check_payload(_committed_payload("optimal.json"))["details"]
    assert details == {
        "recomputed_profit": 99,
        "pieces_per_product": {"P1": 1, "P2": 0, "P3": 3, "P4": 2, "P5": 0},
        "sheet_area": 96,
        "used_area": 70,
        "unused_area": 26,
        "guillotine": True,
        "cut_tree_checked": True,
    }


def test_accepts_committed_shelf_packing_payload() -> None:
    result = _checker.check_payload(_committed_payload("shelf_packing.json"))
    assert (result["status"], result["errors"], result["details"]["recomputed_profit"]) == (
        "accepted",
        [],
        70,
    )


def test_rejects_overlapping_pieces() -> None:
    result = _checker.check_payload(_payload([_piece("A", 0, 0, 2, 2), _piece("B", 1, 0, 1, 2)], 7))
    assert result["status"] == "rejected"
    assert "pieces[0] and pieces[1] overlap" in result["errors"]


def test_rejects_piece_outside_the_sheet() -> None:
    result = _checker.check_payload(_payload([_piece("A", 3, 0, 2, 2)], 5))
    assert result == {
        "status": "rejected",
        "errors": ["pieces[0] [3, 0, 2, 2] is not inside the 4x2 sheet"],
        "details": result["details"],
    }


def test_rejects_rotated_piece() -> None:
    result = _checker.check_payload(_payload([_piece("B", 0, 0, 2, 1)], 2))
    assert result["errors"] == [
        "pieces[0] B is rotated: 2x1, orientation is fixed at 1x2",
    ]


def test_rejects_quantity_above_maximum() -> None:
    result = _checker.check_payload(
        _payload([_piece("A", 0, 0, 2, 2), _piece("A", 2, 0, 2, 2)], 10)
    )
    assert result["errors"] == ["product A is cut 2 times, maximum is 1"]


def test_rejects_objective_that_differs_from_recomputed_profit() -> None:
    result = _checker.check_payload(_payload([_piece("A", 0, 0, 2, 2)], 6))
    assert result["errors"] == ["objective 6 does not match recomputed profit 5"]


def test_rejects_non_guillotine_pinwheel() -> None:
    """Four pieces wound around a 1x1 hole on a 3x3 sheet: they overlap nowhere and
    fit the sheet, but every vertical and every horizontal line crosses a piece."""
    pinwheel_instance = json.dumps(
        {
            "sheet": {"width": 3, "height": 3},
            "products": [
                {"id": "H", "width": 2, "height": 1, "max_quantity": 2, "profit": 1},
                {"id": "V", "width": 1, "height": 2, "max_quantity": 2, "profit": 1},
            ],
        }
    )
    pieces = [
        _piece("H", 0, 2, 2, 1),
        _piece("V", 2, 1, 1, 2),
        _piece("H", 1, 0, 2, 1),
        _piece("V", 0, 0, 1, 2),
    ]
    result = _checker.check_payload(_payload(pieces, 4, problem=pinwheel_instance))
    assert result["errors"] == [
        "not guillotine: no straight edge-to-edge cut separates pieces [0, 1, 2, 3]"
    ]


def test_rejects_cut_tree_inconsistent_with_placements() -> None:
    """The layout itself is guillotine; only the claimed tree disagrees with it."""
    wrong_cut = {
        "rect": {"x": 0, "y": 0, "width": 4, "height": 2},
        "direction": "vertical",
        "position": 3,
    }
    result = _checker.check_payload(
        _payload([_piece("A", 0, 0, 2, 2), _piece("B", 2, 0, 1, 2)], 7, cuts=[wrong_cut])
    )
    assert result["errors"] == [
        "pieces[0] [0, 0, 2, 2] is not a leaf rectangle of the cut tree",
        "pieces[1] [2, 0, 1, 2] is not a leaf rectangle of the cut tree",
    ]


def test_malformed_payload_is_an_error_not_a_rejection() -> None:
    payload = _payload([], 0)
    payload["solution"] = {"placements": []}
    result = _checker.check_payload(payload)
    assert (result["status"], result["errors"]) == ("error", ["solution.pieces must be a list"])


def test_accepts_empty_selection_when_nothing_fits() -> None:
    nothing_fits = json.dumps(
        {
            "sheet": {"width": 1, "height": 1},
            "products": [{"id": "A", "width": 2, "height": 2, "max_quantity": 1, "profit": 5}],
        }
    )
    result = _checker.check_payload(_payload([], 0, problem=nothing_fits, cuts=[]))
    assert (result["status"], result["errors"]) == ("accepted", [])
