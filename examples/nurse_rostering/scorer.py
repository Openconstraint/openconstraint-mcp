"""Independent scorer for a SchedulingPeriod-3.0 roster.

This recomputes every constraint and penalty from the parsed instance and a
finished roster. It takes no part in solving and shares no penalty code with
`model.py`, deliberately: a scorer derived from the model would reproduce the
model's misreadings and confirm the wrong number. The only way this file and the
model can agree is by both being right.

The roster is a plain grid: `roster[employee_id][day]` is a shift ID or "-".

Penalty formula, uniform across pattern rules and workload rules, with `n` the
observed quantity and `c` the threshold:

    <Max>  ->  max(0, n - c) * weight
    <Min>  ->  max(0, c - n) * weight

The `max(0, .)` clamp is present in BOTH forms and has nothing to do with which
tag was written; it only denies credit for over-satisfying a rule. Dropping it
lets an over-satisfied rule score a negative penalty and the objective runs off
to minus infinity.

Run standalone (the instance defaults to QMC-2.ros):
    uv run examples/nurse_rostering/scorer.py QMC-2.Solution.29.roster
    uv run examples/nurse_rostering/scorer.py BCV-3.46.2.Solution.894.roster BCV-3.46.2.ros
"""

from __future__ import annotations

import collections
import sys
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))

from parse_instance import (  # noqa: E402
    Contract,
    Instance,
    Match,
    Pattern,
    Symbol,
    parse_instance,
)

OFF: str = "-"

# The request 2x2, keyed by (wants, names a specific shift). Deliberately spelt
# out again here rather than imported from model.py: these strings are the keys
# the two independent breakdowns are compared on, so a shared table would make
# them agree by construction instead of by both being right.
REQUEST_NAMES: dict[tuple[bool, bool], str] = {
    (False, False): "DayOff",
    (True, False): "DayOn",
    (True, True): "ShiftOn",
    (False, True): "ShiftOff",
}

Roster = dict[str, list[str]]


@dataclass
class Breakdown:
    """Penalties grouped the way the deliverable asks for them."""

    by_label: dict[str, int] = field(default_factory=dict)
    by_cover_type: dict[str, int] = field(default_factory=dict)
    requests: dict[str, int] = field(default_factory=dict)
    violations: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            sum(self.by_label.values())
            + sum(self.by_cover_type.values())
            + sum(self.requests.values())
        )

    def add(self, bucket: dict[str, int], key: str, amount: int) -> None:
        if amount:
            bucket[key] = bucket.get(key, 0) + amount


def _clamped(sense: str, observed: int, threshold: int, weight: int) -> int:
    """The shared penalty formula. See the module docstring."""
    excess: int = observed - threshold if sense == "max" else threshold - observed
    return max(0, excess) * weight


def _symbol_matches(symbol: Symbol, assigned: str, shift_groups: dict[str, list[str]]) -> bool:
    kind: str = symbol["kind"]
    value: str = symbol["value"]
    if kind == "shift":
        # Covers both a concrete shift ID and the day-off symbol "-".
        return assigned == value
    if kind == "group":
        # The group ID is instance-defined; `All` happens to be {E, L, N} here,
        # which excludes "-", so a group symbol never matches a day off.
        return assigned in shift_groups[value]
    if kind == "notshift":
        # "anything except N" -- a day off included. Reading this as "a working
        # shift other than N" would under-count every `Min 2 consecutive 'N'`
        # violation that sits next to a rest day, which is most of them.
        return assigned != value
    if kind == "notgroup":
        # `notshift` one level up: anything outside the group, a day off
        # included. BCV-3.46.2 spells "free day" as <NotGroup>ON</NotGroup>
        # rather than <Shift>-</Shift>, so reading this as "a working shift
        # outside the group" makes every free-weekend rule unfalsifiable --
        # the symbol would never match the day off it exists to describe.
        return assigned not in shift_groups[value]
    raise ValueError(f"unknown symbol kind {kind!r}")


def _candidate_starts(
    pattern: Pattern, match: Match, num_days: int, first_weekday: int
) -> range | list[int]:
    """Days at which this pattern may begin, after both filters.

    Two independent filters stack:

    - the anchor: `<Start>N</Start>` pins the match to day N exactly (an absolute
      date, not "the beginning"), and `<StartDay>Saturday</StartDay>` allows any
      day falling on that weekday;
    - the region: the ENTIRE window must fit, so the last legal start is
      `region_end - len(symbols) + 1`, not `region_end`.
    """
    length: int = len(pattern.symbols)
    lowest: int = match.region_start
    highest: int = min(match.region_end, num_days - 1) - length + 1

    if pattern.start_day_index is not None:
        day: int = pattern.start_day_index
        return [day] if lowest <= day <= highest else []

    if pattern.start_weekday is not None:
        return [
            day
            for day in range(lowest, highest + 1)
            if (first_weekday + day) % 7 == pattern.start_weekday
        ]

    return range(lowest, highest + 1)


def count_pattern_hits(
    pattern: Pattern,
    match: Match,
    schedule: list[str],
    shift_groups: dict[str, list[str]],
    first_weekday: int,
) -> int:
    hits: int = 0
    for start in _candidate_starts(pattern, match, len(schedule), first_weekday):
        if all(
            _symbol_matches(symbol, schedule[start + offset], shift_groups)
            for offset, symbol in enumerate(pattern.symbols)
        ):
            hits += 1
    return hits


def score_employee_rules(
    contract: Contract,
    schedule: list[str],
    instance: Instance,
    first_weekday: int,
    employee_id: str,
    breakdown: Breakdown,
) -> None:
    """Score one employee's row: pattern rules and workload rules.

    Everything here reads exactly one employee's 28-day string. Cover is the only
    rule family that sums across employees, so rows are otherwise independent.
    """
    for match in contract.matches:
        # Hit counts of the several patterns in one <Match> are SUMMED, not
        # OR-ed. If two patterns could match the same window, that window is
        # charged twice -- so sum unconditionally rather than relying on the
        # patterns happening to be mutually exclusive in this instance.
        observed: int = sum(
            count_pattern_hits(pattern, match, schedule, instance.shift_groups, first_weekday)
            for pattern in match.patterns
        )
        penalty: int = _clamped(match.limit.sense, observed, match.limit.count, match.limit.weight)
        breakdown.add(breakdown.by_label, match.limit.label, penalty)
        if penalty:
            breakdown.violations.append(
                f"{employee_id}: {match.limit.label} -- {observed} matches "
                f"vs {match.limit.sense} {match.limit.count} = {penalty}"
            )

    for workload in contract.workload:
        # Hours come from <ShiftTypes><TimeUnits>, never from EndTime - StartTime.
        hours: int = sum(
            instance.shift_time_units[shift]
            for day in range(workload.region_start, min(workload.region_end, len(schedule) - 1) + 1)
            for shift in (schedule[day],)
            if shift != OFF
        )
        penalty = _clamped(workload.limit.sense, hours, workload.limit.count, workload.limit.weight)
        breakdown.add(breakdown.by_label, workload.limit.label, penalty)
        if penalty:
            breakdown.violations.append(
                f"{employee_id}: {workload.limit.label} -- {hours} tenths over days "
                f"{workload.region_start}-{workload.region_end} "
                f"vs max {workload.limit.count} = {penalty}"
            )


def score_cover(
    instance: Instance, roster: Roster, first_weekday: int, breakdown: Breakdown
) -> None:
    """Score coverage: the only place headcounts are summed across employees.

    Cover is specified per day of the week and expanded across the whole period.
    A block with a <Skill> constrains how many of that shift's staff hold the
    skill; a block without one constrains the total headcount. A nurse holding
    both skills satisfies both skill minima with her single assignment.

    A <DateSpecificCover> block names one absolute day and REPLACES the weekday
    block for that (day, shift) -- see `CoverBlock`. Scoring both would charge
    the difference between them twice over.
    """
    skills_of: dict[str, set[str]] = {e.id: set(e.skills) for e in instance.employees}
    # (day, shift) pairs a date-specific block speaks for. Empty for QMC-2,
    # which has no <DateSpecificCover> at all, so the loop below then reduces
    # to the plain weekday match.
    overridden: set[tuple[int, str]] = {
        (block.day, block.shift) for block in instance.cover if block.day is not None
    }

    for day in range(instance.num_days):
        weekday: int = (first_weekday + day) % 7
        for block in instance.cover:
            if block.day is not None:
                if block.day != day:
                    continue
            elif block.weekday != weekday or (day, block.shift) in overridden:
                continue

            on_shift: list[str] = [
                employee_id
                for employee_id, schedule in roster.items()
                if schedule[day] == block.shift
            ]

            if block.skill is not None:
                qualified: int = sum(1 for e in on_shift if block.skill in skills_of[e])
                shortfall: int = max(0, (block.min or 0) - qualified)
                penalty: int = shortfall * instance.cover_weights["MinUnderStaffing"]
                breakdown.add(breakdown.by_cover_type, "skill Min understaffing", penalty)
                if penalty:
                    breakdown.violations.append(
                        f"day {day} shift {block.shift}: {qualified} with {block.skill} "
                        f"vs min {block.min} = {penalty}"
                    )
                continue

            assigned: int = len(on_shift)
            if block.min is not None:
                penalty = max(0, block.min - assigned) * instance.cover_weights["MinUnderStaffing"]
                breakdown.add(breakdown.by_cover_type, "Min understaffing", penalty)
                if penalty:
                    breakdown.violations.append(
                        f"day {day} shift {block.shift}: {assigned} staff "
                        f"vs min {block.min} = {penalty}"
                    )
            if block.max is not None:
                penalty = max(0, assigned - block.max) * instance.cover_weights["MaxOverStaffing"]
                breakdown.add(breakdown.by_cover_type, "Max overstaffing", penalty)
                if penalty:
                    breakdown.violations.append(
                        f"day {day} shift {block.shift}: {assigned} staff "
                        f"vs max {block.max} = {penalty}"
                    )
            if block.preferred is not None:
                # The Preferred penalty applies INSIDE the [Min, Max] band too:
                # sitting at Min when Preferred is 3 costs (3 - Min), it is not free.
                under: int = max(0, block.preferred - assigned)
                over: int = max(0, assigned - block.preferred)
                breakdown.add(
                    breakdown.by_cover_type,
                    "Preferred understaffing",
                    under * instance.cover_weights["PrefUnderStaffing"],
                )
                breakdown.add(
                    breakdown.by_cover_type,
                    "Preferred overstaffing",
                    over * instance.cover_weights["PrefOverStaffing"],
                )
                if under or over:
                    breakdown.violations.append(
                        f"day {day} shift {block.shift}: {assigned} staff "
                        f"vs preferred {block.preferred}"
                    )


def score_requests(instance: Instance, roster: Roster, breakdown: Breakdown) -> None:
    """Charge every unmet request, over all four corners of the 2x2.

    A request names either a specific shift or "any shift at all", and either
    wants it or wants to avoid it. Whether the day is satisfied therefore
    reduces to one equality compared against `wants`; the four XML tags need no
    separate code paths. model.py builds the same 2x2 out of CP-SAT literals
    from scratch, and the two must land on the same number.
    """
    for request in instance.requests:
        assigned: str = roster[request.employee_id][request.day]
        # "any shift at all" is `assigned != OFF`; a named shift is equality.
        holds: bool = assigned != OFF if request.shift is None else assigned == request.shift
        if holds == request.wants:
            continue

        name: str = REQUEST_NAMES[request.wants, request.shift is not None]
        breakdown.add(
            breakdown.requests, f"{name} request (weight {request.weight})", request.weight
        )
        wanted: str = (
            (request.shift or "any shift") if request.wants else f"not {request.shift or 'to work'}"
        )
        breakdown.violations.append(
            f"{request.employee_id}: wanted {wanted} on day {request.day}, "
            f"got {assigned} = {request.weight}"
        )


def check_one_shift_per_day(roster: Roster) -> list[str]:
    """The one hard constraint that is not written in the file.

    A grid roster cannot express a double booking, so this only guards against a
    malformed roster reaching the scorer -- but it is the constraint most easily
    forgotten in the model, so the scorer states it explicitly rather than
    relying on the data structure to make it unrepresentable.
    """
    return [
        f"{employee_id} day {day}: {value!r} is not a single shift or a day off"
        for employee_id, schedule in roster.items()
        for day, value in enumerate(schedule)
        if not isinstance(value, str) or value == ""
    ]


def score(instance: Instance, roster: Roster) -> Breakdown:
    from datetime import date

    first_weekday: int = date.fromisoformat(instance.start_date).weekday()
    contracts: dict[str, Contract] = {c.id: c for c in instance.contracts}

    breakdown: Breakdown = Breakdown()
    breakdown.violations.extend(check_one_shift_per_day(roster))

    for employee in instance.employees:
        score_employee_rules(
            # Follow <ContractID>: employee P is scored against contract O.
            contracts[employee.contract_id],
            roster[employee.id],
            instance,
            first_weekday,
            employee.id,
            breakdown,
        )
    score_cover(instance, roster, first_weekday, breakdown)
    score_requests(instance, roster, breakdown)
    return breakdown


def read_roster_xml(path: Path, instance: Instance) -> Roster:
    """Read a published `.roster` solution file into the grid form."""
    root: ET.Element = ET.parse(path).getroot()
    roster: Roster = {e.id: [OFF] * instance.num_days for e in instance.employees}
    for employee in root.findall("Employee"):
        employee_id: str = employee.get("ID") or ""
        for assign in employee.findall("Assign"):
            day: int = int((assign.findtext("Day") or "").strip())
            shift: str = (assign.findtext("Shift") or "").strip()
            if shift and shift != OFF:
                roster[employee_id][day] = shift
    return roster


def read_roster_csv(path: Path, instance: Instance) -> Roster:
    """Read the employees x days CSV this example emits (header, ID column first).

    Validated rather than trusted, because the two malformed shapes that
    actually occur both used to reach the scoring code and fail obscurely there:
    a header-only file (what model.py writes when a run found no incumbent)
    surfaced as a bare `KeyError` from a contract lookup, and a truncated row
    silently shortened one employee's schedule, under-counting her pattern
    penalties before the cover pass raised IndexError.
    """
    roster: Roster = {}
    lines: list[str] = [line for line in path.read_text().splitlines() if line.strip()]
    for line in lines[1:]:
        cells: list[str] = [c.strip() for c in line.split(",")]
        roster[cells[0]] = cells[1 : instance.num_days + 1]

    expected: list[str] = [employee.id for employee in instance.employees]
    missing: list[str] = [key for key in expected if key not in roster]
    if missing:
        raise ValueError(f"{path.name}: no roster row for employee(s) {missing}")
    short: list[str] = [key for key in expected if len(roster[key]) != instance.num_days]
    if short:
        raise ValueError(
            f"{path.name}: employee(s) {short} do not have {instance.num_days} day columns"
        )
    return roster


def format_report(breakdown: Breakdown, instance: Instance) -> str:
    lines: list[str] = []
    lines.append(f"{'penalty group':<44}{'cost':>8}")
    lines.append("-" * 52)

    def section(title: str, bucket: dict[str, int]) -> None:
        if not bucket:
            return
        lines.append(f"{title}")
        for key in sorted(bucket, key=lambda k: (-bucket[k], k)):
            lines.append(f"  {key:<42}{bucket[key]:>8}")

    section("pattern & workload rules (by <Label>)", breakdown.by_label)
    section("coverage (by type)", breakdown.by_cover_type)
    section("requests", breakdown.requests)

    zero_labels: list[str] = sorted(
        {m.limit.label for c in instance.contracts for m in c.matches}
        | {w.limit.label for c in instance.contracts for w in c.workload} - set(breakdown.by_label)
    )
    zero_labels = [lbl for lbl in zero_labels if lbl not in breakdown.by_label]
    if zero_labels:
        lines.append("rules with zero penalty")
        for label in zero_labels:
            lines.append(f"  {label:<42}{0:>8}")

    lines.append("-" * 52)
    lines.append(f"{'TOTAL':<44}{breakdown.total:>8}")
    return "\n".join(lines)


def main() -> None:
    here: Path = Path(__file__).parent
    roster_arg: str = sys.argv[1] if len(sys.argv) > 1 else "QMC-2.Solution.29.roster"
    roster_path: Path = Path(roster_arg)
    if not roster_path.exists():
        roster_path = here / roster_arg

    # A roster only means something against the instance it was built for, and
    # there are now two. Scoring BCV's roster against QMC-2 would not fail
    # loudly -- read_roster_xml keys off the employee list, so it would return
    # 46 empty rows and report a confident number computed from nothing.
    instance: Instance = parse_instance(here / (sys.argv[2] if len(sys.argv) > 2 else "QMC-2.ros"))
    reader = read_roster_csv if roster_path.suffix == ".csv" else read_roster_xml
    roster: Roster = reader(roster_path, instance)

    breakdown: Breakdown = score(instance, roster)
    print(f"roster: {roster_path.name}\n")
    print(format_report(breakdown, instance))

    if breakdown.violations:
        print(f"\n{len(breakdown.violations)} individual violations:")
        for violation in sorted(breakdown.violations):
            print(f"  {violation}")

    shifts: collections.Counter[str] = collections.Counter(
        shift for schedule in roster.values() for shift in schedule if shift != OFF
    )
    print(f"\nassignments: {sum(shifts.values())} ({dict(sorted(shifts.items()))})")


if __name__ == "__main__":
    main()
