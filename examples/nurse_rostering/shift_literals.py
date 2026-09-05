"""The pattern-symbol vocabulary, as CP-SAT literals, for the model files.

`model.py`, `model_bounds.py` and `model_regular.py` all encode a `<Pattern>`
window as a conjunction of one literal per symbol, and all three encoded it
identically -- three byte-for-byte copies of the same three methods. A new
symbol kind therefore had to be written three times to work everywhere, and
`$` genuinely was: ERMGH's wildcard landed in each file separately.

What is deliberately NOT shared:

- `scorer.py` keeps its own reading of the same vocabulary, in plain Python
  over a finished roster. That duplication is the whole point of the example --
  a scorer derived from the model would reproduce the model's misreadings and
  confirm the wrong number. Sharing it would make the two agree by construction.
- `model_regular.py`'s `_symbol_letters`, which reads each symbol as a SET OF
  LETTERS for its DFA rather than as a literal. A DFA needs the alphabet subset
  and a window conjunction needs a literal; neither derives from the other, so
  that file states the vocabulary a second time on purpose and is cross-checked
  empirically by pinning it to a published optimum.

So the sharing here collapses three copies of ONE encoding into one, and leaves
both of the deliberate restatements standing.
"""

from __future__ import annotations

from instance import Symbol
from ortools.sat.python import cp_model, cp_model_helper

OFF: str = "-"


class ShiftLiterals:
    """Maps one pattern symbol on one day to a single boolean literal.

    Holds only what that mapping needs -- the model, the assignment variables,
    the `worked at all` variables and the shift vocabulary -- so a model class
    composes it rather than inheriting from it. Every symbol in every shipped
    instance reduces to a literal, which is what keeps the encoding small: a
    match window is then a plain conjunction.
    """

    def __init__(
        self,
        model: cp_model.CpModel,
        x: dict[tuple[str, int, str], cp_model.IntVar],
        works: dict[tuple[str, int], cp_model.IntVar],
        shift_types: list[str],
        shift_groups: dict[str, list[str]],
    ) -> None:
        self.model: cp_model.CpModel = model
        self.x: dict[tuple[str, int, str], cp_model.IntVar] = x
        self.works: dict[tuple[str, int], cp_model.IntVar] = works
        self.shift_types: list[str] = shift_types
        self.shift_groups: dict[str, list[str]] = shift_groups

    def symbol_literal(self, employee_id: str, day: int, symbol: Symbol) -> cp_model_helper.Literal:
        kind: str = symbol["kind"]
        value: str = symbol["value"]

        if kind == "shift":
            if value == OFF:
                return self.works[employee_id, day].Not()
            return self.x[employee_id, day, value]

        if kind == "worked":
            # The `$` wildcard: any working shift, a day off excluded -- which
            # is exactly what `works` already means.
            return self.works[employee_id, day]

        if kind == "group":
            return self.in_group_literal(employee_id, day, value)

        if kind == "notshift":
            # "anything except N", a day off included -- so it is the negation of
            # the single shift literal, NOT "some other working shift".
            return self.x[employee_id, day, value].Not()

        if kind == "notgroup":
            # "anything outside the group", a day off included -- the group
            # literal negated, for the same reason <NotShift> is the shift
            # literal negated. BCV-3.46.2 writes its free days this way.
            return self.in_group_literal(employee_id, day, value).Not()

        raise ValueError(f"unknown symbol kind {kind!r}")

    def in_group_literal(self, employee_id: str, day: int, group: str) -> cp_model.IntVar:
        """A literal true exactly when that day's assignment lies in `group`.

        Shared by the `group` and `notgroup` symbol kinds, which differ only by
        a negation -- keeping the group-membership encoding in one place stops
        the two from drifting into different resolutions of the same group ID.
        """
        return self.in_shifts_literal(employee_id, day, self.shift_groups[group], group)

    def in_shifts_literal(
        self, employee_id: str, day: int, members: list[str], name: str
    ) -> cp_model.IntVar:
        """A literal true exactly when that day's assignment lies in `members`.

        Also reached by ERMGH's inline-<ShiftGroup> requests, which name a set of
        shifts that is not in <ShiftGroups> at all, so this takes the members
        rather than a group ID.
        """
        if len(members) == 1:
            # One shift needs no new variable, and this is the common case: every
            # ordinary ShiftOn/ShiftOff request lands here.
            return self.x[employee_id, day, members[0]]
        if set(members) == set(self.shift_types):
            # The set names every shift type, so "in it" is exactly "worked" --
            # true of QMC-2's `All` and BCV-3.46.2's `ON` alike. Resolved from
            # the members, not assumed: a proper subset is handled below.
            return self.works[employee_id, day]
        in_group: cp_model.IntVar = self.model.new_bool_var(f"g_{employee_id}_{day}_{name}")
        self.model.add_max_equality(in_group, [self.x[employee_id, day, s] for s in members])
        return in_group
