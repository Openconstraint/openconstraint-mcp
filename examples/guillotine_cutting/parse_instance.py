"""Convert a 2DPackLib .ins2D instance into this example's JSON instance format.

2DPackLib (https://site.unibo.it/operations-research/en/research/2dpacklib)
stores one instance per text file:

    m
    W H
    i w_i h_i d_i b_i p_i        (one line per item i = 1..m)

with W, H the sheet size, w_i, h_i the item size, d_i the minimum number of
copies, b_i the maximum number of copies, and p_i the profit per copy. This
example's scope has no minimum demand, so a file with any d_i > 0 is refused
rather than silently dropping the lower bound. The format has no rotation
field; the instances converted here are oriented (fixed orientation).

The raw files stay untouched in data/; the JSON goes to parsed/, so the model
and checker only ever read JSON. The published optimum is not part of the raw
file: it comes from the table below, with its source.

Run from the repository root:
    uv run examples/guillotine_cutting/parse_instance.py CGCUT1.ins2D
    uv run examples/guillotine_cutting/parse_instance.py OF1.ins2D
    uv run examples/guillotine_cutting/parse_instance.py OF2.ins2D
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

EXAMPLE_DIR: Path = Path(__file__).parent

CITATION: str = (
    "M. Iori, V.L. de Lima, S. Martello, M. Monaci. 2DPackLib: a two-dimensional cutting "
    "and packing library. Optimization Letters 16(2):471-480, 2022"
)
OPTIMUM_SOURCE: str = (
    "S. Polyakovskiy, A. Dehghan, J. Mcgregor, P.J. Stuckey. Mixed-integer and constraint "
    "programming models for the two-dimensional guillotine cutting problem. European Journal "
    "of Operational Research 331:48-61, 2026, Table 2"
)


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class Benchmark(FrozenModel):
    source: str
    problem_class: str
    published_optimum: int


BENCHMARKS: dict[str, Benchmark] = {
    "CGCUT1.ins2D": Benchmark(
        source=(
            "https://site.unibo.it/operations-research/en/research/2dpacklib/cgcut.zip/"
            "@@download/file/CGCUT.zip (CGCUT/CGCUT/CGCUT1.ins2D)"
        ),
        problem_class="stage-unrestricted weighted 2DSLOPP",
        published_optimum=244,
    ),
    "OF1.ins2D": Benchmark(
        source=(
            "https://site.unibo.it/operations-research/en/research/2dpacklib/of.zip/"
            "@@download/file/OF.zip (OF/OF/OF1.ins2D)"
        ),
        problem_class="stage-unrestricted unweighted 2DSLOPP",
        published_optimum=2737,
    ),
    "OF2.ins2D": Benchmark(
        source=(
            "https://site.unibo.it/operations-research/en/research/2dpacklib/of.zip/"
            "@@download/file/OF.zip (OF/OF/OF2.ins2D)"
        ),
        problem_class="stage-unrestricted unweighted 2DSLOPP",
        published_optimum=2690,
    ),
}


def parse_ins2d(text: str, filename: str) -> dict[str, Any]:
    """Return the JSON instance for one .ins2D file's text."""
    benchmark: Benchmark = BENCHMARKS[filename]
    rows: list[list[int]] = [[int(token) for token in line.split()] for line in text.splitlines()]
    rows = [row for row in rows if row]
    num_items: int = rows[0][0]
    sheet_width, sheet_height = rows[1]
    if sheet_height < 0:
        raise ValueError(f"{filename}: height {sheet_height} marks a strip packing instance")
    items: list[list[int]] = rows[2:]
    if len(items) != num_items:
        raise ValueError(f"{filename}: header says {num_items} items, found {len(items)}")

    products: list[dict[str, Any]] = []
    for item_id, width, height, min_copies, max_copies, profit in items:
        if min_copies != 0:
            raise ValueError(
                f"{filename}: item {item_id} has minimum demand {min_copies}; "
                "this example only models maximum quantities"
            )
        products.append(
            {
                "id": str(item_id),
                "width": width,
                "height": height,
                "max_quantity": max_copies,
                "profit": profit,
            }
        )

    return {
        "name": Path(filename).stem,
        "provenance": {
            "source": benchmark.source,
            "citation": CITATION,
            "license": "NOASSERTION",
        },
        "published_optimum": {
            "value": benchmark.published_optimum,
            "problem_class": benchmark.problem_class,
            "source": OPTIMUM_SOURCE,
        },
        "sheet": {"width": sheet_width, "height": sheet_height},
        "products": products,
    }


def main() -> None:
    raw_path: Path = EXAMPLE_DIR / "data" / sys.argv[1]
    instance: dict[str, Any] = parse_ins2d(raw_path.read_text(encoding="utf-8"), raw_path.name)
    output_path: Path = EXAMPLE_DIR / "parsed" / f"{raw_path.stem}.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(instance, indent=2) + "\n", encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
