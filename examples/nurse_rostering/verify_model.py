"""Cross-check the CP-SAT model against the independent scorer.

Three gates, in the order that makes a failure diagnosable:

1. GOLDEN -- the scorer, pointed at the published cost-29 roster, must
   independently reproduce 29 with the published per-constraint breakdown. Until
   this passes, nothing else is evidence of anything.
2. GROUND TRUTH -- the MODEL, with every variable pinned to that same published
   roster, must also report 29 with the same breakdown. This is the sharpest
   test of the model's penalty structure: a model missing a penalty scores a
   known-cost-29 roster below 29 and is caught here rather than by producing a
   plausible-looking wrong answer later.
3. AGREEMENT -- for several seeds, the scorer's total for the roster the model
   produced must equal the objective the model reported. Different seeds land on
   different optima with different violation compositions, so each seed is an
   independent test that the objective and the real scoring rules have not
   drifted apart.

Reaching 29 is not itself evidence of correctness; a missing constraint and a
compensating error can land on 29 by coincidence. These three gates are.

Run from the repository root:
    uv run examples/nurse_rostering/verify_model.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from model import Options, Solution, solve  # noqa: E402
from parse_instance import Instance, parse_instance  # noqa: E402
from scorer import Breakdown, read_roster_xml, score  # noqa: E402

HERE: Path = Path(__file__).parent
PUBLISHED_ROSTER: Path = HERE / "QMC-2.Solution.29.roster"

PUBLISHED_BREAKDOWN: dict[str, int] = {
    "ShiftOn request (weight 1)": 24,
    "DayOff request (weight 1)": 1,
    "No half weekends": 1,
    "Preferred overstaffing": 3,
}
PUBLISHED_TOTAL: int = 29

SEEDS: tuple[int, ...] = (1, 7, 42, 2001)


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


def _options(instance_path: Path, seed: int, fix_roster: Path | None = None) -> Options:
    return Options(
        instance_path=instance_path,
        csv_path=HERE / "verify_scratch.csv",
        time_limit=120.0,
        workers=8,
        seed=seed,
        harden=False,
        fix_roster=fix_roster,
    )


def main() -> None:
    instance: Instance = parse_instance(HERE / "QMC-2.ros")
    passed: list[bool] = []

    print("gate 1 -- scorer vs. the published cost-29 roster")
    golden: Breakdown = score(instance, read_roster_xml(PUBLISHED_ROSTER, instance))
    passed.append(
        _report(golden.total == PUBLISHED_TOTAL, f"scorer total = {golden.total} (expected 29)")
    )
    passed.append(
        _report(
            _flatten(golden) == PUBLISHED_BREAKDOWN,
            f"scorer breakdown = {_flatten(golden)}",
        )
    )

    print("\ngate 2 -- model pinned to that same roster")
    pinned: Solution = solve((instance, _options(HERE / "QMC-2.ros", 42, PUBLISHED_ROSTER)))
    passed.append(
        _report(pinned.objective == PUBLISHED_TOTAL, f"model objective = {pinned.objective}")
    )
    passed.append(
        _report(
            _flatten_model(pinned.breakdown) == PUBLISHED_BREAKDOWN,
            f"model breakdown = {_flatten_model(pinned.breakdown)}",
        )
    )

    print("\ngate 3 -- model objective vs. scorer, across seeds")
    for seed in SEEDS:
        solution: Solution = solve((instance, _options(HERE / "QMC-2.ros", seed)))
        if not solution.roster:
            # No incumbent inside this seed's budget. That is a FAIL for the
            # seed, not a reason to abandon the run: score() indexes the roster
            # by employee ID, so handing it the empty dict solve() returns for a
            # non-OPTIMAL/FEASIBLE status raises KeyError, which takes the
            # remaining seeds and the scratch-file cleanup down with it.
            passed.append(
                _report(
                    False,
                    f"seed {seed:>4}: status {solution.status}, no solution to score",
                )
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

    scratch: Path = HERE / "verify_scratch.csv"
    if scratch.exists():
        scratch.unlink()

    failures: int = sum(1 for ok in passed if not ok)
    print(f"\n{len(passed) - failures}/{len(passed)} gates passed")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
