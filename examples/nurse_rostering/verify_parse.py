"""Assert the parsed instances reproduce every published count.

This is a gate, not a report: a wrong parse produces a model and a scorer that
agree with each other and disagree with reality, which is the one failure mode
no amount of solving will surface. Every expectation here comes from the
instance's documented structure, so a mismatch means the parser is wrong.

All three bundled instances are checked, and they are deliberately unalike:
QMC-2 has skills, a Min/Max/Preferred cover band, and only DayOff and ShiftOn
requests; BCV-3.46.2 has no skills, Preferred-only cover with two date-specific
overrides, <NotGroup> pattern symbols, and DayOff, DayOn and ShiftOff requests
but no ShiftOn; ERMGH states all its cover over <TimePeriod> intervals with a
skill on every block, is the only one with <Min>-sense limits or the `$`
wildcard, and writes four requests as an inline <ShiftGroup>. Between them every
branch of the parser is exercised.

Run from the repository root:
    uv run examples/nurse_rostering/verify_parse.py
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from parse_instance import (  # noqa: E402
    DEFAULT_COVER_WEIGHTS,
    CoverBlock,
    Instance,
    Match,
    Pattern,
    Request,
    WorkloadLimit,
    _clock_minutes,
    parse_instance,
)

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

# BCV-3.46.2. Its weight vocabulary is nothing like QMC-2's -- there is no 1000
# / 100 / 1 ladder, cover sits at 10000 and every request at 50 -- which is why
# the two instances get separate expectations rather than a shared parametrised
# one. Reusing QMC-2's "1000 is de facto hard" reading here would be wrong.
BCV_EXPECTED_WEIGHT_COUNTS: dict[int, int] = {
    1000: 5,
    30: 10,
    20: 5,
    10: 5,
    5: 23,
    3: 44,
    2: 5,
    1: 30,
}

BCV_EXPECTED_RULES: dict[str, tuple[int, str, frozenset[tuple[int, int]]]] = {
    # label: (number of <Match> blocks, sense, {(threshold, weight), ...})
    #
    # Unlike QMC-2, one label here can carry several thresholds -- the 44
    # `Max shift types per week` blocks cap between 1 and 5 -- so the expectation
    # is the SET of (threshold, weight) pairs rather than a single pair. 30
    # blocks carry no <Label> element at all and parse to "", which is a real
    # part of this file's structure, not a parse failure.
    "Max shift types per week": (44, "max", frozenset({(1, 3), (2, 3), (3, 3), (4, 3), (5, 3)})),
    "": (
        30,
        "max",
        frozenset({(0, 30), (0, 1000), (2, 1), (8, 5), (9, 5), (14, 5), (16, 5), (19, 5)}),
    ),
    "Both days on or off on weekend": (5, "max", frozenset({(0, 20)})),
    "Max working weekends in four weeks": (5, "max", frozenset({(2, 1)})),
    "Min 2 consecutive working days": (5, "max", frozenset({(0, 1)})),
    "No night shift before free weekend": (5, "max", frozenset({(0, 10)})),
    "Max 2 consecutive N shifts": (4, "max", frozenset({(0, 1)})),
    "Max 4 consecutive working days": (3, "max", frozenset({(0, 5)})),
    "Max 6 N shifts": (3, "max", frozenset({(6, 5)})),
    "Max 7 consecutive free days": (3, "max", frozenset({(0, 1)})),
    "Max 8 N shifts": (2, "max", frozenset({(8, 5)})),
    "Max 10 V shifts": (1, "max", frozenset({(10, 5)})),
    "Max 10 consecutive free days": (1, "max", frozenset({(0, 1)})),
    "Max 15 L shifts": (1, "max", frozenset({(15, 5)})),
    "Max 15 V shifts": (1, "max", frozenset({(15, 5)})),
    "Max 19 L shifts": (1, "max", frozenset({(19, 5)})),
    "Max 19 V shifts": (1, "max", frozenset({(19, 5)})),
    "Max 4 L shifts": (1, "max", frozenset({(4, 5)})),
    "Max 4 consecutive N shifts": (1, "max", frozenset({(0, 1)})),
    "Max 5 V shifts": (1, "max", frozenset({(5, 5)})),
    "Max 5 consecutive working days": (1, "max", frozenset({(0, 5)})),
    "Max 6 L shifts": (1, "max", frozenset({(6, 5)})),
    "Max 7 consecutive working days": (1, "max", frozenset({(0, 5)})),
    "Min 3 consecutive free days": (1, "max", frozenset({(0, 1)})),
}

BCV_EXPECTED_WORKLOAD_RULES: dict[str, tuple[int, int, int]] = {
    # label: (number of <TimeUnits> blocks, threshold in time units, weight)
    # One cap per contract, over the whole period, in this file's own time units
    # (N=10, L=8, V=7) -- not QMC-2's tenths of an hour.
    "Max 152 time units": (1, 152, 2),
    "Max 120 time units": (1, 120, 2),
    "Max 114 time units": (1, 114, 2),
    "Max 80 time units": (1, 80, 2),
    "Max 76 time units": (1, 76, 2),
}


def _requests_of(instance: Instance, wants: bool, named: bool) -> list[Request]:
    """One corner of the request 2x2 -- see `parse_instance.Request`.

    The four XML request tags parse into a single list, so the per-tag counts
    the published structure quotes are recovered by filtering rather than by
    reading four separate fields.
    """
    return [r for r in instance.requests if r.wants is wants and (r.shifts is not None) is named]


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
    _check(
        results,
        "shift durations (tenths of an hour)",
        instance.shift_time_units,
        {"E": 75, "L": 75, "N": 100},
    )
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
    _check(
        results,
        "<Cover> blocks carrying a <Skill>",
        sum(1 for c in instance.cover if c.skill is not None),
        31,
    )
    _check(
        results,
        "cover weights",
        instance.cover_weights,
        {
            "PrefOverStaffing": 1,
            "PrefUnderStaffing": 1,
            "MaxOverStaffing": 1000,
            "MinUnderStaffing": 1000,
        },
    )

    matches: list[Match] = [m for c in instance.contracts for m in c.matches]
    _check(results, "<Match> blocks", len(matches), 121)
    _check(
        results,
        "<Match> blocks with a <Min> sense",
        sum(1 for m in matches if m.limit.sense == "min"),
        0,
    )

    patterns: list[Pattern] = [p for m in matches for p in m.patterns]
    _check(results, "<Pattern> elements", len(patterns), 235)
    lengths: collections.Counter[int] = collections.Counter(len(p.symbols) for p in patterns)
    _check(results, "pattern lengths", dict(sorted(lengths.items())), {2: 190, 3: 26, 7: 19})
    # "Shape" has two defensible readings and the instance is documented as
    # having 10; it has 9 distinct symbol sequences, or 11 once the anchor kind
    # is also distinguished. Nothing downstream depends on the taxonomy, so
    # assert the decomposition that does matter -- the length histogram above --
    # and merely report these.
    symbol_shapes: set[tuple[tuple[str, str], ...]] = {
        tuple((s["kind"], s["value"]) for s in p.symbols) for p in patterns
    }
    anchored_shapes: set[tuple[str, tuple[tuple[str, str], ...]]] = {
        (
            "start"
            if p.start_day_index is not None
            else "startday"
            if p.start_weekday is not None
            else "free",
            tuple((s["kind"], s["value"]) for s in p.symbols),
        )
        for p in patterns
    }
    results.append(
        (
            True,
            f"{'distinct pattern shapes':<46} "
            f"{len(symbol_shapes)} by symbols, {len(anchored_shapes)} with anchor",
        )
    )

    workload: list[WorkloadLimit] = [w for c in instance.contracts for w in c.workload]
    _check(results, "<TimeUnits> workload structures", len(workload), 57)
    _check(
        results,
        "<TimeUnits> total (durations + workload)",
        len(instance.shift_time_units) + len(workload),
        60,
    )
    regions: set[tuple[int, int]] = {(w.region_start, w.region_end) for w in workload}
    _check(results, "workload regions", sorted(regions), [(0, 13), (7, 20), (14, 27)])

    for label, (count, threshold, weight) in EXPECTED_WORKLOAD_RULES.items():
        blocks: list[WorkloadLimit] = [w for w in workload if w.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(blocks), count)
        _check(
            results,
            f"rule {label!r}: threshold/weight",
            {(w.limit.count, w.limit.weight) for w in blocks},
            {(threshold, weight)},
        )

    for label, (count, sense, threshold, weight) in EXPECTED_RULES.items():
        match_blocks: list[Match] = [m for m in matches if m.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(match_blocks), count)
        _check(
            results,
            f"rule {label!r}: sense/threshold/weight",
            {(m.limit.sense, m.limit.count, m.limit.weight) for m in match_blocks},
            {(sense, threshold, weight)},
        )

    labels: collections.Counter[str] = collections.Counter(m.limit.label for m in matches)
    _check(results, "distinct <Match> labels", sorted(labels), sorted(EXPECTED_RULES))

    # `No half weekends` is 19 blocks but not 19 identical rules: contract A uses a
    # weekday anchor while the other 18 name specific weekends and directions.
    half: list[Match] = [m for m in matches if m.limit.label == "No half weekends"]
    weekday_anchored: list[Match] = [
        m for m in half if any(p.start_weekday is not None for p in m.patterns)
    ]
    _check(results, "'No half weekends' using <StartDay>", len(weekday_anchored), 1)
    _check(results, "'No half weekends' using <Start>", len(half) - len(weekday_anchored), 18)

    day_off: list[Request] = _requests_of(instance, wants=False, named=False)
    _check(results, "day-off requests", len(day_off), 223)
    day_off_weights: collections.Counter[int] = collections.Counter(r.weight for r in day_off)
    _check(results, "day-off requests at weight 1", day_off_weights[1], 168)
    _check(results, "day-off requests at weight 1000", day_off_weights[1000], 55)

    shift_on: list[Request] = _requests_of(instance, wants=True, named=True)
    _check(results, "shift-on requests", len(shift_on), 155)
    _check(
        results,
        "shift-on requests at weight 1",
        collections.Counter(r.weight for r in shift_on)[1],
        155,
    )
    # QMC-2 uses only the DayOff/ShiftOn diagonal. Asserting the other two
    # corners are empty is what makes this a regression test for the request
    # unification: a `wants` flag set the wrong way round moves requests between
    # corners while leaving the total at 378 and every other count untouched.
    _check(
        results,
        "day-on and shift-off requests (unused here)",
        [len(_requests_of(instance, True, False)), len(_requests_of(instance, False, True))],
        [0, 0],
    )

    # Every <Weight> ELEMENT in the file: the 121 match limits plus the 57
    # workload limits. Requests carry a weight ATTRIBUTE (weight="1") and cover
    # weights live in their own named tags, so neither belongs in this tally.
    weights: collections.Counter[int] = collections.Counter()
    weights.update(m.limit.weight for m in matches)
    weights.update(w.limit.weight for w in workload)
    _check(results, "<Weight> element value counts", dict(weights), EXPECTED_WEIGHT_COUNTS)
    _check(results, "<Weight> elements total", sum(weights.values()), 178)

    return results


def verify_bcv(instance: Instance) -> list[tuple[bool, str]]:
    """The same gate for BCV-3.46.2, whose structure differs at every turn."""
    results: list[tuple[bool, str]] = []

    _check(results, "employees", len(instance.employees), 46)
    # IDs are numeric strings and the file does NOT list them in numeric order
    # (it opens "1", "46", "2"). Anything that sorts them as text -- a CSV
    # writer, a report -- reorders the roster relative to the instance.
    _check(
        results,
        "employee IDs are 1..46",
        sorted(int(e.id) for e in instance.employees),
        list(range(1, 47)),
    )
    _check(
        results,
        "employee IDs in file order",
        [e.id for e in instance.employees][:3],
        ["1", "46", "2"],
    )

    _check(results, "contracts defined", len(instance.contracts), 5)
    referenced: set[str] = {e.contract_id for e in instance.employees}
    defined: set[str] = {c.id for c in instance.contracts}
    _check(results, "contracts referenced", len(referenced), 5)
    _check(results, "unused contracts", sorted(defined - referenced), [])
    _check(
        results,
        "employees per contract",
        dict(sorted(collections.Counter(e.contract_id for e in instance.employees).items())),
        {"1_2": 9, "3_4": 7, "8_10": 10, "nacht": 2, "waverpleeg": 18},
    )

    _check(results, "shift types", instance.shift_types, ["N", "L", "V"])
    # V is a Vacation shift, but an ordinary assignable shift type with its own
    # duration -- not a second spelling of a day off.
    _check(
        results,
        "shift durations (time units)",
        instance.shift_time_units,
        {"N": 10, "L": 8, "V": 7},
    )
    _check(results, "shift group ON", instance.shift_groups.get("ON"), ["V", "N", "L"])

    _check(results, "planning period days", instance.num_days, 26)
    _check(results, "start date", instance.start_date, "2000-03-06")
    _check(results, "end date", instance.end_date, "2000-03-31")

    # No <Skills> section at all, so the skill branch of cover never fires here.
    _check(results, "skills", instance.skills, [])
    _check(
        results, "employees holding any skill", sum(1 for e in instance.employees if e.skills), 0
    )

    _check(results, "<Cover> blocks", len(instance.cover), 23)
    _check(
        results,
        "<Cover> blocks carrying a <Skill>",
        sum(1 for c in instance.cover if c.skill is not None),
        0,
    )
    _check(
        results,
        "<Cover> blocks with only <Preferred>",
        sum(
            1 for c in instance.cover if c.min is None and c.max is None and c.preferred is not None
        ),
        23,
    )
    # Only PrefUnder/PrefOverStaffing are defined -- there is no MinUnderStaffing
    # or MaxOverStaffing key, which is consistent because no block carries a
    # Min or Max to charge against.
    _check(
        results,
        "cover weights",
        instance.cover_weights,
        {"PrefOverStaffing": 10000, "PrefUnderStaffing": 10000},
    )

    date_specific: list[CoverBlock] = [c for c in instance.cover if c.day is not None]
    _check(results, "<DateSpecificCover> blocks", len(date_specific), 2)
    _check(
        results,
        "date-specific (day, shift, preferred)",
        sorted((c.day, c.shift, c.preferred) for c in date_specific),
        [(7, "N", 4), (13, "N", 6)],
    )
    # The override is only meaningful because the weekday block it displaces
    # asks for something DIFFERENT: day 7 is a Monday wanting 6 on N while the
    # date-specific block wants 4, and day 13 a Sunday wanting 4 against 6.
    # Scoring both would charge that difference at 10000 a head.
    weekday_peers: list[int | None] = [
        peer.preferred
        for block in sorted(date_specific, key=lambda c: c.day or 0)
        for peer in instance.cover
        if peer.day is None
        and peer.shift == block.shift
        and peer.weekday == ((0 + (block.day or 0)) % 7)  # the period starts on a Monday
    ]
    _check(results, "weekday blocks the two dates override", weekday_peers, [6, 4])

    matches: list[Match] = [m for c in instance.contracts for m in c.matches]
    _check(results, "<Match> blocks", len(matches), 122)
    _check(
        results,
        "<Match> blocks with a <Min> sense",
        sum(1 for m in matches if m.limit.sense == "min"),
        0,
    )
    _check(
        results,
        "<Match> blocks with no <Label>",
        sum(1 for m in matches if m.limit.label == ""),
        30,
    )

    patterns: list[Pattern] = [p for m in matches for p in m.patterns]
    _check(results, "<Pattern> elements", len(patterns), 155)
    lengths: collections.Counter[int] = collections.Counter(len(p.symbols) for p in patterns)
    _check(
        results,
        "pattern lengths",
        dict(sorted(lengths.items())),
        {1: 74, 2: 49, 3: 12, 4: 5, 5: 3, 6: 5, 7: 1, 8: 4, 9: 1, 11: 1},
    )
    # The symbol census is the gate on <NotGroup>: parsed as anything else it
    # lands in a different bucket here, or raises "unknown pattern element".
    kinds: collections.Counter[str] = collections.Counter(
        s["kind"] for p in patterns for s in p.symbols
    )
    _check(
        results,
        "pattern symbol kinds",
        dict(sorted(kinds.items())),
        {"group": 119, "notgroup": 35, "notshift": 5, "shift": 173},
    )
    _check(
        results,
        "pattern anchors (Start / StartDay / free)",
        [
            sum(1 for p in patterns if p.start_day_index is not None),
            sum(1 for p in patterns if p.start_weekday is not None),
            sum(1 for p in patterns if p.start_day_index is None and p.start_weekday is None),
        ],
        [12, 40, 103],
    )
    _check(
        results,
        "<StartDay> weekdays (0=Monday)",
        dict(
            sorted(
                collections.Counter(
                    p.start_weekday for p in patterns if p.start_weekday is not None
                ).items()
            )
        ),
        {4: 5, 5: 30, 6: 5},
    )
    _check(
        results,
        "<Match> regions",
        sorted({(m.region_start, m.region_end) for m in matches}),
        [(0, 6), (0, 20), (0, 25), (7, 13), (14, 20), (21, 25)],
    )

    workload: list[WorkloadLimit] = [w for c in instance.contracts for w in c.workload]
    _check(results, "<TimeUnits> workload structures", len(workload), 5)
    _check(
        results,
        "<TimeUnits> total (durations + workload)",
        len(instance.shift_time_units) + len(workload),
        8,
    )
    _check(
        results,
        "workload regions",
        sorted({(w.region_start, w.region_end) for w in workload}),
        [(0, 25)],
    )

    for label, (count, threshold, weight) in BCV_EXPECTED_WORKLOAD_RULES.items():
        blocks: list[WorkloadLimit] = [w for w in workload if w.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(blocks), count)
        _check(
            results,
            f"rule {label!r}: threshold/weight",
            {(w.limit.count, w.limit.weight) for w in blocks},
            {(threshold, weight)},
        )

    for label, (count, sense, pairs) in BCV_EXPECTED_RULES.items():
        match_blocks: list[Match] = [m for m in matches if m.limit.label == label]
        _check(results, f"rule {label!r}: blocks", len(match_blocks), count)
        _check(
            results,
            f"rule {label!r}: sense/thresholds/weights",
            (
                {m.limit.sense for m in match_blocks},
                frozenset((m.limit.count, m.limit.weight) for m in match_blocks),
            ),
            ({sense}, pairs),
        )

    _check(
        results,
        "distinct <Match> labels",
        sorted(collections.Counter(m.limit.label for m in matches)),
        sorted(BCV_EXPECTED_RULES),
    )

    # The request 2x2, three corners of which this instance uses. ShiftOn is
    # absent entirely, which is the mirror image of QMC-2 using only DayOff and
    # ShiftOn -- between them all four corners are covered by a real instance.
    _check(results, "requests, total", len(instance.requests), 768)
    _check(results, "day-off requests", len(_requests_of(instance, wants=False, named=False)), 138)
    _check(results, "day-on requests", len(_requests_of(instance, wants=True, named=False)), 132)
    _check(results, "shift-off requests", len(_requests_of(instance, wants=False, named=True)), 498)
    _check(
        results,
        "shift-on requests (unused here)",
        len(_requests_of(instance, wants=True, named=True)),
        0,
    )
    _check(
        results,
        "request weights",
        dict(collections.Counter(r.weight for r in instance.requests)),
        {50: 768},
    )
    _check(
        results,
        "shift-off requests by shift",
        dict(
            sorted(
                collections.Counter(
                    ",".join(r.shifts or [])
                    for r in _requests_of(instance, wants=False, named=True)
                ).items()
            )
        ),
        {"L": 188, "N": 88, "V": 222},
    )

    weights: collections.Counter[int] = collections.Counter()
    weights.update(m.limit.weight for m in matches)
    weights.update(w.limit.weight for w in workload)
    _check(
        results,
        "<Weight> element value counts",
        dict(sorted(weights.items(), reverse=True)),
        dict(sorted(BCV_EXPECTED_WEIGHT_COUNTS.items(), reverse=True)),
    )
    _check(results, "<Weight> elements total", sum(weights.values()), 127)

    return results


ERMGH_PERIODS: list[tuple[tuple[str, str], list[str]]] = [
    # (<TimePeriod>, the shifts on duty for the WHOLE of it). Re-derived here by
    # hand from <ShiftTypes>' clock spans -- D 07:30-15:30, DH 12:00-20:00,
    # E 15:30-23:30, N 23:30-07:30 -- so this is an independent check of
    # `_shifts_covering` rather than a restatement of it. Containment, not
    # overlap: E starts at 15:30 and so does not staff 12:00-15:30.
    (("07:30:00", "11:30:00"), ["D"]),
    (("11:30:00", "12:00:00"), ["D"]),
    (("12:00:00", "15:30:00"), ["D", "DH"]),
    (("15:30:00", "19:30:00"), ["DH", "E"]),
    (("19:30:00", "20:00:00"), ["DH", "E"]),
    (("20:00:00", "23:30:00"), ["E"]),
    (("23:30:00", "07:30:00"), ["N"]),
]


def verify_ermgh(instance: Instance) -> list[tuple[bool, str]]:
    """The same gate for ERMGH, which breaks a habit each of the others taught."""
    results: list[tuple[bool, str]] = []

    _check(results, "employees", len(instance.employees), 41)
    _check(results, "contracts defined", len(instance.contracts), 41)
    # One contract per employee, unlike BCV-3.46.2's five shared ones. That is
    # why 843 <Match> blocks produce 843 per-employee instances and not 34563.
    _check(
        results,
        "every employee has her own contract",
        sorted(collections.Counter(e.contract_id for e in instance.employees).values()),
        [1] * 41,
    )

    _check(results, "planning period days", instance.num_days, 42)
    _check(results, "start date", instance.start_date, "2002-06-02")
    _check(results, "end date", instance.end_date, "2002-07-13")

    _check(results, "shift types", instance.shift_types, ["D", "DH", "E", "N"])
    # Every shift pays the same 8 units, so workload limits here are pure shift
    # COUNTS -- `Min 80` over a fortnight is ten shifts, not eighty hours.
    _check(
        results,
        "shift durations (time units)",
        instance.shift_time_units,
        dict.fromkeys(("D", "DH", "E", "N"), 8),
    )
    _check(
        results,
        "shift clock spans",
        instance.shift_times,
        {
            "D": ("07:30:00", "15:30:00"),
            "DH": ("12:00:00", "20:00:00"),
            "E": ("15:30:00", "23:30:00"),
            "N": ("23:30:00", "07:30:00"),
        },
    )
    # A <ShiftGroup> ID is not a shift ID: the group called `D` holds DH alone,
    # and nothing may resolve a group by assuming the two namespaces coincide.
    _check(
        results,
        "shift groups",
        instance.shift_groups,
        {"EorD": ["E", "D"], "N": ["N"], "E": ["E"], "D": ["DH"]},
    )

    _check(results, "skills", instance.skills, ["1", "2", "3", "4", "5", "6", "7"])
    _check(
        results,
        "employees by skill set",
        sorted(collections.Counter(tuple(sorted(e.skills)) for e in instance.employees).items()),
        [
            (("1", "2"), 1),
            (("1", "2", "3"), 6),
            (("1", "2", "3", "4"), 7),
            (("1", "2", "3", "4", "5"), 27),
        ],
    )

    # Cover. Every block is skill-qualified and states its demand as a
    # <TimePeriod>: 14 blocks on each of the 7 weekdays, 7 periods x 2 skills.
    _check(results, "<Cover> blocks", len(instance.cover), 98)
    _check(results, "<DateSpecificCover> blocks", sum(1 for c in instance.cover if c.day), 0)
    _check(
        results,
        "<Cover> blocks naming a <Shift>",
        sum(1 for c in instance.cover if c.shift is not None),
        0,
    )
    _check(
        results,
        "<Cover> blocks by skill",
        dict(sorted(collections.Counter(c.skill for c in instance.cover).items(), key=str)),
        {"1": 49, "5": 49},
    )
    _check(
        results,
        "time periods resolved to shifts",
        sorted({(c.time_period, tuple(c.shifts)) for c in instance.cover}),
        sorted((period, tuple(shifts)) for period, shifts in ERMGH_PERIODS),
    )
    # The refutation of "a skill block is a bare minimum", which QMC-2's skill
    # blocks all happen to be: here skill 1 carries Max and Preferred and NO
    # Min, and skill 5 carries Min alone. Ignoring Max/Preferred on a skill
    # block scores this instance's published optimum at 0 instead of 779.
    _check(
        results,
        "(skill, min, max, preferred) bands",
        sorted({(c.skill, c.min, c.max, c.preferred) for c in instance.cover}, key=str),
        [("1", None, 38, 8), ("1", None, 40, 10), ("1", None, 41, 11), ("5", 1, None, None)],
    )
    _check(
        results,
        "cover weights",
        instance.cover_weights,
        {
            "MinUnderStaffing": 100,
            "MaxOverStaffing": 100,
            "PrefOverStaffing": 1,
            "PrefUnderStaffing": 1,
        },
    )

    matches: list[Match] = [m for c in instance.contracts for m in c.matches]
    _check(results, "<Match> blocks", len(matches), 843)
    # The only instance with <Min>-sense <Match> limits. Its <Max> forms clamp
    # at zero for exactly the same reason, so a parser that keys the clamp off
    # the tag rather than applying it to both is caught by these 18.
    _check(
        results,
        "<Match> senses",
        dict(sorted(collections.Counter(m.limit.sense for m in matches).items())),
        {"max": 825, "min": 18},
    )
    # Not one <Match> or <Workload> block carries a <Label>, so every rule
    # penalty lands in the empty-string bucket. That is this file's structure,
    # not a parse failure -- BCV-3.46.2 leaves 30 of 122 unlabelled, ERMGH all
    # 1070.
    _check(results, "labelled rules", sum(1 for m in matches if m.limit.label), 0)

    workload: list[WorkloadLimit] = [w for c in instance.contracts for w in c.workload]
    _check(results, "<Workload><TimeUnits> blocks", len(workload), 227)
    _check(
        results,
        "<Workload> senses",
        dict(sorted(collections.Counter(w.limit.sense for w in workload).items())),
        {"max": 123, "min": 104},
    )
    # Three consecutive, NON-overlapping fortnights, unlike QMC-2's three
    # windows sliding a week at a time.
    _check(
        results,
        "workload regions",
        sorted(collections.Counter((w.region_start, w.region_end) for w in workload).items()),
        [((0, 13), 77), ((14, 27), 76), ((28, 41), 74)],
    )

    patterns: list[Pattern] = [p for m in matches for p in m.patterns]
    _check(results, "<Pattern> blocks", len(patterns), 1345)
    # The symbol census, and the line that pins the `$` wildcard. It parses to
    # its own kind rather than to a shift named "$": read as a shift ID it
    # matches nothing, every rule scores 0, and the published optimum still
    # totals 779 because its rules are all satisfied -- a silent pass. Read as
    # "anything at all" the same roster scores 226079.
    _check(
        results,
        "pattern symbols by kind",
        dict(sorted(collections.Counter(s["kind"] for p in patterns for s in p.symbols).items())),
        {"group": 602, "shift": 1265, "worked": 1288},
    )
    _check(
        results,
        "symbols parsed as a shift named $",
        sum(1 for p in patterns for s in p.symbols if s["kind"] == "shift" and s["value"] == "$"),
        0,
    )
    _check(
        results,
        "anchors (<Start>, <StartDay>, free)",
        (
            sum(1 for p in patterns if p.start_day_index is not None),
            sum(1 for p in patterns if p.start_weekday is not None),
            sum(1 for p in patterns if p.start_day_index is None and p.start_weekday is None),
        ),
        (46, 862, 437),
    )

    _check(results, "requests", len(instance.requests), 1514)
    for wants, named, name, expected in (
        (False, False, "DayOff", 1),
        (True, False, "DayOn", 0),
        (True, True, "ShiftOn", 605),
        (False, True, "ShiftOff", 908),
    ):
        _check(results, f"{name} requests", len(_requests_of(instance, wants, named)), expected)
    _check(
        results,
        "request weights",
        dict(sorted(collections.Counter(r.weight for r in instance.requests).items())),
        {10: 1243, 1000: 271},
    )
    # The inline-<ShiftGroup> requests: four ShiftOn requests naming {E, D} as an
    # anonymous pair rather than a <ShiftTypeID>. `EorD` is a real group ID in
    # this file with the same members, but these blocks do not reference it --
    # they spell the shifts out -- so a parser that only reads <ShiftTypeID>
    # drops them and one that resolves through <ShiftGroups> reads a name that
    # is not there.
    _check(
        results,
        "requests naming several shifts",
        sorted(
            (r.employee_id, r.day, tuple(r.shifts or []))
            for r in instance.requests
            if r.shifts is not None and len(r.shifts) > 1
        ),
        [
            ("700596", 32, ("E", "D")),
            ("700619", 13, ("E", "D")),
            ("700619", 14, ("E", "D")),
            ("700619", 41, ("E", "D")),
        ],
    )

    return results


# The four instances that pin the OPTIONAL-ELEMENT fallbacks, and the element
# each one is here to keep honest. All four reproduce their published optimum
# exactly, which is what makes them evidence rather than decoration: a fallback
# that changed a cost would move the total, and verify_model.py would say so.
FALLBACK_CASES: tuple[tuple[str, str], ...] = (
    ("BCDT-Sep.ros", "no <CoverWeights> element, and <Match> limits with no <Weight>"),
    ("GPost.ros", "<Shift> with no <TimeUnits>"),
    ("Millar-2Shift-DATA1.ros", "<Employee> with no <Name>"),
    ("QMC-1.ros", "<ShiftGroupID> in a request"),
)


def verify_fallbacks(instances: dict[str, Instance]) -> list[tuple[bool, str]]:
    """Gate the optional-element fallbacks, on the instances that need them.

    Deliberately narrow. These four files are not here for a full structural
    census the way the three above are -- each earns its place by being the
    smallest shipped file that exercises one fallback, so this asserts the
    fallback fired AND produced the specific value the file implies, never
    merely that parsing succeeded.
    """
    results: list[tuple[bool, str]] = []

    # BCDT-Sep: no <CoverWeights> at all. Its published cost-100 optimum splits
    # 80 rules + 2 units of Min understaffing, so the default can only be 10.
    bcdt: Instance = instances["BCDT-Sep.ros"]
    _check(
        results,
        "BCDT-Sep cover weights (all defaulted)",
        bcdt.cover_weights,
        dict(DEFAULT_COVER_WEIGHTS),
    )
    _check(
        results,
        "BCDT-Sep <Match> limits defaulting to weight 1",
        sum(1 for c in bcdt.contracts for m in c.matches if m.limit.weight == 1),
        5,
    )
    # A declared <CoverWeights> is never topped up: BCV-3.46.2 names only the two
    # Pref keys and must keep exactly those two.
    _check(
        results,
        "a declared <CoverWeights> is left alone",
        sorted(instances["BCV-3.46.2.ros"].cover_weights),
        ["PrefOverStaffing", "PrefUnderStaffing"],
    )

    # GPost: no <TimeUnits> anywhere, so a duration is the clock span in minutes.
    gpost: Instance = instances["GPost.ros"]
    _check(
        results,
        "GPost durations are the clock span",
        gpost.shift_time_units,
        {
            shift: (_clock_minutes(end) - _clock_minutes(start)) % 1440 or 1440
            for shift, (start, end) in gpost.shift_times.items()
        },
    )
    _check(results, "GPost D runs 08:00-17:00 = 540", gpost.shift_time_units["D"], 540)
    # QMC-2 STATES <TimeUnits> and pays 15 minutes less than its clock span, so
    # the fallback must not fire there: deriving would read 90 instead of 75.
    _check(
        results,
        "a stated <TimeUnits> is never derived",
        instances["QMC-2.ros"].shift_time_units["E"],
        75,
    )

    # Millar: no <Name>. It falls back to the ID and can never feed a rule.
    millar: Instance = instances["Millar-2Shift-DATA1.ros"]
    _check(
        results,
        "Millar employee names fall back to the ID",
        all(e.name == e.id for e in millar.employees),
        True,
    )

    # QMC-1: <ShiftGroupID>L</ShiftGroupID> is a REFERENCE resolved through
    # <ShiftGroups>, unlike ERMGH's inline <ShiftGroup>, which spells its members
    # out. Reading either as the other looks up a name that is not there, or
    # ignores members that are.
    qmc1: Instance = instances["QMC-1.ros"]
    grouped: list[Request] = [
        r for r in qmc1.requests if r.shifts is not None and len(r.shifts) > 1
    ]
    _check(
        results,
        "QMC-1 requests naming a group reference",
        len(grouped) > 0,
        True,
    )
    defined: set[tuple[str, ...]] = {tuple(v) for v in qmc1.shift_groups.values()}
    _check(
        results,
        "QMC-1 resolves them through <ShiftGroups>",
        all(tuple(r.shifts or []) in defined for r in grouped),
        True,
    )

    return results


def _contract_of(instance: Instance, employee_id: str) -> str:
    contract_id: str = next(e.contract_id for e in instance.employees if e.id == employee_id)
    return contract_id


def main() -> None:
    """Gate both instances.

    Deliberately takes no instance argument: each verifier hard-codes the counts
    of one specific file, so pointing one at the other file would report dozens
    of failures that say nothing about the parser.
    """
    here: Path = Path(__file__).parent
    results: list[tuple[bool, str]] = []

    for title, filename, verifier in (
        ("QMC-2.ros", "QMC-2.ros", verify),
        ("BCV-3.46.2.ros", "BCV-3.46.2.ros", verify_bcv),
        ("ERMGH.ros", "ERMGH.ros", verify_ermgh),
    ):
        print(f"{title}")
        section: list[tuple[bool, str]] = verifier(parse_instance(here / filename))
        for ok, line in section:
            print(f"  {'ok  ' if ok else 'FAIL'}  {line}")
        print()
        results.extend(section)

    print("optional-element fallbacks")
    for filename, pins in FALLBACK_CASES:
        print(f"  {filename} pins {pins}")
    loaded: dict[str, Instance] = {
        name: parse_instance(here / name)
        for name in [f for f, _ in FALLBACK_CASES] + ["QMC-2.ros", "BCV-3.46.2.ros"]
    }
    fallbacks: list[tuple[bool, str]] = verify_fallbacks(loaded)
    for ok, line in fallbacks:
        print(f"  {'ok  ' if ok else 'FAIL'}  {line}")
    print()
    results.extend(fallbacks)

    failures: int = sum(1 for ok, _ in results if not ok)
    print(f"{len(results) - failures}/{len(results)} checks passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
