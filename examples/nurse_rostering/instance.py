"""The normalized nurse rostering instance: 9 records, no XML.

`parse_instance.py` builds these records from a `.ros` file and writes them
out as JSON; every runtime script (`model.py`, `model_bounds.py`,
`model_regular.py`, `scorer.py`, `checker.py`, `shift_literals.py`,
`verify_model.py`) reads that JSON back through `load_instance` and never
touches XML.

Loading is `model_validate_json`, not `json.load()` + `model_validate()`:
under `ConfigDict(frozen=True, strict=True)` the JSON path restores
`dict[str, tuple[str, str]]` and `tuple[str, str] | None` as real tuples,
while python-mode `model_validate()` on the same lists raises
`ValidationError`.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict


class FrozenModel(BaseModel):
    """Base for the immutable records passed across this script's function boundary."""

    model_config = ConfigDict(frozen=True, strict=True)


# A pattern symbol is one of:
#   {"kind": "shift",    "value": "E"}   -- that day is exactly shift E
#   {"kind": "shift",    "value": "-"}   -- that day is a day off
#   {"kind": "worked",   "value": "$"}   -- that day is any WORKING shift
#   {"kind": "group",    "value": "All"} -- that day is any shift in group `All`
#   {"kind": "notshift", "value": "N"}   -- that day is anything except N,
#                                           a day off included
#   {"kind": "notgroup", "value": "ON"}  -- that day is any shift NOT in group
#                                           `ON`, a day off included
Symbol = dict[str, str]


class Pattern(FrozenModel):
    symbols: list[Symbol]
    # Anchor: exactly one of these is set, or neither (pattern floats freely).
    start_day_index: int | None = None  # <Start>N</Start>: absolute day N
    start_weekday: int | None = None  # <StartDay>Saturday</StartDay>: 0=Monday


class Limit(FrozenModel):
    """A `<Max>`/`<Min>` threshold with its weight and human-readable label.

    The penalty is always clamped at zero -- `max(0, n - count)` for a Max limit
    and `max(0, count - n)` for a Min one. The clamp is unrelated to which tag
    was used; it only denies credit for over-satisfying.
    """

    sense: str  # "max" or "min"
    count: int
    weight: int
    label: str


class Match(FrozenModel):
    """A pattern-counting rule: `sum of hits over all patterns` vs. a limit.

    Hit counts of the several `<Pattern>` children are SUMMED, not OR-ed. The
    region restricts the searched substring of the 28-day schedule, so the whole
    match window -- not merely its first day -- must lie inside it.
    """

    limit: Limit
    region_start: int
    region_end: int
    patterns: list[Pattern] = []


class WorkloadLimit(FrozenModel):
    """A workload cap over one region, in the instance's own time units.

    QMC-2 counts tenths of an hour (E=75, L=75, N=100); BCV-3.46.2 counts
    something else entirely (N=10, L=8, V=7). Both are just <TimeUnits>, and
    nothing here converts between them.
    """

    limit: Limit
    region_start: int
    region_end: int


class Contract(FrozenModel):
    id: str
    label: str
    workload: list[WorkloadLimit] = []
    matches: list[Match] = []


class Employee(FrozenModel):
    id: str
    name: str
    contract_id: str
    skills: list[str] = []


class CoverBlock(FrozenModel):
    """One coverage requirement, on a weekday or a single date.

    A block states its demand in one of two ways, and `shifts` is the resolution
    of both: `<Shift>N</Shift>` names one shift type, while ERMGH's
    `<TimePeriod>` names a clock interval and the block then counts everyone
    whose shift is on duty for the WHOLE of it. `shifts` is `[shift]` in the
    first case and the covering set in the second, so every consumer counts the
    same way and only this file knows the difference.

    Without a skill: the total headcount over `shifts` must lie in [min, max],
    preferably exactly `preferred`. With a skill: at least `min` of that
    headcount must hold it, and the block carries no max or preferred.

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
    shift: str | None  # the named shift type; None on a time-period block
    shifts: list[str]  # every shift this block counts -- see above
    skill: str | None
    min: int | None
    max: int | None
    preferred: int | None
    # ("15:30:00", "19:30:00") on a time-period block, None on a shift block.
    # Kept after resolution so a verifier can re-derive `shifts` independently.
    time_period: tuple[str, str] | None = None
    day: int | None = None  # absolute day offset; None on a weekday block


class Request(FrozenModel):
    """One employee's wish about one day, as a 2x2 over `wants` x `shift`.

    The four XML request tags differ only in these two fields, so they collapse
    to one record rather than four near-identical lists:

        DayOff   (wants=False, shifts=None)  ShiftOff (wants=False, shifts=["V"])
        DayOn    (wants=True,  shifts=None)  ShiftOn  (wants=True,  shifts=["E"])

    QMC-2 uses only the DayOff/ShiftOn diagonal; BCV-3.46.2 uses the other three
    corners and no ShiftOn at all; ERMGH uses three corners and has an empty
    <DayOnRequests> element.
    """

    employee_id: str
    day: int
    weight: int
    wants: bool  # True: wants this assignment. False: wants to avoid it.
    # The shift IDs named, or None for "any shift at all". A list rather than a
    # single ID because ERMGH writes four ShiftOn requests as an inline
    # <ShiftGroup> of two shifts; any member satisfies the request.
    shifts: list[str] | None = None


class Instance(FrozenModel):
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
    # Clock span per shift, read from <StartTime>/<EndTime>. Used for ONE thing:
    # resolving a <TimePeriod> cover block to the shifts on duty throughout it.
    # It is still never used to derive paid duration -- see shift_time_units.
    shift_times: dict[str, tuple[str, str]]
    shift_groups: dict[str, list[str]]
    contracts: list[Contract]
    employees: list[Employee]
    cover: list[CoverBlock]
    cover_weights: dict[str, int]
    requests: list[Request]


def load_instance(json_path: Path) -> Instance:
    return Instance.model_validate_json(json_path.read_text())
