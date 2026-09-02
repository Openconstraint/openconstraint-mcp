"""Reference CP-SAT script: nurse rostering (SchedulingPeriod-3.0 / QMC-2).

Minimizes the weighted sum of all soft-constraint violations for a nurse
rostering instance. The format has no hard constraints at all -- a "hard" rule
is written as a soft one with weight 1000 -- so the objective is the whole
problem. Exactly one hard constraint is added, and it is the one the file does
NOT contain: an employee works at most one shift per day.

Modelling posture (first pass): every rule in the file is soft, at its true
weight. Nothing is hardened with `add_forbidden_assignments`, because hardening
turns "expensive" into INFEASIBLE and hides modelling bugs -- and for the
weight-1 rules it is outright wrong, since the published optimum violates one of
them. Pass --harden to enable the sound-but-lossy speedup described below.

Structure worth knowing: `<CoverRequirements>` is the only thing that couples
two employees. Contracts, workload and requests each read a single row of the
19 x 28 matrix; cover sums a column. Drop cover and the instance falls apart
into 19 independent single-nurse problems.

Run from the repository root:
    uv run examples/nurse_rostering/model.py --time-limit 300
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
    Symbol,
    parse_instance,
)

OFF: str = "-"

# Weight classes. The format encodes hardness as magnitude: 1000 is de facto
# hard (shift succession, consecutive-day caps, cover bands), 100 is the hour
# limits, 1 is a genuine preference. Since the optimum is 29 < 100, an optimal
# roster violates nothing at 100 or above.
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
    reads five of these fields and `main()` a sixth, so a dict turned every read
    into a place where a typo surfaces as a KeyError at solve time instead of an
    error at the boundary -- and it left the two path fields as bare strings,
    re-wrapped in `Path(...)` downstream. docs/cpsat-python.md asks the spine for
    a typed record across `solve()` for exactly this reason.
    """

    instance_path: Path
    csv_path: Path
    time_limit: float
    workers: int
    seed: int
    harden: bool
    fix_roster: Path | None


def read_input() -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="QMC-2.ros")
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
            "is 'you may violate this, it costs 1' -- the published optimum does."
        ),
    )
    parser.add_argument("--csv", default="solution.csv")
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

    here: Path = Path(__file__).parent
    return Options(
        instance_path=here / args.instance,
        csv_path=here / args.csv,
        time_limit=args.time_limit,
        workers=args.workers,
        seed=args.seed,
        harden=args.harden,
        # Resolved against the script directory, exactly like --instance and
        # --csv above. Left CWD-relative it broke the invocation this file's own
        # docstring documents -- `uv run examples/nurse_rostering/model.py
        # --fix-roster QMC-2.Solution.29.roster` from the repository root died
        # with FileNotFoundError instead of pinning the published optimum.
        fix_roster=None if args.fix_roster is None else here / args.fix_roster,
    )


def parse_input(raw: Options) -> tuple[Instance, Options]:
    return parse_instance(raw.instance_path), raw


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

    def _symbol_literal(
        self, employee_id: str, day: int, symbol: Symbol
    ) -> cp_model_helper.Literal:
        """Map one pattern symbol on one day to a single boolean literal.

        Every symbol in this instance reduces to a literal, which is what keeps
        the encoding small: a match window is then a plain conjunction.
        """
        kind: str = symbol["kind"]
        value: str = symbol["value"]

        if kind == "shift":
            if value == OFF:
                return self.works[employee_id, day].Not()
            return self.x[employee_id, day, value]

        if kind == "group":
            members: list[str] = self.instance.shift_groups[value]
            if set(members) == set(self.instance.shift_types):
                # `All` = {E, L, N} here, so "any shift in the group" is exactly
                # "worked". Resolved through <ShiftGroups>, not assumed: another
                # instance may define a proper subset, handled below.
                return self.works[employee_id, day]
            in_group: cp_model.IntVar = self.model.new_bool_var(f"g_{employee_id}_{day}_{value}")
            self.model.add_max_equality(in_group, [self.x[employee_id, day, s] for s in members])
            return in_group

        if kind == "notshift":
            # "anything except N", a day off included -- so it is the negation of
            # the single shift literal, NOT "some other working shift".
            return self.x[employee_id, day, value].Not()

        raise ValueError(f"unknown symbol kind {kind!r}")

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
                            self._symbol_literal(employee.id, start + offset, symbol)
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

        for day in range(instance.num_days):
            # Cover is given per day of the week and expanded over all 28 days.
            weekday: int = (self.first_weekday + day) % 7
            for block in instance.cover:
                if block.weekday != weekday:
                    continue

                if block.skill is not None:
                    # A sub-requirement of the SAME shift's headcount, not a
                    # separate shift. A nurse holding both skills satisfies both
                    # minima with her one assignment -- summing over holders does
                    # that automatically, with no double-assignment.
                    qualified = sum(
                        self.x[employee.id, day, block.shift]
                        for employee in instance.employees
                        if block.skill in employee.skills
                    )
                    self._add_cover_penalty(
                        "skill Min understaffing",
                        (block.min or 0) - qualified,
                        weights["MinUnderStaffing"],
                        block.min or 0,
                    )
                    continue

                assigned = sum(
                    self.x[employee.id, day, block.shift] for employee in instance.employees
                )
                if block.min is not None:
                    self._add_cover_penalty(
                        "Min understaffing",
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
        if self.harden and weight >= WORKLOAD_WEIGHT:
            self.model.add(penalty == 0)
        self.penalties.append(
            PenaltyTerm(group="cover", key=key, expression=penalty, weight=weight)
        )

    # -- requests ----------------------------------------------------------

    def _build_requests(self) -> None:
        for request in self.instance.day_off_requests:
            # Violated exactly when the employee works at all that day.
            self.penalties.append(
                PenaltyTerm(
                    group="request",
                    key=f"DayOff request (weight {request.weight})",
                    expression=self.works[request.employee_id, request.day],
                    weight=request.weight,
                )
            )
        for request in self.instance.shift_on_requests:
            # Violated when the employee is not on that exact shift -- a day off
            # and a different shift are both violations.
            assert request.shift is not None
            self.penalties.append(
                PenaltyTerm(
                    group="request",
                    key=f"ShiftOn request (weight {request.weight})",
                    expression=1 - self.x[request.employee_id, request.day, request.shift],
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


# def write_csv(roster: dict[str, list[str]], num_days: int, path: Path) -> None:
#     header: str = "employee," + ",".join(f"d{day}" for day in range(num_days))
#     rows: list[str] = [header] + [
#         employee_id + "," + ",".join(schedule) for employee_id, schedule in sorted(roster.items())
#     ]
#     path.write_text("\n".join(rows) + "\n")


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
    # Written unconditionally. Skipping the write on a run that found no
    # incumbent left the PREVIOUS run's roster in place, and the follow-up step
    # problem.txt documents -- `scorer.py solution.csv` -- then scored that stale
    # roster and printed a cost the reader naturally attributes to the run that
    # just failed. An empty roster writes the header alone, which scorer.py's
    # reader rejects by name.
    # write_csv(solution.roster, parsed[0].num_days, raw.csv_path)
    write_output(serialize_solution(solution))


if __name__ == "__main__":
    main()
