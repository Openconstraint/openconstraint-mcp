"""Parse a SchedulingPeriod-3.0 nurse rostering XML file into compact JSON.

The XML (119 KB for QMC-2) is highly repetitive; this collapses it into a flat
structure that the model and the scorer both consume, so neither ever touches
the XML again.

Three parsing traps this handles explicitly:

- `<TimeUnits>` means two different things depending on its parent. Under
  `<ShiftTypes><Shift>` it is a bare number (the shift's paid duration, in
  instance-defined units); under `<Contract><Workload>` it is a structure
  holding a Max/Min limit plus a region. Matching on the tag name alone
  conflates them.
- `<Start>` and `<StartDay>` are *anchors*, not pattern symbols. They pin where
  a match may begin but contribute no day to the match window, so a pattern
  with a `<Start>` child is one symbol shorter than its child count suggests.
- `<DateSpecificCover>` overrides the `<DayOfWeekCover>` block for the same
  (day, shift) instead of stacking with it. See `CoverBlock`.

Two instances are parsed by this example and they exercise different corners of
the format: QMC-2 has skills, a Min/Max/Preferred cover band and only DayOff and
ShiftOn requests; BCV-3.46.2 has no skills at all, Preferred-only cover with two
date-specific overrides, `<NotGroup>` pattern symbols, and DayOff, DayOn and
ShiftOff requests but no ShiftOn.

Run from the repository root:
    uv run examples/nurse_rostering/parse_instance.py QMC-2.ros data_qmc2.json
    uv run examples/nurse_rostering/parse_instance.py BCV-3.46.2.ros data_bcv.json
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# A pattern symbol is one of:
#   {"kind": "shift",    "value": "E"}   -- that day is exactly shift E
#   {"kind": "shift",    "value": "-"}   -- that day is a day off
#   {"kind": "group",    "value": "All"} -- that day is any shift in group `All`
#   {"kind": "notshift", "value": "N"}   -- that day is anything except N,
#                                           a day off included
#   {"kind": "notgroup", "value": "ON"}  -- that day is any shift NOT in group
#                                           `ON`, a day off included
Symbol = dict[str, str]

# The four request tags, as (element path, wants, carries a <ShiftTypeID>). They
# differ only in those two flags, so one loop over this table replaces four
# copies of the same four-line comprehension -- and adding the two corners
# BCV-3.46.2 needs cannot leave the other two behind.
REQUEST_KINDS: tuple[tuple[str, bool, bool], ...] = (
    ("DayOffRequests/DayOff", False, False),
    ("DayOnRequests/DayOn", True, False),
    ("ShiftOnRequests/ShiftOn", True, True),
    ("ShiftOffRequests/ShiftOff", False, True),
)


@dataclass
class Pattern:
    symbols: list[Symbol]
    # Anchor: exactly one of these is set, or neither (pattern floats freely).
    start_day_index: int | None = None  # <Start>N</Start>: absolute day N
    start_weekday: int | None = None  # <StartDay>Saturday</StartDay>: 0=Monday


@dataclass
class Limit:
    """A `<Max>`/`<Min>` threshold with its weight and human-readable label.

    The penalty is always clamped at zero -- `max(0, n - count)` for a Max limit
    and `max(0, count - n)` for a Min one. The clamp is unrelated to which tag
    was used; it only denies credit for over-satisfying.
    """

    sense: str  # "max" or "min"
    count: int
    weight: int
    label: str


@dataclass
class Match:
    """A pattern-counting rule: `sum of hits over all patterns` vs. a limit.

    Hit counts of the several `<Pattern>` children are SUMMED, not OR-ed. The
    region restricts the searched substring of the 28-day schedule, so the whole
    match window -- not merely its first day -- must lie inside it.
    """

    limit: Limit
    region_start: int
    region_end: int
    patterns: list[Pattern] = field(default_factory=list)


@dataclass
class WorkloadLimit:
    """A workload cap over one region, in the instance's own time units.

    QMC-2 counts tenths of an hour (E=75, L=75, N=100); BCV-3.46.2 counts
    something else entirely (N=10, L=8, V=7). Both are just <TimeUnits>, and
    nothing here converts between them.
    """

    limit: Limit
    region_start: int
    region_end: int


@dataclass
class Contract:
    id: str
    label: str
    workload: list[WorkloadLimit] = field(default_factory=list)
    matches: list[Match] = field(default_factory=list)


@dataclass
class Employee:
    id: str
    name: str
    contract_id: str
    skills: list[str] = field(default_factory=list)


@dataclass
class CoverBlock:
    """One coverage requirement for one shift, on a weekday or a single date.

    Without a skill: the total headcount on that shift must lie in [min, max],
    preferably exactly `preferred`. With a skill: at least `min` of the staff on
    that shift must hold it, and the block carries no max or preferred.

    `<DayOfWeekCover>` sets `weekday` and repeats every week; `<DateSpecificCover>`
    sets `day` and applies to that one date. Exactly one of the two is set.

    A date-specific block REPLACES the weekday block for the same (day, shift)
    rather than adding to it. BCV-3.46.2's published cost-894 optimum proves it:
    its day 7 is a Monday, whose weekday block prefers 6 on N while the
    date-specific block prefers 4, and the optimum staffs 4. At
    PrefUnder/PrefOverStaffing = 10000 a single unit of deviation costs more
    than the entire optimum, so scoring both blocks would put that roster at
    40894 instead of 894. The override is per (day, shift), not per day: day 7
    carries a date-specific block for N only, and its L and V still follow the
    Monday block, which the optimum also matches.
    """

    weekday: int | None  # 0 = Monday; None on a date-specific block
    shift: str
    skill: str | None
    min: int | None
    max: int | None
    preferred: int | None
    day: int | None = None  # absolute day offset; None on a weekday block


@dataclass
class Request:
    """One employee's wish about one day, as a 2x2 over `wants` x `shift`.

    The four XML request tags differ only in these two fields, so they collapse
    to one record rather than four near-identical lists:

        DayOff   (wants=False, shift=None)   ShiftOff (wants=False, shift="V")
        DayOn    (wants=True,  shift=None)   ShiftOn  (wants=True,  shift="E")

    QMC-2 uses only the DayOff/ShiftOn diagonal; BCV-3.46.2 uses the other three
    corners and no ShiftOn at all.
    """

    employee_id: str
    day: int
    weight: int
    wants: bool  # True: wants this assignment. False: wants to avoid it.
    shift: str | None = None  # a specific shift, or None for "any shift at all"


@dataclass
class Instance:
    id: str
    start_date: str
    end_date: str
    num_days: int
    skills: list[str]
    shift_types: list[str]
    # Paid duration per shift, in the instance's own units, read from
    # <ShiftTypes><Shift><TimeUnits>. Deliberately NOT derived from
    # EndTime - StartTime: in QMC-2 every shift runs 15 unpaid minutes longer on
    # the clock than it pays, and that gap turns a roster sitting exactly on
    # the 750 boundary into a phantom violation.
    shift_time_units: dict[str, int]
    shift_groups: dict[str, list[str]]
    contracts: list[Contract]
    employees: list[Employee]
    cover: list[CoverBlock]
    cover_weights: dict[str, int]
    requests: list[Request]


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        raise ValueError("expected an element with text content")
    return element.text.strip()


def _int(element: ET.Element | None) -> int:
    return int(_text(element))


def _optional_int(parent: ET.Element, tag: str, default: int) -> int:
    """Read an optional integer child.

    A missing `<RegionStart>` means day 0 and a missing `<RegionEnd>` means the
    last day of the period -- the region defaults to the whole schedule.
    """
    child: ET.Element | None = parent.find(tag)
    return default if child is None else _int(child)


def _parse_limit(container: ET.Element) -> Limit:
    """Read the single `<Max>` or `<Min>` child of a Match or TimeUnits block."""
    for sense in ("Max", "Min"):
        node: ET.Element | None = container.find(sense)
        if node is not None:
            label_node: ET.Element | None = node.find("Label")
            return Limit(
                sense=sense.lower(),
                count=_int(node.find("Count")),
                weight=_int(node.find("Weight")),
                label="" if label_node is None else _text(label_node),
            )
    raise ValueError("limit block has neither <Max> nor <Min>")


def _parse_pattern(node: ET.Element) -> Pattern:
    symbols: list[Symbol] = []
    start_day_index: int | None = None
    start_weekday: int | None = None

    for child in node:
        tag: str = child.tag
        if tag == "Start":
            start_day_index = _int(child)
        elif tag == "StartDay":
            start_weekday = WEEKDAY_NAMES.index(_text(child))
        elif tag == "Shift":
            symbols.append({"kind": "shift", "value": _text(child)})
        elif tag == "ShiftGroup":
            symbols.append({"kind": "group", "value": _text(child)})
        elif tag == "NotShift":
            symbols.append({"kind": "notshift", "value": _text(child)})
        elif tag == "NotGroup":
            # The group-level mirror of <NotShift>: "anything outside this
            # group", resolved through <ShiftGroups> at scoring time exactly as
            # <ShiftGroup> is. BCV-3.46.2 writes free days as <NotGroup>ON</...>
            # rather than <Shift>-</Shift>, so reading it as "some other working
            # shift" would silently zero out every weekend and free-day rule.
            symbols.append({"kind": "notgroup", "value": _text(child)})
        else:
            raise ValueError(f"unknown pattern element <{tag}>")

    if not symbols:
        raise ValueError("pattern has no symbols")
    return Pattern(symbols=symbols, start_day_index=start_day_index, start_weekday=start_weekday)


def _parse_contract(node: ET.Element, last_day: int) -> Contract:
    contract_id: str = node.get("ID") or ""
    label_node: ET.Element | None = node.find("Label")

    workload: list[WorkloadLimit] = []
    workload_node: ET.Element | None = node.find("Workload")
    if workload_node is not None:
        # Under <Workload>, every <TimeUnits> is a limit structure -- never the
        # bare shift-duration number that the same tag carries under <ShiftTypes>.
        for time_units in workload_node.findall("TimeUnits"):
            workload.append(
                WorkloadLimit(
                    limit=_parse_limit(time_units),
                    region_start=_optional_int(time_units, "RegionStart", 0),
                    region_end=_optional_int(time_units, "RegionEnd", last_day),
                )
            )

    matches: list[Match] = []
    patterns_node: ET.Element | None = node.find("Patterns")
    if patterns_node is not None:
        for match_node in patterns_node.findall("Match"):
            matches.append(
                Match(
                    limit=_parse_limit(match_node),
                    region_start=_optional_int(match_node, "RegionStart", 0),
                    region_end=_optional_int(match_node, "RegionEnd", last_day),
                    patterns=[_parse_pattern(p) for p in match_node.findall("Pattern")],
                )
            )

    return Contract(
        id=contract_id,
        label="" if label_node is None else _text(label_node),
        workload=workload,
        matches=matches,
    )


def _parse_cover_block(cover: ET.Element, weekday: int | None, day: int | None) -> CoverBlock:
    """Read one `<Cover>` element, under either kind of parent."""
    skill_node: ET.Element | None = cover.find("Skill")
    min_node: ET.Element | None = cover.find("Min")
    max_node: ET.Element | None = cover.find("Max")
    pref_node: ET.Element | None = cover.find("Preferred")
    return CoverBlock(
        weekday=weekday,
        shift=_text(cover.find("Shift")),
        skill=None if skill_node is None else _text(skill_node),
        min=None if min_node is None else _int(min_node),
        max=None if max_node is None else _int(max_node),
        preferred=None if pref_node is None else _int(pref_node),
        day=day,
    )


def _parse_cover(node: ET.Element, day_index: Callable[[str], int]) -> list[CoverBlock]:
    """Read both kinds of cover requirement into one list.

    `<DayOfWeekCover>` is expanded over every matching weekday downstream;
    `<DateSpecificCover>` names one absolute date and applies to it alone,
    overriding the weekday block for that (day, shift). See `CoverBlock`.
    """
    blocks: list[CoverBlock] = []
    for day_cover in node.findall("DayOfWeekCover"):
        weekday: int = WEEKDAY_NAMES.index(_text(day_cover.find("Day")))
        blocks.extend(
            _parse_cover_block(cover, weekday, None) for cover in day_cover.findall("Cover")
        )
    for date_cover in node.findall("DateSpecificCover"):
        day: int = day_index(_text(date_cover.find("Date")))
        blocks.extend(_parse_cover_block(cover, None, day) for cover in date_cover.findall("Cover"))
    return blocks


def parse_instance(xml_path: Path) -> Instance:
    root: ET.Element = ET.parse(xml_path).getroot()

    start_date: str = _text(root.find("StartDate"))
    end_date: str = _text(root.find("EndDate"))
    num_days: int = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    last_day: int = num_days - 1

    shift_types: list[str] = []
    shift_time_units: dict[str, int] = {}
    for shift in root.findall("ShiftTypes/Shift"):
        shift_id: str = shift.get("ID") or ""
        shift_types.append(shift_id)
        shift_time_units[shift_id] = _int(shift.find("TimeUnits"))

    shift_groups: dict[str, list[str]] = {}
    for group in root.findall("ShiftGroups/ShiftGroup"):
        # `All` is an instance-defined group ID, not a keyword: resolve it here
        # rather than hardcoding "any working shift" at the point of use.
        shift_groups[group.get("ID") or ""] = [_text(s) for s in group.findall("Shift")]

    employees: list[Employee] = []
    for employee in root.findall("Employees/Employee"):
        skills_node: ET.Element | None = employee.find("Skills")
        employees.append(
            Employee(
                id=employee.get("ID") or "",
                name=_text(employee.find("Name")),
                # Follow <ContractID>; employee P uses contract O, so assuming
                # employee ID == contract ID silently applies the wrong rules.
                contract_id=_text(employee.find("ContractID")),
                skills=(
                    [] if skills_node is None else [_text(s) for s in skills_node.findall("Skill")]
                ),
            )
        )

    day_of: dict[str, int] = {}

    def day_index(iso_date: str) -> int:
        if iso_date not in day_of:
            day_of[iso_date] = (date.fromisoformat(iso_date) - date.fromisoformat(start_date)).days
        return day_of[iso_date]

    requests: list[Request] = [
        Request(
            employee_id=_text(req.find("EmployeeID")),
            day=day_index(_text(req.find("Date"))),
            weight=int(req.get("weight") or 1),
            wants=wants,
            shift=_text(req.find("ShiftTypeID")) if has_shift else None,
        )
        for path, wants, has_shift in REQUEST_KINDS
        for req in root.findall(path)
    ]

    cover_weights_node: ET.Element | None = root.find("CoverWeights")
    cover_weights: dict[str, int] = (
        {}
        if cover_weights_node is None
        else {child.tag: _int(child) for child in cover_weights_node}
    )

    cover_node: ET.Element | None = root.find("CoverRequirements")

    return Instance(
        id=root.get("ID") or "",
        start_date=start_date,
        end_date=end_date,
        num_days=num_days,
        skills=[s.get("ID") or "" for s in root.findall("Skills/Skill")],
        shift_types=shift_types,
        shift_time_units=shift_time_units,
        shift_groups=shift_groups,
        contracts=[_parse_contract(c, last_day) for c in root.findall("Contracts/Contract")],
        employees=employees,
        cover=[] if cover_node is None else _parse_cover(cover_node, day_index),
        cover_weights=cover_weights,
        requests=requests,
    )


def main() -> None:
    here: Path = Path(__file__).parent
    xml_path: Path = here / (sys.argv[1] if len(sys.argv) > 1 else "QMC-2.ros")
    out_path: Path = here / (sys.argv[2] if len(sys.argv) > 2 else "data_qmc2.json")

    instance: Instance = parse_instance(xml_path)
    out_path.write_text(json.dumps(asdict(instance), indent=1) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
