"""Parse a SchedulingPeriod-3.0 nurse rostering XML file into compact JSON.

The XML (119 KB for QMC-2) is highly repetitive; this collapses it into a flat
structure that the model and the scorer both consume, so neither ever touches
the XML again.

Four parsing traps this handles explicitly:

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
- A `<Cover>` block states its demand EITHER as a `<Shift>` or as a
  `<TimePeriod>`, never both. The second form is resolved here to the shifts on
  duty for the whole interval, so downstream code sees one shape. See
  `_shifts_covering`.

Three instances are parsed by this example and they exercise different corners
of the format: QMC-2 has skills, a Min/Max/Preferred cover band and only DayOff
and ShiftOn requests; BCV-3.46.2 has no skills at all, Preferred-only cover with
two date-specific overrides, `<NotGroup>` pattern symbols, and DayOff, DayOn and
ShiftOff requests but no ShiftOn; ERMGH states all of its cover over
`<TimePeriod>` intervals rather than shifts, is the only one with `<Min>`-sense
`<Match>` and `<Workload>` limits, and writes four ShiftOn requests as an inline
`<ShiftGroup>` of two shifts.

Run from the repository root:
    uv run examples/nurse_rostering/parse_instance.py QMC-2.ros
    uv run examples/nurse_rostering/parse_instance.py BCV-3.46.2.ros
    uv run examples/nurse_rostering/parse_instance.py ERMGH.ros
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))

from instance import (  # noqa: E402
    Contract,
    CoverBlock,
    Employee,
    Instance,
    Limit,
    Match,
    Pattern,
    Request,
    Symbol,
    WorkloadLimit,
)

WEEKDAY_NAMES: tuple[str, ...] = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)

# `<Shift>$</Shift>` is a wildcard, not a shift ID: "any working shift", a day
# off EXCLUDED. ERMGH writes 1288 of them and neither other instance has one.
# Its meaning is settled by the working-weekend idiom it is always used in --
# three patterns `-$`, `$-`, `$$` summed under one `Max 1` limit. Read as "any
# working shift" those three score exactly one hit per worked weekend and zero
# for a free one, which is the rule. Read as "anything at all" a FREE weekend
# matches all three and scores 3, making the rule fire hardest on the rosters it
# exists to reward.
WORKED_SYMBOL: str = "$"

# What a cover deviation costs when <CoverWeights> does not say. BCDT-Sep is the
# only instance that omits the element, and its published cost-100 optimum pins
# the value: its rules account for exactly 80 and its cover for two units of Min
# understaffing, so the weight can only be 10. That is one data point, not a
# documented format rule, which is why verify_parse.py gates it -- a second
# instance disagreeing must fail loudly rather than quietly rescale a cost.
#
# Applied ONLY when <CoverWeights> is missing outright, never key by key: across
# all 28 instances of this dialect no file declares the element and then omits a
# key its own <Cover> blocks need, so a partial fill would serve no instance
# while quietly overwriting an absence that is itself an asserted fact.
DEFAULT_COVER_WEIGHTS: dict[str, int] = {
    "MinUnderStaffing": 10,
    "MaxOverStaffing": 10,
    "PrefUnderStaffing": 10,
    "PrefOverStaffing": 10,
}

# The four request tags, as (element path, wants, names shifts). They differ only
# in those two flags, so one loop over this table replaces four copies of the
# same four-line comprehension -- and adding the two corners BCV-3.46.2 needs
# cannot leave the other two behind.
REQUEST_KINDS: tuple[tuple[str, bool, bool], ...] = (
    ("DayOffRequests/DayOff", False, False),
    ("DayOnRequests/DayOn", True, False),
    ("ShiftOnRequests/ShiftOn", True, True),
    ("ShiftOffRequests/ShiftOff", False, True),
)


def _text(element: ET.Element | None) -> str:
    if element is None or element.text is None:
        raise ValueError("expected an element with text content")
    return element.text.strip()


def _int(element: ET.Element | None) -> int:
    return int(_text(element))


def _optional_text(parent: ET.Element, tag: str, default: str) -> str:
    """Read an optional text child, or fall back."""
    child: ET.Element | None = parent.find(tag)
    return default if child is None else _text(child)


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
                # A limit with no <Weight> counts one per violation. Twenty-six
                # instances omit it; three of them -- BCDT-Sep, HED01 and HED01b
                # -- have published optima that this default reproduces exactly,
                # which is what settles it as 1 rather than "hard" or "free".
                weight=_optional_int(node, "Weight", 1),
                label="" if label_node is None else _text(label_node),
            )
    raise ValueError("limit block has neither <Max> nor <Min>")


def _parse_pattern(node: ET.Element, day_index: Callable[[str], int]) -> Pattern:
    symbols: list[Symbol] = []
    start_day_index: int | None = None
    start_weekday: int | None = None

    for child in node:
        tag: str = child.tag
        if tag == "Start":
            start_day_index = _int(child)
        elif tag == "StartDate":
            # The same absolute-day anchor as <Start>, written as a date instead
            # of an offset. Ikegami-2Shift-DATA1 is the only instance that uses
            # it, and it pins matches to specific Saturdays.
            start_day_index = day_index(_text(child))
        elif tag == "StartDay":
            start_weekday = WEEKDAY_NAMES.index(_text(child))
        elif tag == "Shift":
            value: str = _text(child)
            symbols.append(
                {"kind": "worked" if value == WORKED_SYMBOL else "shift", "value": value}
            )
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


def _parse_contract(node: ET.Element, last_day: int, day_index: Callable[[str], int]) -> Contract:
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
                    patterns=[_parse_pattern(p, day_index) for p in match_node.findall("Pattern")],
                )
            )

    return Contract(
        id=contract_id,
        label="" if label_node is None else _text(label_node),
        workload=workload,
        matches=matches,
    )


MINUTES_PER_DAY: int = 24 * 60


def _clock_minutes(clock: str) -> int:
    """ "15:30:00" -> 930. Seconds are always zero in these files."""
    hours, minutes, _seconds = clock.split(":")
    return int(hours) * 60 + int(minutes)


def _shifts_covering(period: tuple[str, str], shift_times: dict[str, tuple[str, str]]) -> list[str]:
    """The shift types on duty for the WHOLE of a `<TimePeriod>`.

    ERMGH states cover over clock intervals instead of shifts, so the headcount
    a block speaks about is "everyone whose shift spans this interval". The test
    is containment, not overlap: a shift that covers only part of the period
    does not staff it, and ERMGH's periods are cut exactly at shift boundaries
    so containment is never partial by accident.

    Both intervals wrap midnight -- N runs 23:30 to 07:30 and one period runs
    with it -- so each is normalised to an arc `[start, end)` with `end` pushed
    past 1440 when it wraps, and the period is then tried at the three offsets
    that can bring it alongside the shift.
    """
    period_start: int = _clock_minutes(period[0])
    period_end: int = _clock_minutes(period[1])
    if period_end <= period_start:
        period_end += MINUTES_PER_DAY

    covering: list[str] = []
    for shift_id, (start_clock, end_clock) in shift_times.items():
        shift_start: int = _clock_minutes(start_clock)
        shift_end: int = _clock_minutes(end_clock)
        if shift_end <= shift_start:
            shift_end += MINUTES_PER_DAY
        if any(
            shift_start <= period_start + offset and period_end + offset <= shift_end
            for offset in (-MINUTES_PER_DAY, 0, MINUTES_PER_DAY)
        ):
            covering.append(shift_id)
    return covering


def _parse_cover_block(
    cover: ET.Element,
    weekday: int | None,
    day: int | None,
    shift_times: dict[str, tuple[str, str]],
) -> CoverBlock:
    """Read one `<Cover>` element, under either kind of parent.

    Resolves the two ways a block names its headcount -- `<Shift>` and
    `<TimePeriod>` -- into the single `shifts` list every consumer reads.
    """
    shift_node: ET.Element | None = cover.find("Shift")
    period_node: ET.Element | None = cover.find("TimePeriod")
    if (shift_node is None) == (period_node is None):
        raise ValueError("a <Cover> block needs exactly one of <Shift> and <TimePeriod>")

    shift: str | None = None
    period: tuple[str, str] | None = None
    shifts: list[str]
    if period_node is None:
        shift = _text(shift_node)
        shifts = [shift]
    else:
        period = (_text(period_node.find("Start")), _text(period_node.find("End")))
        shifts = _shifts_covering(period, shift_times)
        if not shifts:
            # Silent zero cover, not a harmless empty list: the block would then
            # ask for staff nobody can supply and charge its Min every day.
            raise ValueError(f"no shift type is on duty throughout the period {period}")

    skill_node: ET.Element | None = cover.find("Skill")
    min_node: ET.Element | None = cover.find("Min")
    max_node: ET.Element | None = cover.find("Max")
    pref_node: ET.Element | None = cover.find("Preferred")
    return CoverBlock(
        weekday=weekday,
        shift=shift,
        shifts=shifts,
        skill=None if skill_node is None else _text(skill_node),
        min=None if min_node is None else _int(min_node),
        max=None if max_node is None else _int(max_node),
        preferred=None if pref_node is None else _int(pref_node),
        time_period=period,
        day=day,
    )


def _parse_cover(
    node: ET.Element,
    day_index: Callable[[str], int],
    shift_times: dict[str, tuple[str, str]],
) -> list[CoverBlock]:
    """Read both kinds of cover requirement into one list.

    `<DayOfWeekCover>` is expanded over every matching weekday downstream;
    `<DateSpecificCover>` names one absolute date and applies to it alone,
    overriding the weekday block for that (day, shift). See `CoverBlock`.
    """
    blocks: list[CoverBlock] = []
    for day_cover in node.findall("DayOfWeekCover"):
        weekday: int = WEEKDAY_NAMES.index(_text(day_cover.find("Day")))
        blocks.extend(
            _parse_cover_block(cover, weekday, None, shift_times)
            for cover in day_cover.findall("Cover")
        )
    for date_cover in node.findall("DateSpecificCover"):
        day: int = day_index(_text(date_cover.find("Date")))
        blocks.extend(
            _parse_cover_block(cover, None, day, shift_times)
            for cover in date_cover.findall("Cover")
        )
    return blocks


def _shift_duration(shift: ET.Element, start_clock: str, end_clock: str) -> int:
    """The shift's paid duration, in whatever unit the instance counts in.

    `<TimeUnits>` when the file states one, and NOTHING is derived in that case:
    QMC-2 pays 15 minutes less than every shift's clock span, so deriving there
    turns a roster sitting exactly on its 750 boundary into a phantom violation.
    Twenty-two of the benchmark's instances omit the tag entirely, and for those
    the clock span is the only duration the file offers. A shift running to
    midnight or past it wraps, so a zero span means a full day, never nothing.

    The two are therefore NOT interchangeable and the fallback is not a
    convenience: it fires only where the file is silent, and it changes the unit
    from the instance's own to minutes. Nothing may compare a duration across
    instances, which was already true -- QMC-2 counts tenths of an hour and
    BCV-3.46.2 counts something else again.
    """
    stated: ET.Element | None = shift.find("TimeUnits")
    if stated is not None:
        return _int(stated)
    span: int = (_clock_minutes(end_clock) - _clock_minutes(start_clock)) % MINUTES_PER_DAY
    return span or MINUTES_PER_DAY


def _request_shifts(request: ET.Element, shift_groups: dict[str, list[str]]) -> list[str]:
    """The shift IDs one Shift(On|Off) request names.

    Three spellings, and the two group-shaped ones are NOT the same thing:

      <ShiftTypeID>E</ShiftTypeID>          one shift, the common case
      <ShiftGroup><Shift>E</Shift>...</>    an INLINE, anonymous set (ERMGH)
      <ShiftGroupID>L</ShiftGroupID>        a REFERENCE into <ShiftGroups> (QMC-1)

    The inline form spells its members out and is read in place; the reference
    form names a group ID and must be resolved through the table. Reading either
    one as the other looks up a name that is not there, or ignores members that
    are. Any member satisfies the request.
    """
    group: ET.Element | None = request.find("ShiftGroup")
    if group is not None:
        return [_text(s) for s in group.findall("Shift")]
    reference: ET.Element | None = request.find("ShiftGroupID")
    if reference is not None:
        return shift_groups[_text(reference)]
    return [_text(request.find("ShiftTypeID"))]


def parse_instance(xml_path: Path) -> Instance:
    root: ET.Element = ET.parse(xml_path).getroot()

    start_date: str = _text(root.find("StartDate"))
    end_date: str = _text(root.find("EndDate"))
    num_days: int = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days + 1
    last_day: int = num_days - 1

    shift_types: list[str] = []
    shift_time_units: dict[str, int] = {}
    shift_times: dict[str, tuple[str, str]] = {}
    for shift in root.findall("ShiftTypes/Shift"):
        shift_id: str = shift.get("ID") or ""
        shift_types.append(shift_id)
        start_clock: str = _text(shift.find("StartTime"))
        end_clock: str = _text(shift.find("EndTime"))
        shift_time_units[shift_id] = _shift_duration(shift, start_clock, end_clock)
        shift_times[shift_id] = (start_clock, end_clock)

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
                # Display only, and absent in eleven instances. It never feeds
                # a rule, so falling back to the ID loses nothing; requiring it
                # would reject those files over a label.
                name=_optional_text(employee, "Name", employee.get("ID") or ""),
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
            shifts=_request_shifts(req, shift_groups) if names_shifts else None,
        )
        for path, wants, names_shifts in REQUEST_KINDS
        for req in root.findall(path)
    ]

    # A declared <CoverWeights> is taken EXACTLY as written, undeclared keys and
    # all -- BCV-3.46.2 names only the two Pref keys, and that absence is a real
    # fact about the file that verify_parse.py asserts. The fallback replaces the
    # whole block and only when the element is missing outright.
    cover_weights_node: ET.Element | None = root.find("CoverWeights")
    cover_weights: dict[str, int] = (
        dict(DEFAULT_COVER_WEIGHTS)
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
        shift_times=shift_times,
        shift_groups=shift_groups,
        contracts=[
            _parse_contract(c, last_day, day_index) for c in root.findall("Contracts/Contract")
        ],
        employees=employees,
        cover=[] if cover_node is None else _parse_cover(cover_node, day_index, shift_times),
        cover_weights=cover_weights,
        requests=requests,
    )


def main() -> None:
    here: Path = Path(__file__).parent
    xml_path: Path = here / (sys.argv[1] if len(sys.argv) > 1 else "QMC-2.ros")
    out_path: Path = here / f"{xml_path.stem}.json"

    instance: Instance = parse_instance(xml_path)
    out_path.write_text(instance.model_dump_json() + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
