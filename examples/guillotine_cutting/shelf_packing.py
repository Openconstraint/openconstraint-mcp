"""Comparison script: a simple shelf-packing rule for guillotine cutting.

Not an optimizer. It builds one feasible pattern with a rule a person could
follow by hand, so its profit can be set against model.py's optimum:

    Take pieces in descending profit per unit area (up to each product's maximum,
    ties in input order); put each on the first shelf where it fits under the
    shelf height and in the remaining width, otherwise open a new shelf on top at
    the piece's height, and skip the piece if that would exceed the sheet height.

Shelves are cut off the sheet bottom-up with horizontal cuts and pieces are cut
off each shelf left to right with vertical cuts, so the pattern is guillotine by
construction. Output has the same shape as model.py's, with status "feasible".

Run from the repository root:
    uv run examples/guillotine_cutting/shelf_packing.py polarizing_film.json
"""

import json
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class Product(FrozenModel):
    id: str
    width: int
    height: int
    max_quantity: int
    profit: int


class ProblemInstance(FrozenModel):
    sheet_width: int
    sheet_height: int
    products: list[Product]


class Rect(FrozenModel):
    x: int
    y: int
    width: int
    height: int


class PlacedPiece(FrozenModel):
    product: str
    x: int
    y: int
    width: int
    height: int


class Cut(FrozenModel):
    rect: Rect
    direction: Literal["vertical", "horizontal"]
    position: int


class Solution(FrozenModel):
    status: str
    objective: int
    pieces: list[PlacedPiece]
    cuts: list[Cut]


def read_input() -> dict[str, Any]:
    filename: str = sys.argv[1] if len(sys.argv) > 1 else "polarizing_film.json"
    data_path: Path = Path(__file__).parent / "parsed" / filename
    raw: dict[str, Any] = json.loads(data_path.read_text(encoding="utf-8"))
    return raw


def parse_input(raw: dict[str, Any]) -> ProblemInstance:
    products: list[Product] = [
        Product(
            id=item["id"],
            width=item["width"],
            height=item["height"],
            max_quantity=item["max_quantity"],
            profit=item["profit"],
        )
        for item in raw["products"]
    ]
    return ProblemInstance(
        sheet_width=raw["sheet"]["width"],
        sheet_height=raw["sheet"]["height"],
        products=products,
    )


def solve(instance: ProblemInstance) -> Solution:
    sheet_width: int = instance.sheet_width
    sheet_height: int = instance.sheet_height
    order: list[Product] = sorted(
        instance.products,
        key=lambda product: -Fraction(product.profit, product.width * product.height),
    )

    shelf_y: list[int] = []
    shelf_height: list[int] = []
    shelf_used_width: list[int] = []
    shelf_pieces: list[list[PlacedPiece]] = []
    top: int = 0

    for product in order:
        for _ in range(product.max_quantity):
            shelf: int | None = next(
                (
                    s
                    for s in range(len(shelf_y))
                    if product.height <= shelf_height[s]
                    and shelf_used_width[s] + product.width <= sheet_width
                ),
                None,
            )
            if shelf is None:
                if top + product.height > sheet_height or product.width > sheet_width:
                    continue
                shelf = len(shelf_y)
                shelf_y.append(top)
                shelf_height.append(product.height)
                shelf_used_width.append(0)
                shelf_pieces.append([])
                top += product.height
            shelf_pieces[shelf].append(
                PlacedPiece(
                    product=product.id,
                    x=shelf_used_width[shelf],
                    y=shelf_y[shelf],
                    width=product.width,
                    height=product.height,
                )
            )
            shelf_used_width[shelf] += product.width

    # Parents before children: each shelf is cut off the rectangle left above the
    # previous shelf, then its pieces are cut off the rest of the shelf in turn.
    cuts: list[Cut] = []
    for y, height, pieces_on_shelf in zip(shelf_y, shelf_height, shelf_pieces, strict=True):
        if y + height < sheet_height:
            above: Rect = Rect(x=0, y=y, width=sheet_width, height=sheet_height - y)
            cuts.append(Cut(rect=above, direction="horizontal", position=y + height))
        for piece in pieces_on_shelf:
            if piece.x + piece.width < sheet_width:
                rest: Rect = Rect(x=piece.x, y=y, width=sheet_width - piece.x, height=height)
                cuts.append(Cut(rect=rest, direction="vertical", position=piece.x + piece.width))
            if piece.height < height:
                column: Rect = Rect(x=piece.x, y=y, width=piece.width, height=height)
                cuts.append(Cut(rect=column, direction="horizontal", position=y + piece.height))

    pieces: list[PlacedPiece] = [piece for shelf_row in shelf_pieces for piece in shelf_row]
    profit_by_id: dict[str, int] = {product.id: product.profit for product in instance.products}
    return Solution(
        status="feasible",
        objective=sum(profit_by_id[piece.product] for piece in pieces),
        pieces=pieces,
        cuts=cuts,
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    return {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": None,
        "solution": {
            "pieces": [piece.model_dump() for piece in solution.pieces],
            "cuts": [cut.model_dump() for cut in solution.cuts],
        },
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(serialize_solution(solve(parse_input(read_input()))))


if __name__ == "__main__":
    main()
