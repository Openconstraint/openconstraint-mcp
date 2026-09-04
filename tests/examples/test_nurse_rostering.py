"""Fast gates for `examples/nurse_rostering`.

The example ships `verify_parse.py` and `verify_model.py` as scripts a human
runs by hand, and `verify_model.py`'s third gate solves the instance from
scratch on four seeds -- far too slow for `just check`. This file gates the
parts that are cheap and catch the most: the parse counts, the scorer against
the published cost-29 roster, and the model pinned to that same roster. The
four-seed agreement gate stays in `verify_model.py`.

Every import routes through the example's own modules on purpose. Those modules
put their own directory on `sys.path` and import each other flatly (`from
parse_instance import ...`), so `examples.nurse_rostering.model.parse_instance`
and a direct `examples.nurse_rostering.parse_instance` import resolve to two
distinct module objects with two distinct sets of dataclasses. Taking
`parse_instance` from `model` keeps this file on the same copy the example uses.
"""

from pathlib import Path

import pytest

from examples.nurse_rostering.checker import check_payload
from examples.nurse_rostering.model import Options, Solution, parse_instance, solve
from examples.nurse_rostering.scorer import read_roster_xml, score
from examples.nurse_rostering.verify_parse import verify, verify_ermgh, verify_fallbacks

ROOT = Path(__file__).parents[2]
EXAMPLE_DIR = ROOT / "examples" / "nurse_rostering"
INSTANCE_PATH = EXAMPLE_DIR / "QMC-2.ros"
PUBLISHED_ROSTER = EXAMPLE_DIR / "QMC-2.Solution.29.roster"

PUBLISHED_TOTAL = 29
PUBLISHED_BREAKDOWN = {
    "ShiftOn request (weight 1)": 24,
    "DayOff request (weight 1)": 1,
    "No half weekends": 1,
    "Preferred overstaffing": 3,
}

# ERMGH, the third instance, carries the format corners QMC-2 has none of:
# <TimePeriod> cover, skill blocks with <Max>/<Preferred>, the `$` wildcard and
# inline-<ShiftGroup> requests. Its published cost-779 roster is proven optimal.
ERMGH_PATH = EXAMPLE_DIR / "ERMGH.ros"
ERMGH_ROSTER = EXAMPLE_DIR / "ERMGH.Solution.779.roster"
ERMGH_TOTAL = 779
ERMGH_BREAKDOWN = {
    "skill Preferred understaffing": 777,
    "skill Preferred overstaffing": 2,
}


@pytest.fixture(scope="module")
def instance():
    return parse_instance(INSTANCE_PATH)


@pytest.fixture(scope="module")
def published_roster(instance):
    return read_roster_xml(PUBLISHED_ROSTER, instance)


def _options(instance_path: Path, fix_roster: Path | None = None) -> Options:
    return Options(
        instance_path=instance_path,
        time_limit=60.0,
        workers=8,
        seed=42,
        harden=False,
        fix_roster=fix_roster,
    )


def _flatten(*buckets: dict[str, int]) -> dict[str, int]:
    flat: dict[str, int] = {}
    for bucket in buckets:
        for key, value in bucket.items():
            flat[key] = flat.get(key, 0) + value
    return flat


def test_parse_reproduces_every_published_count(instance) -> None:
    failures = [line for ok, line in verify(instance) if not ok]

    assert failures == []


def test_scorer_reproduces_the_published_optimum(instance, published_roster) -> None:
    breakdown = score(instance, published_roster)

    assert breakdown.total == PUBLISHED_TOTAL


def test_scorer_attributes_the_published_cost_to_the_published_rules(
    instance, published_roster
) -> None:
    """29 reached by the wrong route is not evidence: a missing penalty and a
    compensating over-charge land on the same total."""
    breakdown = score(instance, published_roster)

    flat = _flatten(breakdown.by_label, breakdown.by_cover_type, breakdown.requests)
    assert flat == PUBLISHED_BREAKDOWN


def test_model_pinned_to_the_published_roster_agrees_with_the_scorer(instance) -> None:
    """The sharpest cheap test of the penalty structure: pinned to a roster of
    known cost, a model missing a penalty scores it below 29."""
    solution = solve((instance, _options(INSTANCE_PATH, PUBLISHED_ROSTER)))

    assert (solution.status, solution.objective) == ("optimal", PUBLISHED_TOTAL)


def test_model_pinned_breakdown_matches_the_scorer_label_for_label(instance) -> None:
    solution = solve((instance, _options(INSTANCE_PATH, PUBLISHED_ROSTER)))

    assert _flatten(*solution.breakdown.values()) == PUBLISHED_BREAKDOWN


# -- ERMGH --------------------------------------------------------------------
#
# Each of these fails on a DIFFERENT misreading, and two of the misreadings are
# silent on QMC-2 and BCV-3.46.2 because neither file exercises them.


@pytest.fixture(scope="module")
def ermgh():
    return parse_instance(ERMGH_PATH)


@pytest.fixture(scope="module")
def ermgh_roster(ermgh):
    return read_roster_xml(ERMGH_ROSTER, ermgh)


def test_ermgh_parse_reproduces_every_published_count(ermgh) -> None:
    failures = [line for ok, line in verify_ermgh(ermgh) if not ok]

    assert failures == []


def test_ermgh_time_period_cover_resolves_to_the_shifts_on_duty_throughout(ermgh) -> None:
    """Containment, not overlap. E starts at 15:30 and so does not staff
    12:00-15:30; DH runs 12:00-20:00 and does staff 15:30-19:30."""
    resolved = {block.time_period: tuple(block.shifts) for block in ermgh.cover}

    assert resolved == {
        ("07:30:00", "11:30:00"): ("D",),
        ("11:30:00", "12:00:00"): ("D",),
        ("12:00:00", "15:30:00"): ("D", "DH"),
        ("15:30:00", "19:30:00"): ("DH", "E"),
        ("19:30:00", "20:00:00"): ("DH", "E"),
        ("20:00:00", "23:30:00"): ("E",),
        ("23:30:00", "07:30:00"): ("N",),
    }


def test_ermgh_dollar_is_a_wildcard_and_not_a_shift_named_dollar(ermgh) -> None:
    """`<Shift>$</Shift>` must parse to its own kind. As a shift ID it matches
    nothing, every rule scores 0, and the published optimum STILL totals 779 --
    so only the symbol census catches it, never the total."""
    symbols = [s for c in ermgh.contracts for m in c.matches for p in m.patterns for s in p.symbols]

    assert sum(1 for s in symbols if s["kind"] == "worked") == 1288
    assert [s for s in symbols if s["kind"] == "shift" and s["value"] == "$"] == []


def test_ermgh_scorer_reproduces_the_published_optimum(ermgh, ermgh_roster) -> None:
    breakdown = score(ermgh, ermgh_roster)

    assert breakdown.total == ERMGH_TOTAL


def test_ermgh_scorer_charges_the_cost_to_skill_qualified_cover(ermgh, ermgh_roster) -> None:
    """Reading a skill block as a bare <Min>, which is all QMC-2's skill blocks
    carry, ignores the <Max>/<Preferred> every ERMGH block carries and scores
    this roster 0 rather than 779."""
    breakdown = score(ermgh, ermgh_roster)

    flat = _flatten(breakdown.by_label, breakdown.by_cover_type, breakdown.requests)
    assert flat == ERMGH_BREAKDOWN


def test_ermgh_model_pinned_to_the_published_roster(ermgh) -> None:
    solution = solve((ermgh, _options(ERMGH_PATH, ERMGH_ROSTER)))

    assert (solution.status, solution.objective) == ("optimal", ERMGH_TOTAL)
    assert _flatten(*solution.breakdown.values()) == ERMGH_BREAKDOWN


# -- optional-element fallbacks ------------------------------------------------
#
# Four further instances ship purely to keep the parser's fallbacks honest. Each
# is the smallest benchmark file exercising one optional element, and each has a
# published optimum, so a fallback that silently rescaled a cost moves a number
# here rather than passing quietly.

FALLBACK_GOLDEN = (
    ("BCDT-Sep", "BCDT-Sep.Solution.100.roster", 100),
    ("GPost", "GPost.Solution.5.roster", 5),
    ("Millar-2Shift-DATA1", "Millar-2Shift-DATA1.Solution.0.roster", 0),
    ("QMC-1", "QMC-1.Solution.13.roster", 13),
)


@pytest.fixture(scope="module")
def fallback_instances():
    names = [f"{n}.ros" for n, _, _ in FALLBACK_GOLDEN] + ["QMC-2.ros", "BCV-3.46.2.ros"]
    return {name: parse_instance(EXAMPLE_DIR / name) for name in names}


def test_optional_element_fallbacks(fallback_instances) -> None:
    failures = [line for ok, line in verify_fallbacks(fallback_instances) if not ok]

    assert failures == []


@pytest.mark.parametrize(("name", "roster", "published"), FALLBACK_GOLDEN)
def test_fallback_instance_reproduces_its_published_optimum(name, roster, published) -> None:
    """The gate that makes the fallbacks evidence rather than decoration: each
    default was chosen because it is the value this roster's published cost
    implies, so a changed default moves the total."""
    instance = parse_instance(EXAMPLE_DIR / f"{name}.ros")

    total = score(instance, read_roster_xml(EXAMPLE_DIR / roster, instance)).total

    assert total == published


def test_a_stated_time_units_is_never_derived_from_the_clock(fallback_instances) -> None:
    """QMC-2 pays 15 minutes less than every shift's clock span, so deriving a
    duration it already states reads 90 where the file says 75 -- and a roster
    sitting exactly on its 750 boundary becomes a phantom violation."""
    assert fallback_instances["QMC-2.ros"].shift_time_units == {"E": 75, "L": 75, "N": 100}


def test_a_declared_cover_weights_block_is_not_topped_up(fallback_instances) -> None:
    """BCV-3.46.2 declares only the two Pref keys, and that absence is itself an
    asserted fact. Only a wholly missing element falls back."""
    assert sorted(fallback_instances["BCV-3.46.2.ros"].cover_weights) == [
        "PrefOverStaffing",
        "PrefUnderStaffing",
    ]


# -- <Min>-sense <Match> limits ------------------------------------------------
#
# QMC-2 contains none: all 121 of its match limits are <Max>. ERMGH supplies 18
# real ones, but only over its own rule shapes; the synthetic variants below
# still earn their place by putting a <Min> limit on the two corners no shipped
# instance reaches -- an empty candidate-window set, and a threshold above the
# window count. The model's
# penalty encoding is sense-dependent in two places -- whether an empty
# candidate-window set may be skipped, and how wide the penalty variable's
# domain must be -- so both need an instance the benchmark does not supply.


def _with_min_sense_match(tmp_path: Path, region: str, count: int) -> Path:
    """QMC-2 with contract A's first `No N-E` <Max> block swapped for a <Min> one."""
    xml = INSTANCE_PATH.read_text()
    mark = xml.index("No N-E")
    lo = xml.rindex("<Match>", 0, mark)
    hi = xml.index("</Match>", mark) + len("</Match>")
    block = (
        "<Match>\r\n"
        f"      <Min><Count>{count}</Count><Weight>7</Weight>"
        "<Label>Min N-E</Label></Min>\r\n"
        f"      {region}\r\n"
        "      <Pattern><Shift>N</Shift><Shift>E</Shift></Pattern>\r\n"
        "    </Match>"
    )
    path = tmp_path / "min_sense.ros"
    path.write_text(xml[:lo] + block + xml[hi:])
    return path


def _pinned_min_sense(tmp_path: Path, region: str, count: int) -> tuple[Solution, int]:
    """Solve the variant pinned to the published roster; return it and the scorer's total."""
    path = _with_min_sense_match(tmp_path, region, count)
    variant = parse_instance(path)
    solution = solve((variant, _options(path, PUBLISHED_ROSTER)))
    if not solution.roster:
        return solution, -1
    return solution, score(variant, solution.roster).total


def test_min_sense_match_with_no_candidate_window_is_still_charged(tmp_path) -> None:
    """A region too narrow to hold the pattern leaves zero windows, so the
    observed count is 0 and a Min limit is violated by its full threshold.
    Skipping the term made the model report 29 where the scorer said 50."""
    solution, recomputed = _pinned_min_sense(tmp_path, "<RegionStart>27</RegionStart>", 3)

    assert solution.objective == recomputed == PUBLISHED_TOTAL + 3 * 7


def test_min_sense_match_threshold_above_the_window_count_stays_feasible(
    tmp_path,
) -> None:
    """Two windows against a threshold of 5: the excess reaches 5, so a penalty
    variable capped at the window count cannot hold it and the whole instance
    comes back INFEASIBLE."""
    solution, recomputed = _pinned_min_sense(
        tmp_path, "<RegionStart>0</RegionStart><RegionEnd>2</RegionEnd>", 5
    )

    assert solution.objective == recomputed == PUBLISHED_TOTAL + 5 * 7


# -- the checker protocol ------------------------------------------------------


def _payload(roster, objective, problem=None, solver_status="optimal") -> dict:
    return {
        "problem": problem,
        "solution": {"roster": roster},
        "objective": objective,
        "solver_status": solver_status,
    }


def test_checker_accepts_a_roster_whose_objective_matches(published_roster) -> None:
    result = check_payload(_payload(published_roster, PUBLISHED_TOTAL))

    assert result["status"] == "accepted", result["errors"]


def test_checker_rejects_an_objective_the_roster_does_not_support(
    published_roster,
) -> None:
    """The disagreement the example exists to catch: a model that under-counts
    its own penalties reports a better number than its roster earns."""
    result = check_payload(_payload(published_roster, PUBLISHED_TOTAL - 5))

    assert result["status"] == "rejected"


def test_checker_resolves_a_ros_path_in_problem(published_roster) -> None:
    result = check_payload(_payload(published_roster, PUBLISHED_TOTAL, problem="QMC-2.ros"))

    assert result["status"] == "accepted", result["errors"]


def test_checker_errors_when_problem_is_not_a_ros_path(published_roster) -> None:
    """Falling back to the bundled instance would grade a payload that names a
    different problem against QMC-2's rules and return a confident verdict."""
    result = check_payload(
        _payload(published_roster, PUBLISHED_TOTAL, problem="somewhere/else.txt")
    )

    assert result["status"] == "error"
