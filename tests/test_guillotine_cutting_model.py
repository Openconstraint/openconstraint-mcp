"""In-process solves of examples/guillotine_cutting/model.py on instances small
enough to prove optimality instantly, so they run in the default `just check`
(as tests/examples/test_online_printing_shop.py does for sops1)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

_MODEL_PATH = Path(__file__).parent.parent / "examples" / "guillotine_cutting" / "model.py"


def _load_model() -> Any:
    spec = importlib.util.spec_from_file_location("guillotine_cutting_model", _MODEL_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_model = _load_model()


@pytest.mark.parametrize(
    ("sheet", "product_size"),
    [
        # Area rules the piece out: the node-count bound is 0.
        ((1, 1), (2, 2)),
        # Area allows one piece but its height does not fit: the root can be
        # neither a leaf nor a cut.
        ((2, 2), (1, 4)),
    ],
)
def test_empty_selection_is_optimal_when_no_piece_fits(
    sheet: tuple[int, int], product_size: tuple[int, int]
) -> None:
    raw = {
        "sheet": {"width": sheet[0], "height": sheet[1]},
        "products": [
            {
                "id": "A",
                "width": product_size[0],
                "height": product_size[1],
                "max_quantity": 1,
                "profit": 5,
            }
        ],
    }
    solution = _model.solve(_model.parse_input(raw))
    assert (solution.status, solution.objective, solution.pieces, solution.cuts) == (
        "optimal",
        0,
        [],
        [],
    )
