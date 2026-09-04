"""Bound-strengthening CP-SAT script: nurse rostering (SchedulingPeriod-3.0).

Same problem and the same breakdown keys as model.py, and deliberately the same
window encoding of the pattern rules -- model_regular.py replaced that encoding
with automata and measured it slower on both instances, so this file keeps what
won. What it adds is aimed at a different weakness.

On BCV-3.46.2, model.py --harden FINDS the published optimum of 894 inside 60
seconds and then cannot PROVE it: the objective bound parks around 880. Search
is not the bottleneck, the formulation's lower bound is. Three changes attack
that, and one is a portability fix rather than a speedup:

--upper-bound N   Replaces model.py's --harden. Given a roster of cost N you
                  already know exists, any rule whose weight exceeds N cannot be
                  violated by an optimal roster, because one violation alone
                  would cost more than N; so every penalty above that weight is
                  pinned to zero. --harden hardcodes QMC-2's 1000/100 ladder and
                  its comment says outright that the argument "does not carry
                  over to another instance unaided". This reads nothing but the
                  instance's own weights and the bound supplied, so it does:

                      --upper-bound 894  on BCV-3.46.2   --upper-bound 29 on QMC-2

                  On BCV-3.46.2 the cover half of that is decisive. Cover blocks
                  there carry only <Preferred> and both PrefUnderStaffing and
                  PrefOverStaffing weigh 10000, so an optimal roster staffs every
                  (day, shift) at exactly its preferred headcount: 78 soft
                  penalties become 78 cardinality equalities and the daily
                  headcount per shift turns into a constant.

--prove N         Post `objective <= N - 1` and ask for satisfiability instead of
                  optimality. INFEASIBLE is then a PROOF that nothing beats N,
                  which is the outcome wanted; a solution is a counterexample
                  that beats the bound believed optimal. Bounding the objective
                  up front lets the solver prune from the first node rather than
                  walking a bound upward, and that is usually the faster way to
                  close a known-good incumbent.

--no-redundant    Control arm for the implied aggregate constraints (per-day
                  headcount conservation and horizon-wide per-shift totals).
                  They cut no solutions, so this changes runtime only. Measured
                  on BCV-3.46.2 at 60s: dropping them cost 109 objective units.

Deliberately NOT here: a per-day "pigeonhole" cut on unmet DayOn requests. It
looks compelling -- six BCV days have 22 nurses requesting work against 18-20
slots, forcing exactly the 16 unmet requests that make up 800 of the 894 -- but
written as a linear inequality it reduces to `sum of works over the employees
who did NOT request that day >= 0`, which is vacuous. The LP already recovers
that 800 in one step from per-day conservation, and the bound of 880 sits above
it. The unproven 14 lives in the rule costs, not the requests.

Run from the repository root:
    uv run examples/nurse_rostering/model_bounds.py \
        --instance BCV-3.46.2.ros --upper-bound 894 --time-limit 600
    uv run examples/nurse_rostering/model_bounds.py \
        --instance BCV-3.46.2.ros --upper-bound 894 --prove 894 --time-limit 600
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

from parse_instance import (  # noqa: E402
    Contract,
    Instance,
    Limit,
    Match,
    Pattern,
    parse_instance,
)
from shift_literals import ShiftLiterals  # noqa: E402

OFF: str = "-"

# The request 2x2, keyed by (wants, names a specific shift). Written out here
# rather than imported from scorer.py for the reason model.py gives: these are
# the keys the two breakdowns are compared on, and a shared table would make the
# two agree by construction instead of by both being right.
REQUEST_NAMES: dict[tuple[bool, bool], str] = {
    (False, False): "DayOff",
    (True, False): "DayOn",
    (True, True): "ShiftOn",
    (False, True): "ShiftOff",
}


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
    proof: str | None = None


@dataclass(frozen=True)
class Options:
    """Everything the command line settles before any modelling happens."""

    instance_path: Path
    time_limit: float
    workers: int
    seed: int
    upper_bound: int | None
    prove: int | None
    redundant: bool
    fix_roster: Path | None


def read_input() -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="QMC-2.ros")
    parser.add_argument("--time-limit", type=float, default=300.0)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument(
        "--upper-bound",
        type=int,
        default=None,
        help=(
            "Cost of a roster already known to exist. Every penalty whose weight "
            "exceeds it is pinned to zero, because one violation would already "
            "cost more. Sound for any valid upper bound on any instance; off by "
            "default, which leaves every rule soft at its true weight."
        ),
    )
    parser.add_argument(
        "--prove",
        type=int,
        default=None,
        help=(
            "Ask whether any roster costs less than N, by posting "
            "`objective <= N - 1`. INFEASIBLE proves N optimal; a solution is a "
            "counterexample beating it. Independent of --upper-bound, which "
            "prunes by rule weight rather than by total cost -- pass both."
        ),
    )
    parser.add_argument(
        "--no-redundant",
        dest="redundant",
        action="store_false",
        help=(
            "Drop the implied aggregate constraints. They cut no solutions, so "
            "this changes runtime only -- which is the point of being able to "
            "turn them off."
        ),
    )
    parser.add_argument(
        "--fix-roster",
        default=None,
        help=(
            "Pin every assignment variable to the roster in this file and solve "
            "trivially, so the model reports its own objective for a roster it "
            "did not choose. Pointed at a published optimum this is the sharpest "
            "test of the penalty structure: a model missing a penalty scores "
            "that ground-truth roster below its known cost."
        ),
    )
    args = parser.parse_args()

    here: Path = Path(__file__).parent
    return Options(
        instance_path=here / args.instance,
        time_limit=args.time_limit,
        workers=args.workers,
        seed=args.seed,
        upper_bound=args.upper_bound,
        prove=args.prove,
        redundant=args.redundant,
        fix_roster=None if args.fix_roster is None else here / args.fix_roster,
    )


def parse_input(raw: Options) -> tuple[Instance, Options]:
    return parse_instance(raw.instance_path), raw


class RosterModel:
    """Builds the CP-SAT model and keeps the handles needed to read it back."""

    def __init__(self, instance: Instance, upper_bound: int | None, redundant: bool) -> None:
        self.instance: Instance = instance
        self.upper_bound: int | None = upper_bound
        self.redundant: bool = redundant
        self.model: cp_model.CpModel = cp_model.CpModel()
        self.first_weekday: int = date.fromisoformat(instance.start_date).weekday()
        self.penalties: list[PenaltyTerm] = []

        # x[employee, day, shift]: the core booleans. No skill subscript -- an
        # employee's skills are fixed attributes, not a decision. Skills enter
        # only when counting coverage.
        self.x: dict[tuple[str, int, str], cp_model.IntVar] = {}
        # works[employee, day]: worked at all that day, i.e. the negation of "-".
        self.works: dict[tuple[str, int], cp_model.IntVar] = {}
        # daily[day, shift]: headcount, named once and shared by every cover
        # penalty and every redundant cut. model.py rebuilds this sum up to four
        # times per cover block; naming it is what lets the implied constraints
        # below refer to the same quantity the objective is charged on.
        self.daily: dict[tuple[int, str], cp_model.IntVar] = {}

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
        if self.redundant:
            self._build_redundant()

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
                # Without it the solver cheerfully puts a nurse on three shifts
                # the same day to satisfy three cover blocks with one person.
                self.model.add_at_most_one(literals)

                works: cp_model.IntVar = self.model.new_bool_var(f"w_{employee.id}_{day}")
                self.model.add_max_equality(works, literals)
                self.works[employee.id, day] = works

    # -- shared helpers ----------------------------------------------------

    def _dominated(self, weight: int) -> bool:
        """True when one violation at this weight already costs more than a
        roster known to exist, so no optimal roster can afford it."""
        return self.upper_bound is not None and weight > self.upper_bound

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
        """A boolean true exactly when every literal in the window holds.

        Reified in both directions. Only `hit >= AND(literals)` is needed to make
        the objective a valid upper bound, but the reverse costs little and makes
        the per-label breakdown read back exactly rather than approximately --
        which matters, because that breakdown is cross-checked against an
        independent scorer.
        """
        hit: cp_model.IntVar = self.model.new_bool_var(f"hit_{employee_id}_{len(self.penalties)}")
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

        The clamp is a non-negative variable bounded below by the excess.
        Carrying a positive coefficient in a minimised objective it settles
        exactly on `max(0, excess)`, so no `add_max_equality` is needed. The
        clamp is what stops an over-satisfied rule from scoring a negative
        penalty and dragging the objective downwards.

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

        if self._dominated(limit.weight):
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
                # OR-ing them would cap `Max 3 working weekends` at 1 and make
                # the rule unfalsifiable.
                observed: list[cp_model.LinearExprT] = []
                observed_upper: int = 0
                for pattern in match.patterns:
                    starts: list[int] = self._candidate_starts(pattern, match)
                    observed_upper += len(starts)
                    if len(pattern.symbols) == 1:
                        # A one-day window IS its literal, so reifying a
                        # conjunction of one is pure waste. BCV-3.46.2 spends 44
                        # of its 122 blocks on `Max shift types per week`, every
                        # one of them this shape; model.py reifies all of them.
                        observed.extend(
                            self.literals.symbol_literal(employee.id, start, pattern.symbols[0])
                            for start in starts
                        )
                        continue
                    for start in starts:
                        literals: list[cp_model_helper.Literal] = [
                            self.literals.symbol_literal(employee.id, start + offset, symbol)
                            for offset, symbol in enumerate(pattern.symbols)
                        ]
                        observed.append(self._hit_variable(employee.id, literals))

                # Added even when `observed` is empty -- every window fell
                # outside the region. `sum([])` is 0, the correct observed count,
                # and a Min limit then charges its full `count * weight` exactly
                # as scorer.py does. Skipping the term drops a penalty the scorer
                # still reports, and checker.py rejects the run for a mismatch
                # with no visible cause.
                self._add_clamped_penalty(
                    "rule", match.limit.label, sum(observed), match.limit, observed_upper
                )

            for workload in contract.workload:
                days: range = range(
                    workload.region_start,
                    min(workload.region_end, self.instance.num_days - 1) + 1,
                )
                # Paid duration per shift comes from <ShiftTypes><TimeUnits>.
                # Deriving it from EndTime - StartTime adds the unpaid break to
                # every shift and invents violations on rosters sitting exactly
                # on the boundary.
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

        for day in range(instance.num_days):
            for shift in instance.shift_types:
                headcount: cp_model.IntVar = self.model.new_int_var(
                    0, num_employees, f"cover_{day}_{shift}"
                )
                self.model.add(
                    headcount
                    == sum(self.x[employee.id, day, shift] for employee in instance.employees)
                )
                self.daily[day, shift] = headcount

        # (day, shift) pairs spoken for by a <DateSpecificCover> block, which
        # REPLACES the weekday block there rather than adding to it. Empty for
        # QMC-2, so the loop below then reduces to the plain weekday match.
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
                prefix: str = "" if block.skill is None else "skill "
                assigned: cp_model.LinearExprT
                if block.skill is None:
                    # The precomputed per-shift headcount, so an ordinary
                    # one-shift block still reads a single variable.
                    assigned = sum(self.daily[day, shift] for shift in block.shifts)
                else:
                    assigned = sum(
                        self.x[employee.id, day, shift]
                        for employee in instance.employees
                        for shift in block.shifts
                        if block.skill in employee.skills
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
                        "Max overstaffing",
                        assigned - block.max,
                        weights["MaxOverStaffing"],
                        num_employees,
                    )
                if block.preferred is not None:
                    # Charged INSIDE the [Min, Max] band as well: being at Min
                    # when Preferred is 3 costs (3 - Min), it is not free.
                    self._add_cover_penalty(
                        "Preferred understaffing",
                        block.preferred - assigned,
                        weights["PrefUnderStaffing"],
                        block.preferred,
                    )
                    self._add_cover_penalty(
                        "Preferred overstaffing",
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
        if self._dominated(weight):
            # Pinning both halves of a Preferred pair to zero is what turns
            # cover into an equality: understaffing >= pref - assigned and
            # overstaffing >= assigned - pref, both zero, leaves assigned == pref.
            self.model.add(penalty == 0)
        self.penalties.append(
            PenaltyTerm(group="cover", key=key, expression=penalty, weight=weight)
        )

    # -- requests ----------------------------------------------------------

    def _build_requests(self) -> None:
        """Charge every unmet request, over all four corners of the 2x2.

        Each request reduces to one literal -- "worked at all" for a request
        naming no shift, "on exactly this shift" for one that does. The penalty
        is that literal when the employee wanted to AVOID the assignment, and
        its complement when they wanted it, so the four XML tags need no
        separate code paths:

            wants=False, shifts=None -> works[e, d]          (DayOff)
            wants=True,  shifts=None -> 1 - works[e, d]      (DayOn)
            wants=True,  shifts=[X]  -> 1 - x[e, d, X]       (ShiftOn)
            wants=False, shifts=[X]  -> x[e, d, X]           (ShiftOff)

        A request naming several shifts -- ERMGH's four inline <ShiftGroup>
        ShiftOn requests -- takes the same two rows through a membership
        literal, which for a single shift is that shift's variable unchanged.
        """
        for request in self.instance.requests:
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

    # -- implied constraints -----------------------------------------------

    def _build_redundant(self) -> None:
        """Aggregate cuts implied by constraints already posted.

        They remove no solution, so they cannot change the optimum; they exist
        because the LP relaxation cannot see a sum it was never handed. Both
        bite hardest once --upper-bound has pinned cover to an equality, since
        every `daily` variable is then a constant and these become statements
        about fixed totals -- on BCV-3.46.2, exactly 534 assignments split
        N=150, L=182, V=202, set against per-employee caps like `Max 6 N shifts`.
        """
        instance: Instance = self.instance

        for day in range(instance.num_days):
            # Headcount conservation. at_most_one makes each employee's row sum
            # a 0/1 selector, so the day's workers equal the day's assignments.
            # This is the single step the LP needs to see that a day with 22
            # DayOn requests and 18 slots must leave 4 of them unmet.
            self.model.add(
                sum(self.works[employee.id, day] for employee in instance.employees)
                == sum(self.daily[day, shift] for shift in instance.shift_types)
            )

        for shift in instance.shift_types:
            # Horizon-wide demand for one shift type, which is what the
            # per-employee shift caps are really competing over.
            self.model.add(
                sum(
                    self.x[employee.id, day, shift]
                    for employee in instance.employees
                    for day in range(instance.num_days)
                )
                == sum(self.daily[day, shift] for day in range(instance.num_days))
            )

    def objective(self) -> cp_model.LinearExprT:
        return sum(term.weight * term.expression for term in self.penalties)


def solve(parsed: tuple[Instance, Options]) -> Solution:
    instance, options = parsed
    builder: RosterModel = RosterModel(
        instance, upper_bound=options.upper_bound, redundant=options.redundant
    )

    if options.fix_roster is not None:
        _pin_roster(builder, instance, options.fix_roster)

    total: cp_model.LinearExprT = builder.objective()
    if options.prove is not None:
        # Asking "does anything beat N?" rather than "what is the cheapest?".
        # The objective stays minimised so a counterexample comes back as the
        # BEST counterexample rather than an arbitrary one.
        builder.model.add(total <= options.prove - 1)
    builder.model.minimize(total)

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

    if options.prove is not None:
        # INFEASIBLE means something different under --prove than it does
        # anywhere else in this file, and saying so here keeps a reader from
        # reporting a successful proof as a failed solve.
        solution.proof = {
            "infeasible": f"proved: no roster costs less than {options.prove}",
            "optimal": f"DISPROVED: a roster cheaper than {options.prove} exists",
            "feasible": f"DISPROVED: a roster cheaper than {options.prove} exists",
        }.get(solution.status, f"inconclusive within the time limit ({solution.status})")

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

    Only the roster READER is borrowed from scorer.py -- reading a file is not
    scoring, and the penalty logic on both sides stays independent, which is the
    whole reason the cross-check means anything.
    """
    from scorer import read_roster_csv, read_roster_xml

    reader = read_roster_csv if path.suffix == ".csv" else read_roster_xml
    roster: dict[str, list[str]] = reader(path, instance)

    for employee in instance.employees:
        for day in range(instance.num_days):
            assigned: str = roster[employee.id][day]
            for shift in instance.shift_types:
                builder.model.add(
                    builder.x[employee.id, day, shift] == (1 if assigned == shift else 0)
                )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": solution.status,
        "objective": solution.objective,
        "best_objective_bound": solution.best_objective_bound,
        "wall_time_seconds": round(solution.wall_time, 2),
        "solution": {"roster": solution.roster, "breakdown": solution.breakdown},
    }
    if solution.proof is not None:
        payload["proof"] = solution.proof
    return payload


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    raw: Options = read_input()
    parsed: tuple[Instance, Options] = parse_input(raw)
    solution: Solution = solve(parsed)
    write_output(serialize_solution(solution))


if __name__ == "__main__":
    main()
