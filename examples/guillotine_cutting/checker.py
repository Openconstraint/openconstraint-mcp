"""Checker script for model.py and shelf_packing.py.

Validates one claimed cutting pattern against the guillotine cutting instance
supplied via `payload["problem"]`. It never solves the problem; it only grades
the answer it is given:

- every piece names a known product with that product's width and height, in
  its fixed orientation (a swapped width/height is reported as rotated);
- every piece lies inside the sheet and no two pieces overlap;
- no product is cut more often than its maximum quantity;
- the reported objective equals the recomputed profit;
- the layout can be cut with guillotine cuts only, decided from the piece
  rectangles alone (see `_unseparable_group`);
- if the solution also carries a cut tree, it is consistent with the pieces.
  The tree is checked but never used as the proof of guillotine-ness.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str). The checker admits solver_status in {"optimal",
  "feasible", "timeout"} -- mirroring pyexec/eligibility.py's
  DIAGNOSTIC_ACCEPT_STATUSES -- and treats every other value as ungradeable.
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {...}}
- "accepted" with an empty errors list is the only passing verdict.

The two failing verdicts are split by WHAT failed, as in ../job_shop/checker.py.
"error" means the payload could not be graded at all -- an unusable instance, or
a solution/solver_status that is not a well-formed pattern claim. "rejected"
means a well-formed pattern WAS graded against the instance and violates it.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from typing import Any

from typing_extensions import TypeIs

# (x, y, width, height), origin at the sheet's bottom-left corner.
Rect = tuple[int, int, int, int]
# id -> (width, height, max_quantity, profit)
Products = dict[str, tuple[int, int, int, int]]
# (rect, direction, position)
CutClaim = tuple[Rect, str, int]

ACCEPT_STATUSES: frozenset[str] = frozenset({"optimal", "feasible", "timeout"})


def _is_int(value: object) -> TypeIs[int]:
    """True only for a genuine int: JSON `true`/`false` must not pass as 1/0."""
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_instance(problem: object) -> tuple[tuple[int, int, Products] | None, str | None]:
    """Parse (sheet_width, sheet_height, products) out of payload["problem"]."""
    if not isinstance(problem, str):
        return None, "payload.problem is missing or not a string"
    try:
        instance: object = json.loads(problem)
    except json.JSONDecodeError as exc:
        return None, f"payload.problem is not valid JSON: {exc}"
    if not isinstance(instance, dict):
        return None, "problem instance is not a JSON object"

    sheet: object = instance.get("sheet")
    if not isinstance(sheet, dict):
        return None, "problem instance sheet missing or not an object"
    sheet_width: object = sheet.get("width")
    sheet_height: object = sheet.get("height")
    if not _is_int(sheet_width) or not _is_int(sheet_height):
        return None, "problem instance sheet width/height missing or not ints"
    if sheet_width < 1 or sheet_height < 1:
        return None, f"problem instance sheet {sheet_width}x{sheet_height} is not positive"

    raw_products: object = instance.get("products")
    if not isinstance(raw_products, list) or not raw_products:
        return None, "problem instance products missing, not a list, or empty"

    products: Products = {}
    for index, item in enumerate(raw_products):
        if not isinstance(item, dict):
            return None, f"problem instance products[{index}] is not an object"
        product_id: object = item.get("id")
        if not isinstance(product_id, str):
            return None, f"problem instance products[{index}].id missing or not a string"
        if product_id in products:
            return None, f"problem instance product id {product_id!r} is duplicated"
        values: list[object] = [item.get(key) for key in ("width", "height")]
        limits: list[object] = [item.get(key) for key in ("max_quantity", "profit")]
        if not all(_is_int(v) and v >= 1 for v in values):
            return None, f"problem instance product {product_id!r} width/height not positive ints"
        if not all(_is_int(v) and v >= 0 for v in limits):
            return (
                None,
                f"problem instance product {product_id!r} max_quantity/profit "
                "not non-negative ints",
            )
        products[product_id] = (
            int(item["width"]),
            int(item["height"]),
            int(item["max_quantity"]),
            int(item["profit"]),
        )
    return (sheet_width, sheet_height, products), None


def _load_rect(raw: object, where: str, errors: list[str]) -> Rect | None:
    if not isinstance(raw, dict):
        errors.append(f"{where} is not an object")
        return None
    fields: list[object] = [raw.get(key) for key in ("x", "y", "width", "height")]
    x, y, width, height = fields
    if not (_is_int(x) and _is_int(y) and _is_int(width) and _is_int(height)):
        errors.append(f"{where} needs int x, y, width, height")
        return None
    if width < 1 or height < 1:
        errors.append(f"{where} width/height must be positive")
        return None
    return (x, y, width, height)


def _load_pieces(solution: object) -> tuple[list[tuple[str, Rect]] | None, list[str]]:
    if not isinstance(solution, dict):
        return None, ["solution is not a dict"]
    raw_pieces: object = solution.get("pieces")
    if not isinstance(raw_pieces, list):
        return None, ["solution.pieces must be a list"]
    errors: list[str] = []
    pieces: list[tuple[str, Rect]] = []
    for index, raw in enumerate(raw_pieces):
        where: str = f"pieces[{index}]"
        rect: Rect | None = _load_rect(raw, where, errors)
        product: object = raw.get("product") if isinstance(raw, dict) else None
        if isinstance(raw, dict) and not isinstance(product, str):
            errors.append(f"{where}.product missing or not a string")
        if rect is not None and isinstance(product, str):
            pieces.append((product, rect))
    return (None, errors) if errors else (pieces, errors)


def _load_cuts(solution: dict[str, Any]) -> tuple[list[CutClaim] | None, list[str]]:
    """Return (None, []) when the solution carries no cut tree at all."""
    if "cuts" not in solution:
        return None, []
    raw_cuts: object = solution["cuts"]
    if not isinstance(raw_cuts, list):
        return None, ["solution.cuts must be a list when present"]
    errors: list[str] = []
    cuts: list[CutClaim] = []
    for index, raw in enumerate(raw_cuts):
        where: str = f"cuts[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{where} is not an object")
            continue
        rect: Rect | None = _load_rect(raw.get("rect"), f"{where}.rect", errors)
        direction: object = raw.get("direction")
        position: object = raw.get("position")
        if direction not in ("vertical", "horizontal"):
            errors.append(f"{where}.direction must be 'vertical' or 'horizontal'")
        if not _is_int(position):
            errors.append(f"{where}.position missing or not an int")
        if rect is not None and isinstance(direction, str) and _is_int(position):
            cuts.append((rect, direction, position))
    return (None, errors) if errors else (cuts, errors)


def _separate(rects: list[Rect], group: list[int], axis: int) -> list[list[int]] | None:
    """Split `group` by one straight line across `axis` (0: vertical line at some
    x, 1: horizontal line at some y) that crosses no piece and leaves pieces on
    both sides, or return None if no such line exists."""
    ordered: list[int] = sorted(group, key=lambda i: rects[i][axis])
    reach: int = rects[ordered[0]][axis] + rects[ordered[0]][axis + 2]
    for position, i in enumerate(ordered[1:], start=1):
        if rects[i][axis] >= reach:
            return [ordered[:position], ordered[position:]]
        reach = max(reach, rects[i][axis] + rects[i][axis + 2])
    return None


def _unseparable_group(rects: list[Rect]) -> list[int] | None:
    """Decide guillotine-ness from the piece rectangles alone.

    A layout is guillotine when at most one piece remains, or when some
    full-length straight line crosses no piece, splits the pieces in two, and
    both halves are guillotine in turn. Waste never blocks a cut. Any valid line
    can be taken first: a line that was valid before a split stays valid in the
    half it falls in, so this greedy recursion needs no backtracking.

    Returns the indices of a group of pieces no guillotine cut separates, or None
    when the whole layout is guillotine.
    """
    stack: list[list[int]] = [list(range(len(rects)))]
    while stack:
        group: list[int] = stack.pop()
        if len(group) <= 1:
            continue
        halves: list[list[int]] | None = _separate(rects, group, 0) or _separate(rects, group, 1)
        if halves is None:
            return sorted(group)
        stack.extend(halves)
    return None


def _cut_tree_errors(cuts: list[CutClaim], sheet: Rect, rects: list[Rect]) -> list[str]:
    """Replay the cut tree from the sheet and match every piece to one leaf."""
    uncut: set[Rect] = {sheet}
    for index, (rect, direction, position) in enumerate(cuts):
        if rect not in uncut:
            return [
                f"cuts[{index}] splits {list(rect)}, which is not an uncut rectangle "
                "produced by the sheet and the cuts listed before it"
            ]
        x, y, width, height = rect
        children: tuple[Rect, Rect]
        if direction == "vertical":
            if not x < position < x + width:
                return [f"cuts[{index}] vertical position {position} is not inside {list(rect)}"]
            children = ((x, y, position - x, height), (position, y, x + width - position, height))
        else:
            if not y < position < y + height:
                return [f"cuts[{index}] horizontal position {position} is not inside {list(rect)}"]
            children = ((x, y, width, position - y), (x, position, width, y + height - position))
        uncut.remove(rect)
        uncut.update(children)
    return [
        f"pieces[{index}] {list(rect)} is not a leaf rectangle of the cut tree"
        for index, rect in enumerate(rects)
        if rect not in uncut
    ]


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    parsed, instance_error = _parse_instance(payload.get("problem"))
    if instance_error is not None:
        return {"status": "error", "errors": [instance_error], "details": {}}
    assert parsed is not None
    sheet_width, sheet_height, products = parsed

    protocol_errors: list[str] = []
    solver_status: object = payload.get("solver_status")
    if solver_status not in ACCEPT_STATUSES:
        protocol_errors.append(
            f"solver_status is {solver_status!r}, expected optimal, feasible, or timeout"
        )
    pieces, piece_errors = _load_pieces(payload.get("solution"))
    protocol_errors.extend(piece_errors)
    cuts: list[CutClaim] | None = None
    solution: object = payload.get("solution")
    if isinstance(solution, dict):
        cuts, cut_errors = _load_cuts(solution)
        protocol_errors.extend(cut_errors)
    if protocol_errors:
        return {"status": "error", "errors": protocol_errors, "details": {}}
    assert pieces is not None

    errors: list[str] = []
    rects: list[Rect] = [rect for _, rect in pieces]
    counts: dict[str, int] = dict.fromkeys(products, 0)
    profit: int = 0

    for index, (product_id, (x, y, width, height)) in enumerate(pieces):
        if product_id not in products:
            errors.append(f"pieces[{index}] names unknown product {product_id!r}")
        else:
            product_width, product_height, _, product_profit = products[product_id]
            counts[product_id] += 1
            profit += product_profit
            if (width, height) != (product_width, product_height):
                if (width, height) == (product_height, product_width):
                    errors.append(
                        f"pieces[{index}] {product_id} is rotated: {width}x{height}, orientation "
                        f"is fixed at {product_width}x{product_height}"
                    )
                else:
                    errors.append(
                        f"pieces[{index}] {product_id} is {width}x{height}, "
                        f"expected {product_width}x{product_height}"
                    )
        if x < 0 or y < 0 or x + width > sheet_width or y + height > sheet_height:
            errors.append(
                f"pieces[{index}] {[x, y, width, height]} is not inside the "
                f"{sheet_width}x{sheet_height} sheet"
            )

    for a in range(len(rects)):
        for b in range(a + 1, len(rects)):
            ax, ay, aw, ah = rects[a]
            bx, by, bw, bh = rects[b]
            if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
                errors.append(f"pieces[{a}] and pieces[{b}] overlap")

    for product_id, count in counts.items():
        max_quantity: int = products[product_id][2]
        if count > max_quantity:
            errors.append(f"product {product_id} is cut {count} times, maximum is {max_quantity}")

    objective: object = payload.get("objective")
    if not isinstance(objective, int | float) or isinstance(objective, bool):
        errors.append(
            f"objective must be a number equal to the recomputed profit {profit}, got {objective!r}"
        )
    elif objective != profit:
        errors.append(f"objective {objective} does not match recomputed profit {profit}")

    unseparable: list[int] | None = _unseparable_group(rects)
    if unseparable is not None:
        errors.append(
            f"not guillotine: no straight edge-to-edge cut separates pieces {unseparable}"
        )

    if cuts is not None:
        errors.extend(_cut_tree_errors(cuts, (0, 0, sheet_width, sheet_height), rects))

    used_area: int = sum(width * height for _, _, width, height in rects)
    details: dict[str, Any] = {
        "recomputed_profit": profit,
        "pieces_per_product": counts,
        "sheet_area": sheet_width * sheet_height,
        "used_area": used_area,
        "unused_area": sheet_width * sheet_height - used_area,
        "guillotine": unseparable is None,
        "cut_tree_checked": cuts is not None,
    }
    status: str = "accepted" if not errors else "rejected"
    return {"status": status, "errors": errors, "details": details}


def main() -> None:
    if len(sys.argv) != 2:
        print(
            json.dumps(
                {
                    "status": "error",
                    "errors": ["usage: python checker.py <payload.json>"],
                    "details": {},
                }
            )
        )
        return

    with open(sys.argv[1], encoding="utf-8") as payload_file:
        payload: dict[str, Any] = json.load(payload_file)
    print(json.dumps(check_payload(payload)))


if __name__ == "__main__":
    main()
