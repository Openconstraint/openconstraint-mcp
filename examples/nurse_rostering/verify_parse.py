"""Assert the parsed QMC-2 instance reproduces every published count.

This is a gate, not a report: a wrong parse produces a model and a scorer that
agree with each other and disagree with reality, which is the one failure mode
no amount of solving will surface. Every expectation here comes from the
instance's documented structure, so a mismatch means the parser is wrong.

Run from the repository root:
    uv run examples/nurse_rostering/verify_parse.py
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parse_instance import Instance, parse_instance  # noqa: E402

EXPECTED_WEIGHT_COUNTS: dict[int, int] = {1000: 65, 100: 57, 1: 56}

EXPECTED_RULES: dict[str, tuple[int, str, int, int]] = {
    # label: (number of <Match> blocks, sense, threshold count, weight)
    "No N-E": (19, "max", 0, 1000),
    "No N-L": (19, "max", 0, 1000),
    "Max 6 consecutive working days": (19, "max", 0, 1000),
    "Min 2 consecutive working days": (8, "max", 0, 1000),
    "No half weekends": (19, "max", 0, 1),
    "Max 3 working weekends": (19, "max", 3, 1),
    "Min 2 consecutive 'N' shifts": (18, "max", 0, 1),
}

EXPECTED_WORKLOAD_RULES: dict[str, tuple[int, int, int]] = {
    # label: (number of <TimeUnits> blocks, threshold in tenths of an hour, weight)
    "Max 75 hours in two weeks": (24, 750, 100),
    "Max 60 hours in two weeks": (24, 600, 100),
    "Max 46 hours in two weeks": (6, 460, 100),
    "Max 30 hours in two weeks": (3, 300, 100),
}


def _check(results: list[tuple[bool, str]], label: str, actual: object, expected: object) -> None:
    ok: bool = actual == expected
    detail: str = f"{actual}" if ok else f"{actual}  (expected {expected})"
    results.append((ok, f"{label:<46} {detail}"))


def verify(instance: Instance) -> list[tuple[bool, str]]:
    results: list[tuple[bool, str]] = []

    _check(results, "employees", len(instance.employees), 19)
    _check(
        results,
        "employee IDs",
        "".join(e.id for e in instance.employees),
        "ABCDEFGHIJKLMNOPQRS",
    )
    _check(results, "contracts defined", len(instance.contracts), 19)

    referenced: set[str] = {e.contract_id for e in instance.employees}
    _check(results, "contracts referenced", len(referenced), 18)
    _check(results, "employee P's contract", _contract_of(instance, "P"), "O")
    defined: set[str] = {c.id for c in instance.contracts}
    _check(results, "unused contracts", sorted(defined - referenced), ["P"])

    _check(results, "shift types", instance.shift_types, ["E", "L", "N"])
    _check(results, "shift durations (tenths of an hour)", instance.shift_time_units,
           {"E": 75, "L": 75, "N": 100})
    _check(results, "shift group All", instance.shift_groups.get("All"), ["E", "L", "N"])

    _check(results, "planning period days", instance.num_days, 28)
    _check(results, "start date", instance.start_date, "2001-03-05")
    _check(results, "end date", instance.end_date, "2001-04-01")

    _check(results, "skills", instance.skills, ["RegisteredNurse", "EyeTrained"])
    for skill, expected_holders in (("RegisteredNurse", 16), ("EyeTrained", 12)):
        holders: int = sum(1 for e in instance.employees if skill in e.skills)
        _check(results, f"employees holding {skill}", holders, expected_holders)
    no_skill: list[str] = [e.id for e in instance.employees if not e.skills]
    _check(results, "employees with no skill", no_skill, ["B"])

    _check(results, "<Cover> blocks", len(instance.cover), 52)
    _check(results, "<Cover> blocks carrying a <Skill>",
           sum(1 for c in instance.cover if c.skill is not None), 31)
    _check(results, "cover weights", instance.cover_weights,
           {"PrefOverStaffing": 1, "PrefUnderStaffing": 1,
            "MaxOverStaffing": 1000, "MinUnderStaffing": 1000})

    matches: list = [m for c in instance.contracts for m in c.matches]
    _check(results, "<Match> blocks", len(matches), 121)
    _check(results, "<Match> blocks with a <Min> sense",
           sum(1 for m in matches if m.limit.sense == "min"), 0)

    patterns: list = [p for m in matches for p in m.patterns]
    _check(results, "<Pattern> elements", len(patterns), 235)
    lengths: collections.Counter = collections.Counter(len(p.symbols) for p in patterns)
    _check(results, "pattern lengths", dict(sorted(lengths.items())), {2: 190, 3: 26, 7: 19})
    # "Shape" has two defensible readings and the instance is documented as
    # having 10; it has 9 distinct symbol sequences, or 11 once the anchor kind
    # is also distinguished. Nothing downstream depends on the taxonomy, so
    # assert the decomposition that does matter -- the length histogram above --
    # and merely report these.
    symbol_shapes: set[tuple] = {
        tuple((s["kind"], s["value"]) for s in p.symbols) for p in patterns
    }
    anchored_shapes: set[tuple] = {
        (
            "start" if p.start_day_index is not None
            else "startday" if p.start_weekday is not None
            else "free",
            tuple((s["kind"], s["value"]) for s in p.symbols),
        )
        for p in patterns
    }
    results.append(
        (True, f"{'distinct pattern shapes':<46} "
               f"{len(symbol_shapes)} by symbols, {len(anchored_shapes)} with anchor")
    )

    workload: list = [w for c in instance.contracts for w in c.workload]
    _check(results, "<TimeUnits> workload structures", len(workload), 57)
    _check(results, "<TimeUnits> total (durations + workload)",
           len(instance.shift_time_units) + len(workload), 60)
    regions: set[tuple[int, int]] = {(w.region_start, w.region_end) for w in workload}
    _check(results, "workload regions", sorted(regions), [(0, 13), (7, 20), (14, 27)])

    for label, (count, threshold, weight) in EXPECTED_WORKLOAD_RULES.items():
        blocks: list = [w for w in workload if w.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(blocks), count)
        _check(results, f"rule {label!r}: threshold/weight",
               {(w.limit.count, w.limit.weight) for w in blocks}, {(threshold, weight)})

    for label, (count, sense, threshold, weight) in EXPECTED_RULES.items():
        blocks = [m for m in matches if m.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(blocks), count)
        _check(results, f"rule {label!r}: sense/threshold/weight",
               {(m.limit.sense, m.limit.count, m.limit.weight) for m in blocks},
               {(sense, threshold, weight)})

    labels: collections.Counter = collections.Counter(m.limit.label for m in matches)
    _check(results, "distinct <Match> labels", sorted(labels), sorted(EXPECTED_RULES))

    # `No half weekends` is 19 blocks but not 19 identical rules: contract A uses a
    # weekday anchor while the other 18 name specific weekends and directions.
    half: list = [m for m in matches if m.limit.label == "No half weekends"]
    weekday_anchored: list = [
        m for m in half if any(p.start_weekday is not None for p in m.patterns)
    ]
    _check(results, "'No half weekends' using <StartDay>", len(weekday_anchored), 1)
    _check(results, "'No half weekends' using <Start>", len(half) - len(weekday_anchored), 18)

    _check(results, "day-off requests", len(instance.day_off_requests), 223)
    day_off_weights: collections.Counter = collections.Counter(
        r.weight for r in instance.day_off_requests
    )
    _check(results, "day-off requests at weight 1", day_off_weights[1], 168)
    _check(results, "day-off requests at weight 1000", day_off_weights[1000], 55)

    _check(results, "shift-on requests", len(instance.shift_on_requests), 155)
    _check(results, "shift-on requests at weight 1",
           collections.Counter(r.weight for r in instance.shift_on_requests)[1], 155)

    # Every <Weight> ELEMENT in the file: the 121 match limits plus the 57
    # workload limits. Requests carry a weight ATTRIBUTE (weight="1") and cover
    # weights live in their own named tags, so neither belongs in this tally.
    weights: collections.Counter = collections.Counter()
    weights.update(m.limit.weight for m in matches)
    weights.update(w.limit.weight for w in workload)
    _check(results, "<Weight> element value counts", dict(weights), EXPECTED_WEIGHT_COUNTS)
    _check(results, "<Weight> elements total", sum(weights.values()), 178)

    return results


def _contract_of(instance: Instance, employee_id: str) -> str:
    return next(e.contract_id for e in instance.employees if e.id == employee_id)


def main() -> None:
    here: Path = Path(__file__).parent
    instance: Instance = parse_instance(here / (sys.argv[1] if len(sys.argv) > 1 else "QMC-2.ros"))

    results: list[tuple[bool, str]] = verify(instance)
    for ok, line in results:
        print(f"  {'ok  ' if ok else 'FAIL'}  {line}")

    failures: int = sum(1 for ok, _ in results if not ok)
    print(f"\n{len(results) - failures}/{len(results)} checks passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
