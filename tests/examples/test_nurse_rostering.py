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
from examples.nurse_rostering.model import (
    Solution,
    parse_instance,
    solve,
    write_csv,
)
from examples.nurse_rostering.scorer import read_roster_csv, read_roster_xml, score
from examples.nurse_rostering.verify_parse import verify

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


@pytest.fixture(scope="module")
def instance():
    return parse_instance(INSTANCE_PATH)


@pytest.fixture(scope="module")
def published_roster(instance):
    return read_roster_xml(PUBLISHED_ROSTER, instance)


def _options(instance_path: Path, csv_path: Path, fix_roster: str | None = None) -> dict:
    return {
        "instance_path": str(instance_path),
        "csv_path": str(csv_path),
        "time_limit": 60.0,
        "workers": 8,
        "seed": 42,
        "harden": False,
        "fix_roster": fix_roster,
    }


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


def test_model_pinned_to_the_published_roster_agrees_with_the_scorer(
    instance, tmp_path
) -> None:
    """The sharpest cheap test of the penalty structure: pinned to a roster of
    known cost, a model missing a penalty scores it below 29."""
    solution = solve(
        (instance, _options(INSTANCE_PATH, tmp_path / "out.csv", str(PUBLISHED_ROSTER)))
    )

    assert (solution.status, solution.objective) == ("optimal", PUBLISHED_TOTAL)


def test_model_pinned_breakdown_matches_the_scorer_label_for_label(
    instance, tmp_path
) -> None:
    solution = solve(
        (instance, _options(INSTANCE_PATH, tmp_path / "out.csv", str(PUBLISHED_ROSTER)))
    )

    assert _flatten(*solution.breakdown.values()) == PUBLISHED_BREAKDOWN


# -- <Min>-sense <Match> limits ------------------------------------------------
#
# QMC-2 contains none: all 121 of its match limits are <Max>. The model's
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
    solution = solve(
        (variant, _options(path, tmp_path / "out.csv", str(PUBLISHED_ROSTER)))
    )
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


# -- roster CSV round trip -----------------------------------------------------


def test_roster_csv_round_trips_through_the_scorer(
    instance, published_roster, tmp_path
) -> None:
    path = tmp_path / "solution.csv"
    write_csv(published_roster, instance.num_days, path)

    assert read_roster_csv(path, instance) == published_roster


def test_header_only_roster_csv_is_rejected_by_name(instance, tmp_path) -> None:
    """What model.py writes when a run found no incumbent. Read unchecked it
    surfaced as a bare KeyError from a contract lookup, several frames from the
    real problem, which is that the file holds no roster."""
    path = tmp_path / "solution.csv"
    write_csv({}, instance.num_days, path)

    with pytest.raises(ValueError, match="no roster row for employee"):
        read_roster_csv(path, instance)


def test_truncated_roster_csv_row_is_rejected(
    instance, published_roster, tmp_path
) -> None:
    """A short row used to score silently: pattern rules read `len(schedule)`,
    so that employee's penalties were counted over a shortened period."""
    path = tmp_path / "solution.csv"
    write_csv(published_roster, instance.num_days, path)
    lines = path.read_text().splitlines()
    lines[1] = ",".join(lines[1].split(",")[:-3])
    path.write_text("\n".join(lines) + "\n")

    with pytest.raises(ValueError, match="day columns"):
        read_roster_csv(path, instance)
