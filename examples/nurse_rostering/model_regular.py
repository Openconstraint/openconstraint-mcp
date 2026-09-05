"""Automaton-based CP-SAT script: nurse rostering (SchedulingPeriod-3.0).

Same problem, same objective and the same breakdown keys as model.py, but a
different encoding of the pattern rules. model.py reifies one boolean per
(pattern, start day) window; this file routes every window it can through a
deterministic finite automaton over the employee's row, which is what a
`<Pattern>` sequence actually is.

Three encodings, chosen per pattern rather than per <Match> block, because a
block routinely mixes shapes -- `Max 4 consecutive working days` pairs a long
unanchored pattern with a short one pinned to the last day of the horizon:

  single-symbol       the hit IS the day's literal; no reification at all.
  unanchored, len>=2  a DFA over the whole row (see build_dfa).
  anchored / clipped  model.py's window conjunction, unchanged.

Hit counts are summed across all three, exactly as scorer.py sums them across
patterns, so the split is invisible to the objective.

`--upper-bound N` replaces model.py's `--harden`. Given a roster of cost N that
you already know exists, any rule whose weight exceeds N cannot be violated by
an optimal roster -- one violation alone would cost more than N. So every
penalty above that weight is pinned to zero. Unlike `--harden`, whose soundness
argument is written against QMC-2's specific 1000/100/1 ladder, this rule reads
nothing but the instance's own weights and the bound you supply, so it carries
to any instance:

  --upper-bound 894   on BCV-3.46.2, pinning cover (10000) and the shift
                      succession rules (1000)
  --upper-bound 29    on QMC-2, pinning its weight-1000 and weight-100 rules

For BCV-3.46.2 the cover half of that is the whole ballgame. Its cover blocks
carry only <Preferred>, and both PrefUnderStaffing and PrefOverStaffing weigh
10000, so an optimal roster staffs every (day, shift) at exactly its preferred
headcount -- 78 soft penalties collapse into 78 cardinality equalities and the
daily headcount per shift becomes a constant.

Off by default, so with no `--upper-bound` this file is a faithful scorer of
whatever roster it is given and `--fix-roster` reproduces that roster's true
cost. That is the acceptance test: pinned to a published optimum it must report
that optimum exactly, and its breakdown must match scorer.py's.

Run from the repository root:
    uv run examples/nurse_rostering/model_regular.py \
        --instance BCV-3.46.2.json --upper-bound 894 --time-limit 600
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ortools.sat.python import cp_model, cp_model_helper

sys.path.insert(0, str(Path(__file__).parent))

from instance import (  # noqa: E402
    Contract,
    Instance,
    Limit,
    Match,
    Pattern,
    Symbol,
    load_instance,
)
from roster import resolve_roster_path  # noqa: E402
from shift_literals import ShiftLiterals  # noqa: E402

OFF: str = "-"

# The request 2x2, keyed by (wants, names a specific shift). Written out here
# rather than imported from scorer.py for the reason model.py gives: these are
# the keys the two breakdowns are compared on, and a shared table would make
# them agree by construction instead of by both being right.
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
    model_stats: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Options:
    """Everything the command line settles before any modelling happens."""

    instance_path: Path
    time_limit: float
    workers: int
    seed: int
    upper_bound: int | None
    redundant: bool
    regular: bool
    fix_roster: Path | None


def read_input() -> Options:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", default="QMC-2.json")
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
        "--no-redundant",
        dest="redundant",
        action="store_false",
        help=(
            "Drop the implied aggregate constraints (per-day headcount "
            "conservation and the horizon-wide per-shift totals). They cut no "
            "solutions, so this changes runtime only -- which is the point of "
            "being able to turn them off."
        ),
    )
    parser.add_argument(
        "--no-regular",
        dest="regular",
        action="store_false",
        help=(
            "Route every pattern through the window encoding, automata included. "
            "Both encodings compute the same counts, so this changes runtime "
            "only -- and it is the control arm for the claim that the automata "
            "are worth having. Without it the claim is untestable."
        ),
    )
    parser.add_argument(
        "--fix-roster",
        default=None,
        help=(
            "Pin every assignment variable to the roster in this file and solve "
            "trivially, so the model reports its own objective for a roster it "
            "did not choose. Pointed at a published optimum this is the sharpest "
            "test of the encoding: a DFA that miscounts scores it wrong."
        ),
    )
    args = parser.parse_args()

    here: Path = Path(__file__).parent / "parsed"
    return Options(
        instance_path=here / args.instance,
        time_limit=args.time_limit,
        workers=args.workers,
        seed=args.seed,
        upper_bound=args.upper_bound,
        redundant=args.redundant,
        regular=args.regular,
        fix_roster=None if args.fix_roster is None else resolve_roster_path(args.fix_roster),
    )


def parse_input(raw: Options) -> tuple[Instance, Options]:
    return load_instance(raw.instance_path), raw


# -- the regular layer -----------------------------------------------------


@dataclass(frozen=True)
class Dfa:
    """A deterministic automaton counting occurrences of a set of patterns.

    `triples` is (state, letter, next_state, hits): reading `letter` in `state`
    moves to `next_state` and completes `hits` pattern occurrences. Keeping the
    hit count on the TRANSITION rather than folding a running total into the
    state is what keeps these automata at 2-11 states instead of
    states * max_count; the count comes back as a per-day integer summed
    linearly.
    """

    num_states: int
    max_hits: int
    triples: list[tuple[int, int, int, int]]


def symbol_letters(
    symbol: Symbol, alphabet: list[str], shift_groups: dict[str, list[str]]
) -> frozenset[str]:
    """The concrete assignments one pattern symbol accepts.

    The ground-truth reading of the four symbol kinds, which model.py expresses
    instead as CP-SAT literals: a DFA needs the letter SET, a window conjunction
    needs a literal, and neither derives from the other. That makes this the one
    place where this file restates semantics scorer.py also implements, so the
    DFA path is cross-checked empirically -- `--fix-roster` against a published
    optimum -- rather than by construction.

    `notshift` and `notgroup` both include the day off. Reading either as "some
    other WORKING shift" makes BCV-3.46.2's free-weekend rules unfalsifiable,
    since that file spells a free day as <NotGroup>ON</NotGroup>. The `worked`
    wildcard is the one symbol that goes the other way -- it EXCLUDES the day
    off -- which is what makes ERMGH's working-weekend rules falsifiable.
    """
    kind: str = symbol["kind"]
    value: str = symbol["value"]
    if kind == "shift":
        return frozenset({value})
    if kind == "worked":
        # The `$` wildcard: every letter EXCEPT the day off. See
        # parse_instance.WORKED_SYMBOL.
        return frozenset(letter for letter in alphabet if letter != OFF)
    if kind == "group":
        return frozenset(shift_groups[value])
    if kind == "notshift":
        return frozenset(letter for letter in alphabet if letter != value)
    if kind == "notgroup":
        members: list[str] = shift_groups[value]
        return frozenset(letter for letter in alphabet if letter not in members)
    raise ValueError(f"unknown symbol kind {kind!r}")


def build_dfa(
    patterns: list[Pattern], alphabet: list[str], shift_groups: dict[str, list[str]]
) -> Dfa:
    """Subset construction over the set of live partial matches.

    A pattern symbol is a PREDICATE over the alphabet, not a letter, and the
    predicates overlap -- `notshift:N` and `shift:L` both accept L -- so a trie
    over the patterns is non-deterministic and an Aho-Corasick failure function
    does not apply. Determinising over the concrete alphabet does: a state is
    the frozenset of `(pattern index, symbols matched so far)` pairs still
    alive, and a fresh `(p, 1)` is seeded at every position, which is exactly
    why overlapping occurrences are counted the way scorer.py counts them --
    once per start day.

    The automaton is built once per <Match> block and shared by every employee
    on that contract, so its cost is per contract, not per employee.
    """

    accepted: list[list[frozenset[str]]] = [
        [symbol_letters(symbol, alphabet, shift_groups) for symbol in pattern.symbols]
        for pattern in patterns
    ]

    def step(
        state: frozenset[tuple[int, int]], letter: str
    ) -> tuple[frozenset[tuple[int, int]], int]:
        live: set[tuple[int, int]] = set()
        hits: int = 0
        for index, matched in state:
            if letter in accepted[index][matched]:
                if matched + 1 == len(accepted[index]):
                    hits += 1
                else:
                    live.add((index, matched + 1))
        for index, symbols in enumerate(accepted):
            if letter in symbols[0]:
                if len(symbols) == 1:
                    hits += 1
                else:
                    live.add((index, 1))
        return frozenset(live), hits

    start: frozenset[tuple[int, int]] = frozenset()
    ids: dict[frozenset[tuple[int, int]], int] = {start: 0}
    triples: list[tuple[int, int, int, int]] = []
    queue: deque[frozenset[tuple[int, int]]] = deque([start])
    while queue:
        state: frozenset[tuple[int, int]] = queue.popleft()
        for letter_id, letter in enumerate(alphabet):
            successor, hits = step(state, letter)
            if successor not in ids:
                ids[successor] = len(ids)
                queue.append(successor)
            triples.append((ids[state], letter_id, ids[successor], hits))

    return Dfa(
        num_states=len(ids),
        max_hits=max(triple[3] for triple in triples),
        triples=triples,
    )


class RosterModel:
    """Builds the CP-SAT model and keeps the handles needed to read it back."""

    def __init__(
        self,
        instance: Instance,
        upper_bound: int | None,
        redundant: bool,
        regular: bool = True,
    ) -> None:
        self.instance: Instance = instance
        self.upper_bound: int | None = upper_bound
        self.redundant: bool = redundant
        self.regular: bool = regular
        self.model: cp_model.CpModel = cp_model.CpModel()
        self.first_weekday: int = date.fromisoformat(instance.start_date).weekday()
        self.penalties: list[PenaltyTerm] = []

        # Letter 0 is the day off, letter i+1 is shift_types[i]. The DFAs read
        # `seq`, so this ordering is the alphabet everywhere below.
        self.alphabet: list[str] = [OFF] + list(instance.shift_types)

        # How much of the pattern work each encoding absorbed, reported
        # alongside the solution so the split is visible rather than asserted.
        self.stats: dict[str, int] = {
            "dfa_blocks": 0,
            "dfa_hard_blocks": 0,
            "dfa_states": 0,
            "single_symbol_patterns": 0,
            "window_hit_vars": 0,
        }

        # x[employee, day, shift]: the core booleans. No skill subscript -- an
        # employee's skills are fixed attributes, not a decision.
        self.x: dict[tuple[str, int, str], cp_model.IntVar] = {}
        # works[employee, day]: worked at all that day, i.e. the negation of "-".
        self.works: dict[tuple[str, int], cp_model.IntVar] = {}
        # seq[employee, day]: the same assignment as one integer, 0 = day off.
        # This is the channel the automata read; the booleans stay because cover,
        # requests and the window encoding all want literals.
        self.seq: dict[tuple[str, int], cp_model.IntVar] = {}
        # daily[day, shift]: headcount. Named once and shared by every cover
        # penalty and every redundant cut, instead of rebuilding the same sum.
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
                # Without it the solver cheerfully puts a nurse on N, L and V
                # the same day to satisfy three cover blocks with one person.
                self.model.add_at_most_one(literals)

                works: cp_model.IntVar = self.model.new_bool_var(f"w_{employee.id}_{day}")
                self.model.add_max_equality(works, literals)
                self.works[employee.id, day] = works

                # Channelled by a single linear equation rather than by three
                # reified equalities: at_most_one already makes the sum a 0/1
                # selector, so the weighted sum lands on exactly one letter.
                letter: cp_model.IntVar = self.model.new_int_var(
                    0, len(instance.shift_types), f"seq_{employee.id}_{day}"
                )
                self.model.add(
                    letter == sum((index + 1) * literal for index, literal in enumerate(literals))
                )
                self.seq[employee.id, day] = letter

    # -- shared helpers ----------------------------------------------------

    def _dominated(self, weight: int) -> bool:
        """True when one violation at this weight already costs more than a
        roster we know exists, so no optimal roster can afford it."""
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

        Reified in both directions. Only `hit >= AND(literals)` is needed for a
        valid upper bound, but the reverse costs little and makes the per-label
        breakdown read back exactly -- which matters, because that breakdown is
        cross-checked against an independent scorer.
        """
        hit: cp_model.IntVar = self.model.new_bool_var(f"hit_{employee_id}_{len(self.penalties)}")
        self.model.add_bool_and(literals).only_enforce_if(hit)
        self.model.add_bool_or([literal.Not() for literal in literals] + [hit])
        self.stats["window_hit_vars"] += 1
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
        clamp is what stops an over-satisfied rule from scoring negative and
        dragging the objective down.

        `observed_upper` is the largest value `observed` can take; the penalty's
        own bound follows from it and the sense together, because the two senses
        clamp opposite ends. A Max limit overshoots by at most
        `observed_upper - count`, while a Min limit is worst at `observed = 0`
        and needs room for `count`.
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
        employees_by_contract: dict[str, list[str]] = {}
        for employee in self.instance.employees:
            # Follow <ContractID>. Employee P uses contract O; assuming the
            # employee ID is the contract ID applies contract P, which nobody uses.
            employees_by_contract.setdefault(employee.contract_id, []).append(employee.id)

        for contract_id, employee_ids in employees_by_contract.items():
            contract: Contract = contracts[contract_id]
            for match in contract.matches:
                self._build_match(match, employee_ids)

        for employee in self.instance.employees:
            contract = contracts[employee.contract_id]
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

    def _build_match(self, match: Match, employee_ids: list[str]) -> None:
        """Encode one <Match> block for every employee on its contract.

        Patterns are split by SHAPE, not by block, because a block routinely
        mixes them: `Max 4 consecutive working days` pairs a long unanchored
        pattern with a short one pinned to the horizon's last day. Hits from all
        three encodings are summed into one observed count, which is exactly how
        scorer.py sums them across patterns, so the split cannot change a score.
        """
        full_region: bool = (
            match.region_start == 0 and match.region_end >= self.instance.num_days - 1
        )

        single: list[Pattern] = []
        regular: list[Pattern] = []
        windowed: list[Pattern] = []
        for pattern in match.patterns:
            unanchored: bool = pattern.start_day_index is None and pattern.start_weekday is None
            if len(pattern.symbols) == 1:
                # A one-day pattern's window IS its literal, so reifying a
                # conjunction of one is pure waste. BCV-3.46.2 spends 44 of its
                # 122 blocks on `Max shift types per week`, all of this shape.
                single.append(pattern)
            elif unanchored and full_region and self.regular:
                regular.append(pattern)
            else:
                # Anchored to a weekday or an absolute day, or living in a
                # sub-region the automaton would have to be reset for.
                windowed.append(pattern)

        dfa: Dfa | None = None
        if regular:
            dfa = build_dfa(regular, self.alphabet, self.instance.shift_groups)
            self.stats["dfa_blocks"] += 1
            self.stats["dfa_states"] += dfa.num_states

        # A dominated `max 0` block cannot be violated at all, so the automaton
        # can enforce the language directly instead of counting into a penalty
        # that is pinned to zero anyway -- no state variables, no hit variables,
        # and a propagator that prunes the row as it is built. BCV-3.46.2's
        # weight-1000 shift succession (no N->L, no N->V) is exactly this.
        hard_regular: bool = (
            dfa is not None
            and match.limit.sense == "max"
            and match.limit.count == 0
            and self._dominated(match.limit.weight)
        )
        if hard_regular:
            self.stats["dfa_hard_blocks"] += 1

        for employee_id in employee_ids:
            observed: list[cp_model.LinearExprT] = []
            observed_upper: int = 0

            for pattern in single:
                starts: list[int] = self._candidate_starts(pattern, match)
                observed.extend(
                    self.literals.symbol_literal(employee_id, start, pattern.symbols[0])
                    for start in starts
                )
                observed_upper += len(starts)
                self.stats["single_symbol_patterns"] += 1

            for pattern in windowed:
                for start in self._candidate_starts(pattern, match):
                    literals: list[cp_model_helper.Literal] = [
                        self.literals.symbol_literal(employee_id, start + offset, symbol)
                        for offset, symbol in enumerate(pattern.symbols)
                    ]
                    observed.append(self._hit_variable(employee_id, literals))
                    observed_upper += 1

            if dfa is not None:
                if hard_regular:
                    self._add_hard_automaton(employee_id, dfa)
                else:
                    hits, upper = self._add_counting_automaton(employee_id, dfa)
                    observed.append(hits)
                    observed_upper += upper

            # Added even when nothing was observed -- every window fell outside
            # the region. `sum([])` is 0, the correct observed count, and a Min
            # limit then charges its full `count * weight` exactly as scorer.py
            # does. Skipping the term drops a penalty the scorer still reports,
            # and checker.py rejects the run for a mismatch with no visible cause.
            self._add_clamped_penalty(
                "rule", match.limit.label, sum(observed), match.limit, observed_upper
            )

    def _add_hard_automaton(self, employee_id: str, dfa: Dfa) -> None:
        """Forbid the block's language outright with `add_automaton`.

        Every state is accepting -- the row is legal as long as it never
        completes a pattern -- and the hit-producing transitions are simply
        absent from the table, which is how a DFA says "this letter is
        impossible here".
        """
        self.model.add_automaton(
            [self.seq[employee_id, day] for day in range(self.instance.num_days)],
            0,
            list(range(dfa.num_states)),
            [(tail, letter, head) for tail, letter, head, hits in dfa.triples if hits == 0],
        )

    def _add_counting_automaton(
        self, employee_id: str, dfa: Dfa
    ) -> tuple[cp_model.LinearExprT, int]:
        """Run the DFA as a chain of table constraints and sum the hits.

        `add_automaton` would enforce the language but hide the state, and the
        count is the whole point for a soft rule. Spelling the same automaton as
        `add_allowed_assignments` over (state, letter, next state, hits) exposes
        both, at one table constraint per day -- against one reified conjunction
        per window, over windows up to eleven days long, for the encoding this
        replaces.
        """
        num_days: int = self.instance.num_days
        states: list[cp_model.IntVar] = [
            self.model.new_int_var(0, dfa.num_states - 1, f"q_{employee_id}_{day}")
            for day in range(num_days + 1)
        ]
        self.model.add(states[0] == 0)

        hits: list[cp_model.IntVar] = []
        for day in range(num_days):
            hit: cp_model.IntVar = self.model.new_int_var(
                0, dfa.max_hits, f"h_{employee_id}_{day}_{len(self.penalties)}"
            )
            self.model.add_allowed_assignments(
                [states[day], self.seq[employee_id, day], states[day + 1], hit],
                dfa.triples,
            )
            hits.append(hit)
        return sum(hits), num_days * dfa.max_hits

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
        matter most once `--upper-bound` has pinned cover to an equality, since
        then every `daily` variable is a constant and these become statements
        about fixed totals -- on BCV-3.46.2, exactly 534 assignments split
        N=150, L=182, V=202, against per-employee caps like `Max 6 N shifts`.
        """
        instance: Instance = self.instance

        for day in range(instance.num_days):
            # Headcount conservation: at_most_one makes each employee's row sum
            # a 0/1 selector, so the day's workers equal the day's assignments.
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
        instance,
        upper_bound=options.upper_bound,
        redundant=options.redundant,
        regular=options.regular,
    )

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
    solution.model_stats = dict(builder.stats)

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
        "solution": {
            "roster": solution.roster,
            "breakdown": solution.breakdown,
            "encoding": solution.model_stats,
        },
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    raw: Options = read_input()
    parsed: tuple[Instance, Options] = parse_input(raw)
    solution: Solution = solve(parsed)
    # The roster goes out on stdout with everything else; this script writes no
    # files. See model.py's main() for why the CSV write was removed rather than
    # left disabled.
    write_output(serialize_solution(solution))


if __name__ == "__main__":
    main()
