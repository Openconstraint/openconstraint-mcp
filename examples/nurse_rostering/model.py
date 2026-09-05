"""Reference CP-SAT script: nurse rostering (SchedulingPeriod-3.0).

Minimizes the weighted sum of all soft-constraint violations for a nurse
rostering instance. The format has no hard constraints at all -- a "hard" rule
is written as a soft one with a large weight -- so the objective is the whole
problem. Exactly one hard constraint is added, and it is the one the file does
NOT contain: an employee works at most one shift per day.

Modelling posture (first pass): every rule in the file is soft, at its true
weight. Nothing is hardened with `add_forbidden_assignments`, because hardening
turns "expensive" into INFEASIBLE and hides modelling bugs -- and for the
cheapest rules it is outright wrong, since QMC-2's published optimum violates
one of them. Pass --harden to enable the sound-but-lossy speedup described
below.

Structure worth knowing: `<CoverRequirements>` is the only thing that couples
two employees. Contracts, workload and requests each read a single row of the
employees x days matrix; cover sums a column. Drop cover and the instance falls
apart into independent single-nurse problems.

Three instances ship with this example and they are not interchangeable in
difficulty or in convention:

- QMC-2.ros -- 19 employees x 28 days x 3 shifts, 121 <Match> blocks, published
  proven optimum 29. Its weights form a 1000 / 100 / 1 ladder.
- BCV-3.46.2.ros -- 46 employees x 26 days x 3 shifts, 122 <Match> blocks over
  only 5 shared contracts (so ~1124 per-employee match instances, roughly nine
  times QMC-2's), published proven optimum 894. Its weights are a different
  vocabulary entirely: cover at 10000, requests at 50, rules from 1 to 1000.
  Reading 1000 as "de facto hard" here is a QMC-2 habit, not a format rule.
- ERMGH.ros -- 41 employees x 42 days x 4 shifts, 843 <Match> blocks over 41
  contracts (one per employee), published proven optimum 779. Every one of its
  98 cover blocks is skill-qualified and states its demand as a <TimePeriod>
  rather than a shift; its whole published optimum is cover cost, with every
  rule and every one of its 1514 requests satisfied.

--harden is QMC-2-only. It zeroes every penalty at weight >= 100, which is
sound there and nowhere else: BCV-3.46.2's cover sits at 10000 and ERMGH's
optimum is 779 of cover penalty at weight 1 alongside rules at weight 100, so
hardening either one cuts off its own optimum.

Run from the repository root:
    uv run examples/nurse_rostering/model.py --time-limit 300
    uv run examples/nurse_rostering/model.py --instance BCV-3.46.2.json --time-limit 300
    uv run examples/nurse_rostering/model.py --instance ERMGH.json --time-limit 300
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model, cp_model_helper

sys.path.insert(0, str(Path(__file__).parent))

from instance import (  # noqa: E402
    Contract,
    Employee,
    Instance,
    Limit,
    Match,
    Pattern,
    load_instance,
)
from roster import resolve_roster_path  # noqa: E402
from shift_literals import ShiftLiterals  # noqa: E402

OFF: str = "-"

# The request 2x2, keyed by (wants, names shifts). Written out again
# here rather than imported from scorer.py on purpose: these strings are the
# keys the model's breakdown and the scorer's are compared on, and a shared
# table would make the two agree by construction instead of by both being right.
REQUEST_NAMES: dict[tuple[bool, bool], str] = {
    (False, False): "DayOff",
    (True, False): "DayOn",
    (True, True): "ShiftOn",
    (False, True): "ShiftOff",
}

# Weight classes, and they are QMC-2's, not the format's. There, magnitude
# encodes hardness: 1000 is de facto hard (shift succession, consecutive-day
# caps, cover bands), 100 is the hour limits, 1 is a genuine preference, and
# since the optimum is 29 < 100 an optimal roster violates nothing at 100 or
# above. BCV-3.46.2 numbers its rules on a different scale entirely, so these
# two constants -- read only by --harden -- carry no meaning for it.
HARD_WEIGHT: int = 1000
WORKLOAD_WEIGHT: int = 100


@dataclass
class PenaltyTerm:
    """One penalty contribution, tagged so the objective can be broken down."""

    group: str  # "rule", "cover" or "request"
    key: str  # the <Label>, cover type, or request family
    expression: cp_model.LinearExpr
    weight: int


@dataclass
class Solution:
    status: str
    objective: int | None = None
    best_objective_bound: float | None = None
    wall_time: float = 0.0
    roster: dict[str, list[str]] = field(default_factory=dict)
    breakdown: dict[str, dict[str, int]] = field(default_factory=dict)


@dataclass(frozen=True)
class Options:
    """Everything the command line settles before any modelling happens.

    A typed record rather than the string-keyed dict this used to be. `solve()`
    reads every field, so a dict turned every read into a place where a typo
    surfaces as a KeyError at solve time instead of an error at the boundary --
    and it left the two path fields as bare strings, re-wrapped in `Path(...)`
    downstream. docs/cpsat-python.md asks the spine for a typed record across
    `solve()` for exactly this reason.
    """

    instance_path: Path
    time_limit: float
    workers: int
    seed: int
    harden: bool
    fix_roster: Path | None


def read_input() -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="QMC-2.json")
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--harden",
        action="store_true",
        help=(
            "Second-pass speedup: enforce the weight-1000 and weight-100 rules as "
            "hard constraints. Sound only because a single violation of either "
            "costs more than the known upper bound of 29, so no optimal solution "
            "is cut off. Never applied to the weight-1 rules, whose real semantics "
            "is 'you may violate this, it costs 1' -- the published optimum does. "
            "That argument is derived from QMC-2's weight ladder and its bound of "
            "29; it does not carry over to another instance unaided."
        ),
    )
    parser.add_argument(
        "--fix-roster",
        default=None,
        help=(
            "Pin every assignment variable to the roster in this file and solve "
            "trivially, so the model reports its own objective for a roster it "
            "did not choose. Pointed at the published optimum this is the "
            "sharpest available test of the model's penalty structure: a model "
            "missing a penalty scores that ground-truth roster below 29."
        ),
    )
    args = parser.parse_args()

    here: Path = Path(__file__).parent / "parsed"
    return Options(
        instance_path=here / args.instance,
        time_limit=args.time_limit,
        workers=args.workers,
        seed=args.seed,
        harden=args.harden,
        # Resolved through roster.py rather than against `here`: a JSON roster
        # lives in parsed/ but the sample CSV rosters sit at the example root,
        # so pinning `--fix-roster solution.csv` from the repository root needs
        # both roots tried. Left CWD-relative, or resolved against parsed/
        # alone, the invocations this file's docstring documents die with
        # FileNotFoundError instead of pinning a roster.
        fix_roster=None if args.fix_roster is None else resolve_roster_path(args.fix_roster),
    )


def parse_input(raw: Options) -> tuple[Instance, Options]:
    return load_instance(raw.instance_path), raw


class RosterModel:
    """Builds the CP-SAT model and keeps the handles needed to read it back."""

    def __init__(self, instance: Instance, harden: bool) -> None:
        self.instance: Instance = instance
        self.harden: bool = harden
        self.model: cp_model.CpModel = cp_model.CpModel()
        self.first_weekday: int = date.fromisoformat(instance.start_date).weekday()
        self.penalties: list[PenaltyTerm] = []

        # x[employee, day, shift]: the core 19 x 28 x 3 = 1596 booleans. There is
        # deliberately no skill subscript -- an employee's skills are fixed
        # attributes, not a decision. Skills enter only when counting coverage.
        self.x: dict[tuple[str, int, str], cp_model.IntVar] = {}
        # works[employee, day]: worked at all that day, i.e. the negation of "-".
        self.works: dict[tuple[str, int], cp_model.IntVar] = {}

        self._build_assignment_variables()
        # Composed, not inherited: the symbol vocabulary needs only the model and
        # the two variable tables, and all three model files encoded it
        # identically. See shift_literals.py for what is deliberately NOT shared.
        self.literals: ShiftLiterals = ShiftLiterals(
            self.model, self.x, self.works, instance.shift_types, instance.shift_groups
        )
        self._build_employee_rules()
        self._build_cover()
        self._build_requests()

    # -- variables ---------------------------------------------------------

    def _build_assignment_variables(self) -> None:
        instance: Instance = self.instance
        for employee in instance.employees:
            for day in range(instance.num_days):
                literals: list[cp_model.IntVar] = []
                for shift in instance.shift_types:
                    variable: cp_model.IntVar = self.model.new_bool_var(
                        f"x_{employee.id}_{day}_{shift}"
                    )
                    self.x[employee.id, day, shift] = variable
                    literals.append(variable)

                # THE one hard constraint that appears nowhere in the file.
                # Without it the solver cheerfully puts a nurse on E, L and N
                # the same day to satisfy three cover blocks with one person.
                self.model.add_at_most_one(literals)

                works: cp_model.IntVar = self.model.new_bool_var(f"w_{employee.id}_{day}")
                self.model.add_max_equality(works, literals)
                self.works[employee.id, day] = works

    # -- pattern machinery -------------------------------------------------

    def _candidate_starts(self, pattern: Pattern, match: Match) -> list[int]:
        """Days where this pattern may begin, after anchor AND region filtering.

        The whole window must fit inside the region, so the last legal start is
        `region_end - len(symbols) + 1`. Checking only the start against the
        region -- the usual mistake -- lets windows run past its end.
        """
        length: int = len(pattern.symbols)
        lowest: int = match.region_start
        highest: int = min(match.region_end, self.instance.num_days - 1) - length + 1

        if pattern.start_day_index is not None:
            day: int = pattern.start_day_index
            return [day] if lowest <= day <= highest else []
        if pattern.start_weekday is not None:
            return [
                day
                for day in range(lowest, highest + 1)
                if (self.first_weekday + day) % 7 == pattern.start_weekday
            ]
        return list(range(lowest, highest + 1))

    def _hit_variable(
        self, employee_id: str, literals: list[cp_model_helper.Literal]
    ) -> cp_model.IntVar:
        """A boolean that is true exactly when every literal in the window holds.

        Reified in both directions. Only `hit >= AND(literals)` is needed to make
        the objective a valid upper bound, but the reverse direction costs little
        and makes the per-label breakdown read back exactly rather than
        approximately -- which matters, because that breakdown is cross-checked
        against an independent scorer.
        """
        hit: cp_model.IntVar = self.model.new_bool_var(f"hit_{employee_id}_{len(literals)}")
        self.model.add_bool_and(literals).only_enforce_if(hit)
        self.model.add_bool_or([literal.Not() for literal in literals] + [hit])
        return hit

    def _add_clamped_penalty(
        self,
        group: str,
        key: str,
        observed: cp_model.LinearExprT,
        limit: Limit,
        observed_upper: int,
    ) -> None:
        """Add `max(0, observed - count) * weight` (or the Min mirror image).

        The clamp is realised as a non-negative variable bounded below by the
        excess. Because the variable carries a positive coefficient in a
        minimized objective, it settles exactly on `max(0, excess)` -- no
        `add_max_equality` needed. The clamp is what stops an over-satisfied rule
        from scoring a negative penalty and dragging the objective downwards.

        `observed_upper` is the largest value `observed` can take; the penalty's
        OWN bound follows from it and the sense together, because the two senses
        clamp opposite ends. A Max limit overshoots by at most
        `observed_upper - count`, while a Min limit is worst at `observed = 0`
        and needs room for `count`. Reusing the Max bound for a Min limit makes
        `penalty >= excess` unsatisfiable whenever `count` exceeds it, which
        reports the whole instance INFEASIBLE rather than expensive.
        """
        excess = observed - limit.count if limit.sense == "max" else limit.count - observed
        upper: int = observed_upper - limit.count if limit.sense == "max" else limit.count
        penalty: cp_model.IntVar = self.model.new_int_var(
            0, max(upper, 0), f"pen_{key}_{len(self.penalties)}"
        )
        self.model.add(penalty >= excess)

        if self.harden and limit.weight >= WORKLOAD_WEIGHT:
            self.model.add(penalty == 0)

        self.penalties.append(
            PenaltyTerm(group=group, key=key, expression=penalty, weight=limit.weight)
        )

    # -- rules acting on a single employee's row ---------------------------

    def _build_employee_rules(self) -> None:
        contracts: dict[str, Contract] = {c.id: c for c in self.instance.contracts}

        for employee in self.instance.employees:
            # Follow <ContractID>. Employee P uses contract O; assuming the
            # employee ID is the contract ID applies contract P, which nobody uses.
            contract: Contract = contracts[employee.contract_id]

            for match in contract.matches:
                # Hit counts across the several <Pattern> children are SUMMED.
                # OR-ing them would cap `Max 3 working weekends` at 1 and make the
                # rule unfalsifiable.
                hits: list[cp_model.IntVar] = []
                for pattern in match.patterns:
                    for start in self._candidate_starts(pattern, match):
                        literals: list[cp_model_helper.Literal] = [
                            self.literals.symbol_literal(employee.id, start + offset, symbol)
                            for offset, symbol in enumerate(pattern.symbols)
                        ]
                        hits.append(self._hit_variable(employee.id, literals))

                # Added even when `hits` is empty -- every window fell outside
                # the region. `sum([])` is 0, which is the correct observed
                # count, and a Min limit then charges its full `count * weight`
                # exactly as scorer.py does. Skipping the term instead drops a
                # penalty the scorer still reports, and checker.py rejects the
                # run for a mismatch with no visible cause.
                self._add_clamped_penalty(
                    "rule", match.limit.label, sum(hits), match.limit, len(hits)
                )

            for workload in contract.workload:
                days: range = range(
                    workload.region_start,
                    min(workload.region_end, self.instance.num_days - 1) + 1,
                )
                # Paid duration per shift comes from <ShiftTypes><TimeUnits>, in
                # tenths of an hour. Deriving it from EndTime - StartTime adds the
                # unpaid 15-minute break to every shift and invents violations on
                # rosters sitting exactly on the 750 boundary.
                hours = sum(
                    self.instance.shift_time_units[shift] * self.x[employee.id, day, shift]
                    for day in days
                    for shift in self.instance.shift_types
                )
                max_hours: int = len(days) * max(self.instance.shift_time_units.values())
                self._add_clamped_penalty(
                    "rule", workload.limit.label, hours, workload.limit, max_hours
                )

    # -- the one rule family that couples employees ------------------------

    def _build_cover(self) -> None:
        instance: Instance = self.instance
        weights: dict[str, int] = instance.cover_weights
        num_employees: int = len(instance.employees)
        # (day, shifts) pairs spoken for by a <DateSpecificCover> block, which
        # REPLACES the weekday block there rather than adding to it -- see
        # CoverBlock. Empty for QMC-2 and ERMGH, so the loop below then reduces
        # exactly to the plain weekday match it used to be.
        overridden: set[tuple[int, tuple[str, ...]]] = {
            (block.day, tuple(block.shifts)) for block in instance.cover if block.day is not None
        }

        for day in range(instance.num_days):
            # Cover is given per day of the week and expanded over the period.
            weekday: int = (self.first_weekday + day) % 7
            for block in instance.cover:
                if block.day is not None:
                    if block.day != day:
                        continue
                elif block.weekday != weekday or (day, tuple(block.shifts)) in overridden:
                    continue

                # A skill block is a sub-requirement of the SAME headcount, not
                # a separate shift: it counts the holders instead of everyone,
                # and carries the same Min/Max/Preferred vocabulary. A nurse
                # holding both skills satisfies both minima with her one
                # assignment -- summing over holders does that automatically.
                # `block.shifts` is one shift in QMC-2 and BCV-3.46.2 and the
                # set on duty throughout a <TimePeriod> in ERMGH; summing over
                # it double-counts nobody, because a nurse works one shift a day.
                staff: list[Employee] = [
                    employee
                    for employee in instance.employees
                    if block.skill is None or block.skill in employee.skills
                ]
                prefix: str = "" if block.skill is None else "skill "
                assigned = sum(
                    self.x[employee.id, day, shift] for employee in staff for shift in block.shifts
                )
                if block.min is not None:
                    self._add_cover_penalty(
                        f"{prefix}Min understaffing",
                        block.min - assigned,
                        weights["MinUnderStaffing"],
                        block.min,
                    )
                if block.max is not None:
                    self._add_cover_penalty(
                        f"{prefix}Max overstaffing",
                        assigned - block.max,
                        weights["MaxOverStaffing"],
                        num_employees,
                    )
                if block.preferred is not None:
                    # Charged INSIDE the [Min, Max] band as well: being at Min
                    # when Preferred is 3 costs (3 - Min), it is not free.
                    self._add_cover_penalty(
                        f"{prefix}Preferred understaffing",
                        block.preferred - assigned,
                        weights["PrefUnderStaffing"],
                        block.preferred,
                    )
                    self._add_cover_penalty(
                        f"{prefix}Preferred overstaffing",
                        assigned - block.preferred,
                        weights["PrefOverStaffing"],
                        num_employees,
                    )

    def _add_cover_penalty(
        self,
        key: str,
        excess: cp_model.LinearExprT,
        weight: int,
        upper: int,
    ) -> None:
        penalty: cp_model.IntVar = self.model.new_int_var(
            0, max(upper, 0), f"cov_{len(self.penalties)}"
        )
        self.model.add(penalty >= excess)
        if self.harden and weight >= WORKLOAD_WEIGHT:
            self.model.add(penalty == 0)
        self.penalties.append(
            PenaltyTerm(group="cover", key=key, expression=penalty, weight=weight)
        )

    # -- requests ----------------------------------------------------------

    def _build_requests(self) -> None:
        """Charge every unmet request, over all four corners of the 2x2.

        Each request reduces to one literal -- "worked at all" for a request
        naming no shift, "on one of these shifts" for one that names some. The
        penalty is that literal when the employee wanted to AVOID the
        assignment, and its complement when they wanted it, so the four XML tags
        need no separate code paths:

            wants=False, shifts=None -> works[e, d]          (DayOff)
            wants=True,  shifts=None -> 1 - works[e, d]      (DayOn)
            wants=True,  shifts=[X]  -> 1 - x[e, d, X]       (ShiftOn)
            wants=False, shifts=[X]  -> x[e, d, X]           (ShiftOff)

        A request naming several shifts -- ERMGH's four inline <ShiftGroup>
        ShiftOn requests -- takes the same two rows through a membership
        literal, which for a single shift is that shift's variable unchanged.

        scorer.py rebuilds the same table in plain Python from the roster alone.
        """
        for request in self.instance.requests:
            # Typed as the concrete IntVar both branches produce, not the wide
            # Literal alias: that alias admits a negated literal, which makes the
            # `1 - holds` arm of the ternary below a non-expression.
            holds: cp_model.IntVar = (
                self.works[request.employee_id, request.day]
                if request.shifts is None
                else self.literals.in_shifts_literal(
                    request.employee_id,
                    request.day,
                    request.shifts,
                    "".join(request.shifts),
                )
            )
            self.penalties.append(
                PenaltyTerm(
                    group="request",
                    key=(
                        f"{REQUEST_NAMES[request.wants, request.shifts is not None]} "
                        f"request (weight {request.weight})"
                    ),
                    expression=1 - holds if request.wants else holds,
                    weight=request.weight,
                )
            )

    def objective(self) -> cp_model.LinearExprT:
        return sum(term.weight * term.expression for term in self.penalties)


def solve(parsed: tuple[Instance, Options]) -> Solution:
    instance, options = parsed
    builder: RosterModel = RosterModel(instance, harden=options.harden)

    if options.fix_roster is not None:
        _pin_roster(builder, instance, options.fix_roster)

    builder.model.minimize(builder.objective())

    solver: cp_model.CpSolver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = options.time_limit
    solver.parameters.num_workers = options.workers
    solver.parameters.random_seed = options.seed
    solver.parameters.log_search_progress = False
    status: cp_model.CpSolverStatus = solver.solve(builder.model)

    status_map: dict[cp_model.CpSolverStatus, str] = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }
    solution: Solution = Solution(
        status=status_map.get(status, "error"), wall_time=solver.wall_time
    )

    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return solution

    solution.objective = int(round(solver.objective_value))
    solution.best_objective_bound = float(solver.best_objective_bound)
    solution.roster = {
        employee.id: [
            next(
                (s for s in instance.shift_types if solver.value(builder.x[employee.id, day, s])),
                OFF,
            )
            for day in range(instance.num_days)
        ]
        for employee in instance.employees
    }

    # The model's own view of where the cost went, read back from the penalty
    # variables. scorer.py recomputes the same thing from the roster alone; the
    # two must agree, and a disagreement means the objective has drifted from
    # the real scoring rules.
    breakdown: dict[str, dict[str, int]] = {"rule": {}, "cover": {}, "request": {}}
    for term in builder.penalties:
        cost: int = solver.value(term.expression) * term.weight
        if cost:
            breakdown[term.group][term.key] = breakdown[term.group].get(term.key, 0) + cost
    solution.breakdown = breakdown
    return solution


def _pin_roster(builder: RosterModel, instance: Instance, path: Path) -> None:
    """Freeze every assignment to a given roster.

    Only the roster READERS are borrowed from scorer.py and roster.py -- reading a file is not
    scoring, and the penalty logic on both sides stays independent, which is the
    whole reason the cross-check means anything.
    """
    from roster import load_roster
    from scorer import read_roster_csv

    roster: dict[str, list[str]]
    if path.suffix == ".csv":
        roster = read_roster_csv(path, instance)
    else:
        roster = load_roster(path)

    for employee in instance.employees:
        for day in range(instance.num_days):
            assigned: str = roster[employee.id][day]
            for shift in instance.shift_types:
                builder.model.add(
                    builder.x[employee.id, day, shift] == (1 if assigned == shift else 0)
                )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    return {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": solution.best_objective_bound,
        "wall_time_seconds": round(solution.wall_time, 2),
        "solution": {"roster": solution.roster, "breakdown": solution.breakdown},
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    raw: Options = read_input()
    parsed: tuple[Instance, Options] = parse_input(raw)
    solution: Solution = solve(parsed)
    # The roster goes out on stdout with everything else; this script writes no
    # files. The CSV write that used to sit here was removed rather than left
    # disabled: it wrote to a fixed path, so a run that found no incumbent left
    # the PREVIOUS run's roster on disk and `scorer.py` then scored that stale
    # file and printed a cost the reader naturally attributes to the run that
    # just failed. `scorer.py` still READS a roster CSV, for one written by hand.
    write_output(serialize_solution(solution))


if __name__ == "__main__":
    main()
