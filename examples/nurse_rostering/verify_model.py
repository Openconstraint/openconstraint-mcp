"""Cross-check the CP-SAT model against the independent scorer.

Three gates per instance, in the order that makes a failure diagnosable:

1. GOLDEN -- the scorer, pointed at the published roster, must independently
   reproduce its published cost with the published per-constraint breakdown.
   Until this passes, nothing else is evidence of anything.
2. GROUND TRUTH -- the MODEL, with every variable pinned to that same published
   roster, must report the same cost and the same breakdown. This is the
   sharpest test of the model's penalty structure: a model missing a penalty
   scores a known-cost roster below its published cost and is caught here rather
   than by producing a plausible-looking wrong answer later.
3. AGREEMENT -- for several seeds, the scorer's total for the roster the model
   produced must equal the objective the model reported. Different seeds land on
   different rosters with different violation compositions, so each seed is an
   independent test that the objective and the real scoring rules have not
   drifted apart.

Reaching the published cost is not itself evidence of correctness; a missing
constraint and a compensating error can land on it by coincidence. These three
gates are.

All three instances are gated, and all three published rosters are PROVEN
OPTIMAL on the benchmark site (29 for QMC-2, 894 for BCV-3.46.2, 779 for ERMGH),
so gates 1 and 2 are graded against ground truth rather than against a merely
good roster.

Four further instances get gate 1 alone -- see GOLDEN. They exist to keep the
parser's optional-element fallbacks honest, and gate 1 is exactly the gate that
catches one silently rescaling a cost.

Note what gate 3 does and does not claim. It asserts the model and the scorer
AGREE on whatever roster the search produced, not that the search found the
optimum. In practice all three instances do reach their published cost in these
budgets -- QMC-2 and ERMGH prove 29 and 779 optimal, BCV-3.46.2 reaches 894
without proving it -- but a seed that merely found something worse would still
pass, and should: an agreeing pair of implementations is the property under
test.

Run from the repository root:
    uv run examples/nurse_rostering/verify_model.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from instance import Instance, load_instance  # noqa: E402
from model import Options, Solution, solve  # noqa: E402
from roster import load_roster  # noqa: E402
from scorer import Breakdown, score  # noqa: E402

HERE: Path = Path(__file__).parent / "parsed"


@dataclass(frozen=True)
class Case:
    """One instance's three gates, and the published roster they grade against.

    Parameterised rather than duplicated so a new rule cannot be added to the
    model and gated on one instance only. They differ in the budget gate 3
    gets: BCV-3.46.2 is roughly nine times QMC-2's pattern machinery (1124
    per-employee <Match> instances against 121), and gate 3 only needs an
    incumbent to score, not a good one.
    """

    name: str
    instance: Path
    roster: Path
    total: int
    breakdown: dict[str, int]
    seeds: tuple[int, ...]
    time_limit: float


CASES: tuple[Case, ...] = (
    Case(
        name="QMC-2",
        instance=HERE / "QMC-2.json",
        roster=HERE / "QMC-2.Solution.29.json",
        total=29,
        breakdown={
            "ShiftOn request (weight 1)": 24,
            "DayOff request (weight 1)": 1,
            "No half weekends": 1,
            "Preferred overstaffing": 3,
        },
        seeds=(1, 7, 42, 2001),
        time_limit=120.0,
    ),
    Case(
        name="BCV-3.46.2",
        instance=HERE / "BCV-3.46.2.json",
        roster=HERE / "BCV-3.46.2.Solution.894.json",
        total=894,
        # Every cover block scores zero, which is the load-bearing detail: at
        # PrefUnder/PrefOverStaffing = 10000 a single unit of deviation would
        # dwarf the whole optimum, so this line is what proves the model reads
        # <DateSpecificCover> as overriding the weekday block rather than adding
        # to it. The additive reading scores this same roster 40894.
        breakdown={
            "DayOn request (weight 50)": 800,
            "Both days on or off on weekend": 80,
            "Max 2 consecutive N shifts": 14,
        },
        seeds=(1, 42),
        time_limit=60.0,
    ),
    Case(
        name="ERMGH",
        instance=HERE / "ERMGH.json",
        roster=HERE / "ERMGH.Solution.779.json",
        total=779,
        # The whole optimum is cover cost, and every penny of it is charged
        # against a SKILL-qualified headcount over a <TimePeriod>. Two things
        # are load-bearing here. That no rule and no request appears is real,
        # not a gap: all 843 <Match> blocks, all 227 workload limits and all
        # 1514 requests are satisfied by this roster, so a model that silently
        # dropped any of those three families would still pass gate 1 -- which
        # is why verify_parse.py pins their counts and the `$` census instead.
        # And that the two lines below say "skill": reading a skill block as a
        # bare <Min>, which is all QMC-2's skill blocks carry, ignores the
        # <Max>/<Preferred> that every ERMGH block carries and scores this
        # roster 0.
        breakdown={
            "skill Preferred understaffing": 777,
            "skill Preferred overstaffing": 2,
        },
        seeds=(1, 42),
        time_limit=60.0,
    ),
)


# Instances shipped for gate 1 ALONE: the scorer must reproduce the published
# optimum. They carry no model gate because they are here to keep the parser's
# optional-element fallbacks honest, not to exercise the encoding -- and gate 1
# is the gate that catches a fallback silently rescaling a cost. verify_parse.py
# asserts WHICH element each one exercises; this asserts the number still lands.
#
# Each published roster is the lowest-cost one the benchmark site publishes for
# that instance.
GOLDEN: tuple[tuple[str, str, int], ...] = (
    ("BCDT-Sep", "BCDT-Sep.Solution.100.json", 100),
    ("GPost", "GPost.Solution.5.json", 5),
    ("Millar-2Shift-DATA1", "Millar-2Shift-DATA1.Solution.0.json", 0),
    ("QMC-1", "QMC-1.Solution.13.json", 13),
)


def run_golden() -> list[bool]:
    """Gate 1 only, for the fallback instances."""
    passed: list[bool] = []
    print("golden: scorer vs. the published optimum, fallback instances")
    for name, roster, published in GOLDEN:
        instance: Instance = load_instance(HERE / f"{name}.json")
        total: int = score(instance, load_roster(HERE / roster)).total
        passed.append(
            _report(total == published, f"{name:<22} scorer {total:>5}  published {published:>5}")
        )
    return passed


def _flatten(breakdown: Breakdown) -> dict[str, int]:
    flat: dict[str, int] = {}
    for bucket in (breakdown.by_label, breakdown.by_cover_type, breakdown.requests):
        for key, value in bucket.items():
            flat[key] = flat.get(key, 0) + value
    return flat


def _flatten_model(breakdown: dict[str, dict[str, int]]) -> dict[str, int]:
    flat: dict[str, int] = {}
    for bucket in breakdown.values():
        for key, value in bucket.items():
            flat[key] = flat.get(key, 0) + value
    return flat


def _report(ok: bool, message: str) -> bool:
    print(f"  {'ok  ' if ok else 'FAIL'}  {message}")
    return ok


def _options(case: Case, seed: int, time_limit: float, fix_roster: Path | None = None) -> Options:
    return Options(
        instance_path=case.instance,
        time_limit=time_limit,
        workers=8,
        seed=seed,
        harden=False,
        fix_roster=fix_roster,
    )


def run_case(case: Case) -> list[bool]:
    """Run all three gates for one instance and report each line."""
    instance: Instance = load_instance(case.instance)
    passed: list[bool] = []

    print(f"{case.name} -- gate 1: scorer vs. the published cost-{case.total} roster")
    golden: Breakdown = score(instance, load_roster(case.roster))
    passed.append(
        _report(
            golden.total == case.total, f"scorer total = {golden.total} (expected {case.total})"
        )
    )
    passed.append(
        _report(_flatten(golden) == case.breakdown, f"scorer breakdown = {_flatten(golden)}")
    )

    print(f"\n{case.name} -- gate 2: model pinned to that same roster")
    # Pinned, so the search is trivial and the time limit is irrelevant; the
    # seed is fixed because there is nothing left to randomise.
    pinned: Solution = solve((instance, _options(case, 42, case.time_limit, case.roster)))
    passed.append(_report(pinned.objective == case.total, f"model objective = {pinned.objective}"))
    passed.append(
        _report(
            _flatten_model(pinned.breakdown) == case.breakdown,
            f"model breakdown = {_flatten_model(pinned.breakdown)}",
        )
    )

    print(f"\n{case.name} -- gate 3: model objective vs. scorer, across seeds")
    for seed in case.seeds:
        solution: Solution = solve((instance, _options(case, seed, case.time_limit)))
        if not solution.roster:
            # No incumbent inside this seed's budget. That is a FAIL for the
            # seed, not a reason to abandon the run: score() indexes the roster
            # by employee ID, so handing it the empty dict solve() returns for a
            # non-OPTIMAL/FEASIBLE status raises KeyError, which takes the
            # remaining seeds and the scratch-file cleanup down with it.
            passed.append(
                _report(False, f"seed {seed:>4}: status {solution.status}, no solution to score")
            )
            continue
        recomputed: Breakdown = score(instance, solution.roster)
        agree: bool = recomputed.total == solution.objective
        same_shape: bool = _flatten(recomputed) == _flatten_model(solution.breakdown)
        passed.append(
            _report(
                agree and same_shape,
                f"seed {seed:>4}: status {solution.status}, model {solution.objective}, "
                f"scorer {recomputed.total}"
                + ("" if same_shape else f"  breakdown differs: {_flatten(recomputed)}"),
            )
        )
    return passed


def main() -> None:
    passed: list[bool] = []
    for index, case in enumerate(CASES):
        if index:
            print()
        passed.extend(run_case(case))

    print()
    passed.extend(run_golden())

    failures: int = sum(1 for ok in passed if not ok)
    print(f"\n{len(passed) - failures}/{len(passed)} gates passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
