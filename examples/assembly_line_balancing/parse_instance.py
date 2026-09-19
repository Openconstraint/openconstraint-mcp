"""Convert a Scholl .IN2 precedence graph into this example's JSON instance format.

The Scholl (1993) data set (https://assembly-line-balancing.de/salbp/benchmark-data-sets-1993/)
stores one precedence graph per text file (README.DOC in the archive):

    n                    line 1: number of tasks
    t_1 ... t_n          lines 2..n+1: integer task times, one per line
    i,j                  then one direct precedence relation per line
    -1,-1                optional end mark

Some files follow the end mark with a free-text note naming the graph's source
(e.g. WARNECKE.IN2); it is not data, so parsing stops at the end mark.
A graph has no cycle time of its own: the benchmark pairs each graph with
several cycle times. So an instance here is a (graph, cycle time) pair, named
<GRAPH>_c<ct>, and the table below fixes the cycle time and the published
optimum of each one converted. Before the end mark, a line that is not a task
time or an "i,j" pair, or a task count that disagrees with the header, is
refused rather than silently dropped.

The raw files stay untouched in data/; the JSON goes to parsed/, so the model
and checker only ever read JSON.

Run from the repository root:
    uv run examples/assembly_line_balancing/parse_instance.py JACKSON_c10
"""

import json
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

EXAMPLE_DIR: Path = Path(__file__).parent

CITATION: str = (
    "A. Scholl. Data of assembly line balancing problems. Schriften zur Quantitativen "
    "Betriebswirtschaftslehre 16/93, TH Darmstadt, 1993"
)
OPTIMUM_SOURCE: str = (
    "A. Scholl, R. Klein. Balancing assembly lines effectively - A computational comparison. "
    "European Journal of Operational Research 114:50-58, 1999; as tabulated in the "
    "'SALBP-1 data set' sheet of the workbook 'SALBP data sets.xlsx' in the source archive"
)


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


class Benchmark(FrozenModel):
    raw_file: str
    source: str
    cycle_time: int
    published_optimum: int


BENCHMARKS: dict[str, Benchmark] = {
    "JACKSON_c10": Benchmark(
        raw_file="JACKSON.IN2",
        source=(
            "https://assembly-line-balancing.de/wp-content/uploads/2017/01/SALBP-data-sets.zip "
            "(precedence graphs/JACKSON.IN2)"
        ),
        cycle_time=10,
        published_optimum=5,
    ),
}


def _pair(line: str, filename: str) -> tuple[int, int]:
    fields: list[str] = line.split(",")
    if len(fields) != 2:
        raise ValueError(f"{filename}: expected a precedence 'i,j', got {line!r}")
    return int(fields[0]), int(fields[1])


def parse_in2(text: str, name: str) -> dict[str, Any]:
    """Return the JSON instance `name` built from one .IN2 file's text."""
    benchmark: Benchmark = BENCHMARKS[name]
    filename: str = benchmark.raw_file
    lines: list[str] = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError(f"{filename}: empty file")
    num_tasks: int = int(lines[0])
    time_lines: list[str] = lines[1 : num_tasks + 1]
    if len(time_lines) != num_tasks or any("," in line for line in time_lines):
        raise ValueError(f"{filename}: header says {num_tasks} tasks, found fewer task times")
    times: list[int] = [int(line) for line in time_lines]

    precedences: list[list[int]] = []
    relation_lines: list[str] = lines[num_tasks + 1 :]
    for line in relation_lines:
        i, j = _pair(line, filename)
        if (i, j) == (-1, -1):
            break
        if not (1 <= i <= num_tasks and 1 <= j <= num_tasks):
            raise ValueError(f"{filename}: precedence {i},{j} names an unknown task")
        precedences.append([i, j])

    return {
        "name": name,
        "provenance": {
            "source": benchmark.source,
            "citation": CITATION,
            "license": "NOASSERTION",
        },
        "published_optimum": {
            "value": benchmark.published_optimum,
            "problem_class": "simple assembly line balancing, type 1",
            "source": OPTIMUM_SOURCE,
        },
        "cycle_time": benchmark.cycle_time,
        "tasks": [{"id": index + 1, "time": time} for index, time in enumerate(times)],
        "precedences": precedences,
    }


def main() -> None:
    name: str = sys.argv[1]
    raw_path: Path = EXAMPLE_DIR / "data" / BENCHMARKS[name].raw_file
    instance: dict[str, Any] = parse_in2(raw_path.read_text(encoding="utf-8"), name)
    output_path: Path = EXAMPLE_DIR / "parsed" / f"{name}.json"
    output_path.parent.mkdir(exist_ok=True)
    output_path.write_text(json.dumps(instance, indent=2) + "\n", encoding="utf-8")
    print(output_path)


if __name__ == "__main__":
    main()
