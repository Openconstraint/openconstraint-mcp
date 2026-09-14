"""Reference CP-SAT script: two-dimensional guillotine cutting.

Cut pieces from one rectangular sheet to maximize total profit. Each product has
a fixed orientation, a maximum quantity, and a profit per piece; unused sheet is
allowed. Every cut is a guillotine cut -- straight across the current rectangle
from one edge to the opposite edge -- and the number of cutting stages is
unlimited.

The model decides the cut tree itself, not just piece coordinates. It has a
list of node slots; a used node is either a cut, which splits its rectangle into
two child rectangles, or a leaf, which holds exactly one piece at its bottom-left
corner. A layout that no sequence of guillotine cuts can produce has no such
tree, so it cannot be expressed at all.

Loads a JSON instance from parsed/ (default: polarizing_film.json) and prints one
JSON result. An optional second argument caps CP-SAT's search time in seconds;
without it the search runs until it proves optimality.
Run from the repository root:
    uv run examples/guillotine_cutting/model.py polarizing_film.json
    uv run examples/guillotine_cutting/model.py CGCUT1.json 60
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Literal, NamedTuple

from ortools.sat.python import cp_model
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
    objective: int | None = None
    best_objective_bound: float | None = None
    pieces: list[PlacedPiece] | None = None
    cuts: list[Cut] | None = None


class Node(NamedTuple):
    """CP-SAT variables of one node slot in the cut tree."""

    used: cp_model.IntVar
    is_cut: cp_model.IntVar
    vertical: cp_model.IntVar
    position: cp_model.IntVar
    x1: cp_model.IntVar
    y1: cp_model.IntVar
    x2: cp_model.IntVar
    y2: cp_model.IntVar
    holds: list[cp_model.IntVar]


def _time_limit_seconds() -> float | None:
    return float(sys.argv[2]) if len(sys.argv) > 2 else None


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


def normal_offsets(sizes: list[tuple[int, int]], limit: int) -> list[int]:
    """Cut offsets worth trying: sums of piece sizes, each size used at most its
    maximum quantity, strictly between 0 and `limit`.

    Any guillotine pattern can slide its pieces left and down until every cut
    sits at such a sum from the origin of the rectangle it splits (Herz 1972;
    Christofides & Whitlock 1977), so restricting cuts to these offsets loses no
    optimal solution.
    """
    reachable: set[int] = {0}
    for size, quantity in sizes:
        reachable = {
            base + count * size
            for base in reachable
            for count in range(quantity + 1)
            if base + count * size <= limit
        }
    return sorted(value for value in reachable if 0 < value < limit)


def trim_cuts(leaf: Rect, product: Product) -> list[Cut]:
    """The cuts that free a piece sitting at the bottom-left of its leaf rectangle:
    a vertical cut if the leaf is wider than the piece, then a horizontal cut on
    the part holding the piece if the leaf is taller."""
    cuts: list[Cut] = []
    if leaf.width > product.width:
        cuts.append(Cut(rect=leaf, direction="vertical", position=leaf.x + product.width))
    if leaf.height > product.height:
        column: Rect = Rect(x=leaf.x, y=leaf.y, width=product.width, height=leaf.height)
        cuts.append(Cut(rect=column, direction="horizontal", position=leaf.y + product.height))
    return cuts


def solve(instance: ProblemInstance, time_limit_seconds: float | None = None) -> Solution:
    sheet_width: int = instance.sheet_width
    sheet_height: int = instance.sheet_height
    products: list[Product] = instance.products

    # A full binary cut tree with n pieces as leaves has 2n - 1 nodes. Bound n by
    # the pieces that fit the sheet at all, and by how many of the smallest could
    # share its area. With nothing to place, one (unused) root slot remains.
    fitting: list[Product] = [
        product
        for product in products
        if product.width <= sheet_width
        and product.height <= sheet_height
        and product.max_quantity > 0
    ]
    max_pieces: int = 0
    if fitting:
        smallest_area: int = min(product.width * product.height for product in fitting)
        max_pieces = min(
            sum(product.max_quantity for product in fitting),
            sheet_width * sheet_height // smallest_area,
        )
        
    num_nodes: int = max(1, 2 * max_pieces - 1)

    x_offsets: list[int] = normal_offsets(
        [(product.width, product.max_quantity) for product in fitting], sheet_width
    )
    y_offsets: list[int] = normal_offsets(
        [(product.height, product.max_quantity) for product in fitting], sheet_height
    )

    model: cp_model.CpModel = cp_model.CpModel()

    nodes: list[Node] = []
    for k in range(num_nodes):
        node: Node = Node(
            used=model.new_bool_var(f"used_{k}"),
            is_cut=model.new_bool_var(f"is_cut_{k}"),
            vertical=model.new_bool_var(f"vertical_{k}"),
            position=model.new_int_var(0, max(sheet_width, sheet_height), f"position_{k}"),
            x1=model.new_int_var(0, sheet_width, f"x1_{k}"),
            y1=model.new_int_var(0, sheet_height, f"y1_{k}"),
            x2=model.new_int_var(0, sheet_width, f"x2_{k}"),
            y2=model.new_int_var(0, sheet_height, f"y2_{k}"),
            holds=[model.new_bool_var(f"holds_{k}_{product.id}") for product in products],
        )
        nodes.append(node)

        # A used node is exactly one of: a cut, or a leaf holding one piece.
        model.add(sum(node.holds) + node.is_cut == node.used)
        model.add(node.vertical <= node.is_cut)

        # A cut lies strictly inside its rectangle, at a normal offset from its
        # left (vertical cut) or bottom (horizontal cut) edge.
        if x_offsets:
            x_offset: cp_model.IntVar = model.new_int_var_from_domain(
                cp_model.Domain.from_values(x_offsets), f"x_offset_{k}"
            )
            model.add(node.position == node.x1 + x_offset).only_enforce_if(
                [node.is_cut, node.vertical]
            )
            model.add(node.position < node.x2).only_enforce_if([node.is_cut, node.vertical])
        else:
            model.add(node.vertical == 0)
        if y_offsets:
            y_offset: cp_model.IntVar = model.new_int_var_from_domain(
                cp_model.Domain.from_values(y_offsets), f"y_offset_{k}"
            )
            model.add(node.position == node.y1 + y_offset).only_enforce_if(
                [node.is_cut, ~node.vertical]
            )
            model.add(node.position < node.y2).only_enforce_if([node.is_cut, ~node.vertical])
        else:
            model.add(node.vertical == 1).only_enforce_if(node.is_cut)
        model.add(node.position == 0).only_enforce_if(~node.is_cut)

        # A leaf rectangle is at least as large as its piece; the rest is waste.
        for product, holds in zip(products, node.holds, strict=True):
            model.add(node.x2 - node.x1 >= product.width).only_enforce_if(holds)
            model.add(node.y2 - node.y1 >= product.height).only_enforce_if(holds)

        if k > 0:
            # Used nodes come first, and unused ones carry no geometry.
            model.add(node.used <= nodes[k - 1].used)
            for coordinate in (node.x1, node.y1, node.x2, node.y2):
                model.add(coordinate == 0).only_enforce_if(~node.used)

    # The root rectangle is the whole sheet.
    root: Node = nodes[0]
    model.add(root.x1 == 0)
    model.add(root.y1 == 0)
    model.add(root.x2 == sheet_width)
    model.add(root.y2 == sheet_height)

    # Every non-root used node is one child of an earlier cut: side 0 is the
    # left (or bottom) rectangle, side 1 the right (or top) one.
    links: dict[tuple[int, int, int], cp_model.IntVar] = {}
    previous_slot: cp_model.IntVar | None = None
    for child in range(1, num_nodes):
        c: Node = nodes[child]
        slot_terms: list[tuple[int, cp_model.IntVar]] = []
        for parent in range(child):
            p: Node = nodes[parent]
            for side in (0, 1):
                link: cp_model.IntVar = model.new_bool_var(f"link_{child}_{parent}_{side}")
                links[child, parent, side] = link
                slot_terms.append((2 * parent + side, link))
                model.add_implication(link, p.is_cut)
                if side == 0:
                    model.add(c.x1 == p.x1).only_enforce_if(link)
                    model.add(c.y1 == p.y1).only_enforce_if(link)
                    model.add(c.x2 == p.position).only_enforce_if([link, p.vertical])
                    model.add(c.y2 == p.y2).only_enforce_if([link, p.vertical])
                    model.add(c.x2 == p.x2).only_enforce_if([link, ~p.vertical])
                    model.add(c.y2 == p.position).only_enforce_if([link, ~p.vertical])
                    # Symmetry breaking: a run of parallel cuts is always nested
                    # through the second child, so the first child of a cut never
                    # cuts in the same direction.
                    model.add(c.vertical != p.vertical).only_enforce_if([link, c.is_cut])
                else:
                    model.add(c.x2 == p.x2).only_enforce_if(link)
                    model.add(c.y2 == p.y2).only_enforce_if(link)
                    model.add(c.x1 == p.position).only_enforce_if([link, p.vertical])
                    model.add(c.y1 == p.y1).only_enforce_if([link, p.vertical])
                    model.add(c.x1 == p.x1).only_enforce_if([link, ~p.vertical])
                    model.add(c.y1 == p.position).only_enforce_if([link, ~p.vertical])
        model.add(sum(link for _, link in slot_terms) == c.used)

        # Symmetry breaking: number nodes in breadth-first order, so the slots
        # the used nodes fill strictly increase.
        slot: cp_model.IntVar = model.new_int_var(0, 2 * num_nodes, f"slot_{child}")
        model.add(slot == sum(code * link for code, link in slot_terms))
        if previous_slot is not None:
            model.add(slot > previous_slot).only_enforce_if(c.used)
        previous_slot = slot

    # A cut has exactly two children, so every leaf holds a piece.
    for parent in range(num_nodes):
        for side in (0, 1):
            model.add(
                cp_model.LinearExpr.sum(
                    [links[child, parent, side] for child in range(parent + 1, num_nodes)]
                )
                == nodes[parent].is_cut
            )

    num_placed: cp_model.LinearExpr = cp_model.LinearExpr.sum(
        [holds for node in nodes for holds in node.holds]
    )
    # 2n - 1 used nodes for n >= 1 pieces; none at all for an empty selection.
    model.add(cp_model.LinearExpr.sum([node.used for node in nodes]) == 2 * num_placed - root.used)

    for index, product in enumerate(products):
        model.add(sum(node.holds[index] for node in nodes) <= product.max_quantity)

    # Redundant: the pieces cannot cover more than the sheet.
    model.add(
        sum(
            product.width * product.height * node.holds[index]
            for node in nodes
            for index, product in enumerate(products)
        )
        <= sheet_width * sheet_height
    )

    profit: cp_model.LinearExpr = cp_model.LinearExpr.weighted_sum(
        [node.holds[index] for node in nodes for index in range(len(products))],
        [product.profit for _ in nodes for product in products],
    )
    model.maximize(profit)

    solver: cp_model.CpSolver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = 1
    if time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = time_limit_seconds
    status: cp_model.CpSolverStatus = solver.solve(model)

    status_map: dict[cp_model.CpSolverStatus, str] = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }

    pieces: list[PlacedPiece] | None = None
    cuts: list[Cut] | None = None
    objective: int | None = None
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        children: dict[tuple[int, int], int] = {
            (parent, side): child
            for (child, parent, side), link in links.items()
            if solver.boolean_value(link)
        }
        pieces = []
        cuts = []
        # Walk the tree parents-first, so every cut's rectangle was produced by
        # a cut listed before it.
        stack: list[int] = [0] if solver.boolean_value(root.used) else []
        while stack:
            index_of_current: int = stack.pop()
            current: Node = nodes[index_of_current]
            x1: int = solver.value(current.x1)
            y1: int = solver.value(current.y1)
            rect: Rect = Rect(
                x=x1,
                y=y1,
                width=solver.value(current.x2) - x1,
                height=solver.value(current.y2) - y1,
            )
            if solver.boolean_value(current.is_cut):
                direction: Literal["vertical", "horizontal"] = (
                    "vertical" if solver.boolean_value(current.vertical) else "horizontal"
                )
                cuts.append(
                    Cut(rect=rect, direction=direction, position=solver.value(current.position))
                )
                stack.append(children[index_of_current, 1])
                stack.append(children[index_of_current, 0])
            else:
                placed: Product = next(
                    product
                    for product, holds in zip(products, current.holds, strict=True)
                    if solver.boolean_value(holds)
                )
                pieces.append(
                    PlacedPiece(
                        product=placed.id,
                        x=rect.x,
                        y=rect.y,
                        width=placed.width,
                        height=placed.height,
                    )
                )
                cuts.extend(trim_cuts(rect, placed))
        objective = int(solver.objective_value)

    bound_states: tuple[cp_model.CpSolverStatus, ...] = (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
        cp_model.UNKNOWN,
    )
    best_objective_bound: float | None = (
        float(solver.best_objective_bound) if status in bound_states else None
    )

    return Solution(
        status=status_map.get(status, "error"),
        objective=objective,
        best_objective_bound=best_objective_bound,
        pieces=pieces,
        cuts=cuts,
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload_solution: dict[str, Any] = {}
    if solution.pieces is not None and solution.cuts is not None:
        payload_solution = {
            "pieces": [piece.model_dump() for piece in solution.pieces],
            "cuts": [cut.model_dump() for cut in solution.cuts],
        }
    return {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": solution.best_objective_bound,
        "solution": payload_solution,
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    write_output(serialize_solution(solve(parse_input(read_input()), _time_limit_seconds())))


if __name__ == "__main__":
    main()
