"""Fast gates for `examples/nurse_rostering`.

The example ships `verify_parse.py` and `verify_model.py` as scripts a human
runs by hand, and `verify_model.py`'s third gate solves the instance from
scratch on four seeds -- far too slow for `just check`. This file gates the
parts that are cheap and catch the most: the parse counts, the scorer against
the published cost-29 roster, and the model pinned to that same roster. The
four-seed agreement gate stays in `verify_model.py`.

Every import routes through the example's own modules on purpose. Those modules
put their own directory on `sys.path` and import each other flatly (`from
instance import ...`), so `examples.nurse_rostering.model.load_instance` and a
direct `examples.nurse_rostering.instance` import resolve to two distinct
module objects with two distinct `Instance` classes. Taking `load_instance`
from `model` keeps this file on the same copy the example uses. `parse_instance`
(the XML function, for the parser-fidelity tests below) is taken the same way,
from `verify_parse`, which imports it flatly too. `Roster` is a plain dict, not
a class, so `load_roster` carries no such hazard and is imported directly.

Model/scorer/checker tests read the committed JSON (`load_instance`,
`load_roster`); parser-fidelity tests keep parsing the `.ros`/`.roster` XML
directly (`parse_instance`, `read_roster_xml`), since they exist to catch a
misreading of the XML format, not a JSON round-trip.
"""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from examples.nurse_rostering.checker import check_payload
from examples.nurse_rostering.model import Options, Solution, load_instance, solve
from examples.nurse_rostering.parse_roster import read_roster_xml
from examples.nurse_rostering.roster import load_roster
from examples.nurse_rostering.scorer import score
from examples.nurse_rostering.verify_parse import (
    parse_instance,
    verify,
    verify_ermgh,
    verify_fallbacks,
)

ROOT = Path(__file__).parents[2]
EXAMPLE_DIR = ROOT / "examples" / "nurse_rostering"
DATA_DIR = EXAMPLE_DIR / "data"
PARSED_DIR = EXAMPLE_DIR / "parsed"
INSTANCE_XML_PATH = DATA_DIR / "QMC-2.ros"
INSTANCE_PATH = PARSED_DIR / "QMC-2.json"
PUBLISHED_ROSTER = PARSED_DIR / "QMC-2.Solution.29.json"

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
ERMGH_XML_PATH = DATA_DIR / "ERMGH.ros"
ERMGH_PATH = PARSED_DIR / "ERMGH.json"
ERMGH_ROSTER = PARSED_DIR / "ERMGH.Solution.779.json"
ERMGH_TOTAL = 779
ERMGH_BREAKDOWN = {
    "skill Preferred understaffing": 777,
    "skill Preferred overstaffing": 2,
}


@pytest.fixture(scope="module")
def xml_instance():
    """The XML-parsed instance, for tests exercising the converter itself."""
    return parse_instance(INSTANCE_XML_PATH)


@pytest.fixture(scope="module")
def instance():
    return load_instance(INSTANCE_PATH)


@pytest.fixture(scope="module")
def published_roster():
    return load_roster(PUBLISHED_ROSTER)


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


def test_parse_reproduces_every_published_count(xml_instance) -> None:
    failures = [line for ok, line in verify(xml_instance) if not ok]

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
def ermgh_xml():
    """The XML-parsed instance, for tests exercising the converter itself."""
    return parse_instance(ERMGH_XML_PATH)


@pytest.fixture(scope="module")
def ermgh():
    return load_instance(ERMGH_PATH)


@pytest.fixture(scope="module")
def ermgh_roster():
    return load_roster(ERMGH_ROSTER)


def test_ermgh_parse_reproduces_every_published_count(ermgh_xml) -> None:
    failures = [line for ok, line in verify_ermgh(ermgh_xml) if not ok]

    assert failures == []


def test_ermgh_time_period_cover_resolves_to_the_shifts_on_duty_throughout(ermgh_xml) -> None:
    """Containment, not overlap. E starts at 15:30 and so does not staff
    12:00-15:30; DH runs 12:00-20:00 and does staff 15:30-19:30."""
    resolved = {block.time_period: tuple(block.shifts) for block in ermgh_xml.cover}

    assert resolved == {
        ("07:30:00", "11:30:00"): ("D",),
        ("11:30:00", "12:00:00"): ("D",),
        ("12:00:00", "15:30:00"): ("D", "DH"),
        ("15:30:00", "19:30:00"): ("DH", "E"),
        ("19:30:00", "20:00:00"): ("DH", "E"),
        ("20:00:00", "23:30:00"): ("E",),
        ("23:30:00", "07:30:00"): ("N",),
    }


def test_ermgh_dollar_is_a_wildcard_and_not_a_shift_named_dollar(ermgh_xml) -> None:
    """`<Shift>$</Shift>` must parse to its own kind. As a shift ID it matches
    nothing, every rule scores 0, and the published optimum STILL totals 779 --
    so only the symbol census catches it, never the total."""
    symbols = [
        s for c in ermgh_xml.contracts for m in c.matches for p in m.patterns for s in p.symbols
    ]

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
    return {name: parse_instance(DATA_DIR / name) for name in names}


def test_optional_element_fallbacks(fallback_instances) -> None:
    failures = [line for ok, line in verify_fallbacks(fallback_instances) if not ok]

    assert failures == []


@pytest.mark.parametrize(("name", "roster", "published"), FALLBACK_GOLDEN)
def test_fallback_instance_reproduces_its_published_optimum(name, roster, published) -> None:
    """The gate that makes the fallbacks evidence rather than decoration: each
    default was chosen because it is the value this roster's published cost
    implies, so a changed default moves the total."""
    instance = parse_instance(DATA_DIR / f"{name}.ros")

    total = score(instance, read_roster_xml(DATA_DIR / roster, instance)).total

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
    xml = INSTANCE_XML_PATH.read_text()
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


def test_checker_resolves_a_json_path_in_problem(published_roster) -> None:
    result = check_payload(_payload(published_roster, PUBLISHED_TOTAL, problem="QMC-2.json"))

    assert result["status"] == "accepted", result["errors"]


def test_checker_errors_when_problem_is_not_a_json_path(published_roster) -> None:
    """Falling back to the bundled instance would grade a payload that names a
    different problem against QMC-2's rules and return a confident verdict."""
    result = check_payload(
        _payload(published_roster, PUBLISHED_TOTAL, problem="somewhere/else.txt")
    )

    assert result["status"] == "error"


# -- regeneration ---------------------------------------------------------
#
# Re-parsing every .ros/.roster file must reproduce the committed JSON
# byte-for-byte, so the committed instance/roster files -- the ones every
# runtime script actually reads -- cannot silently drift from the converters
# that produced them.

ALL_INSTANCE_NAMES = (
    "BCDT-Sep",
    "BCV-3.46.2",
    "ERMGH",
    "GPost",
    "Millar-2Shift-DATA1",
    "QMC-1",
    "QMC-2",
)

ALL_ROSTERS = (
    ("BCDT-Sep", "BCDT-Sep.Solution.100.roster"),
    ("BCV-3.46.2", "BCV-3.46.2.Solution.894.roster"),
    ("ERMGH", "ERMGH.Solution.779.roster"),
    ("GPost", "GPost.Solution.5.roster"),
    ("Millar-2Shift-DATA1", "Millar-2Shift-DATA1.Solution.0.roster"),
    ("QMC-1", "QMC-1.Solution.13.roster"),
    ("QMC-2", "QMC-2.Solution.29.roster"),
)


@pytest.mark.parametrize("name", ALL_INSTANCE_NAMES)
def test_instance_json_matches_a_fresh_parse(name) -> None:
    fresh = parse_instance(DATA_DIR / f"{name}.ros").model_dump_json() + "\n"
    committed = (PARSED_DIR / f"{name}.json").read_text()

    assert fresh == committed


@pytest.mark.parametrize(("name", "roster_file"), ALL_ROSTERS)
def test_roster_json_matches_a_fresh_parse(name, roster_file) -> None:
    inst = parse_instance(DATA_DIR / f"{name}.ros")
    fresh = json.dumps(read_roster_xml(DATA_DIR / roster_file, inst)) + "\n"
    committed = (PARSED_DIR / roster_file).with_suffix(".json").read_text()

    assert fresh == committed


# -- pre-migration baseline ----------------------------------------------------
#
# The regeneration tests above compare the POST-migration parser to JSON the
# POST-migration parser produced -- exactly the circular check the plan's task 0
# exists to prevent: a dropped field or a changed default would appear
# identically on both sides and still pass. These digests are the other half:
# they were captured from the PRE-migration parser at BASE_SHA
# (a45afb5945ca1a009c37b6578b83f55484e26014), over the dataclass-based Instance
# and the roster dict that code produced, so a genuine before/after comparison
# survives here even though the record classes were rebuilt as Pydantic models.
#
# The digest is over the CANONICAL form, not the committed bytes: committed
# instance/roster files are `model_dump_json()` / `json.dumps()` output in
# declaration/insertion order, while the baseline was hashed as
# `json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"`. Each test
# below re-canonicalizes the committed artifact the same way before hashing, so
# a real content change still fails it even though key order differs.
#
# Regenerate (read-only, run at BASE_SHA with a clean tree):
#   sys.path.insert(0, ".../examples/nurse_rostering")
#   from parse_instance import parse_instance; from scorer import read_roster_xml
#   canonical(dataclasses.asdict(parse_instance(f"{name}.ros")))  # instances
#   canonical(read_roster_xml(roster_file, instance))             # rosters

BASELINE_INSTANCE_SHA256 = {
    "BCDT-Sep": "19963d6d36d7ed7b9b253b5bedf5a4912f827218828e32d6d3b3dff8b7d4ad80",
    "BCV-3.46.2": "5305f93e14c97b4f8fa97942a268311c2999a26d7a8b692505acb5670a30b72d",
    "ERMGH": "88f4aa27aed563f257520a115eb5e8b8f13e9d822c6223ea29bc04ece0e2d22b",
    "GPost": "48bd6a35bbbfa29eb237159554c0cc14c5265a024ca187e069ed5392f5e5649f",
    "Millar-2Shift-DATA1": "59435d0429cf77121456e1d730c7de8b4625297b21231c8cc82aa185f3d6fe28",
    "QMC-1": "91479a2db386ec8acb4ed0275a7f383a4f1d89ed24ec8d8b408130efc9dcafca",
    "QMC-2": "fc0dd09f91277617f671eca1ce1232e60b95b9cf860670b82a4a78bcc44def40",
}

BASELINE_ROSTER_SHA256 = {
    "BCDT-Sep": "1f26324cbbef7e44962f00abb22bf5ea5551dea6d3e3c9bc2d37a6275fbfda71",
    "BCV-3.46.2": "78f24927f48f63f0db67c42ee515e27b597c593ffbfdc028a3294c718e6ea556",
    "ERMGH": "cdd86e63a7cfdf3531a61aabe81698b0c336a4078ef06467810c2cf78e1c845f",
    "GPost": "acb28390d655c8f76b224a2a6216e14580c29fd804251d462fe025b3e0704abb",
    "Millar-2Shift-DATA1": "392b65837dc3250bd93635108350d3d26e2e6acd24779d7f2a5a32ee25861451",
    "QMC-1": "3a3bdc6f727814b47bb80fa86e7d5745413228b016bedc5d16cab31e80113751",
    "QMC-2": "cb00102199595fcbabc35916180ddf2e86e02abca83036251df5fe82a3851e0a",
}


def _canonical_sha256(payload: object) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False) + "\n"
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.mark.parametrize("name", ALL_INSTANCE_NAMES)
def test_committed_instance_matches_the_pre_migration_baseline(name) -> None:
    payload = json.loads((PARSED_DIR / f"{name}.json").read_text())

    assert _canonical_sha256(payload) == BASELINE_INSTANCE_SHA256[name]


@pytest.mark.parametrize(("name", "roster_file"), ALL_ROSTERS)
def test_committed_roster_matches_the_pre_migration_baseline(name, roster_file) -> None:
    committed_path = (PARSED_DIR / roster_file).with_suffix(".json")
    payload = json.loads(committed_path.read_text())

    assert _canonical_sha256(payload) == BASELINE_ROSTER_SHA256[name]


# -- CLI smoke ------------------------------------------------------------
#
# Every test above imports the example's functions and calls them directly, so
# nothing here exercised argparse, the --instance/--fix-roster defaults, or the
# path resolution that turns a bare filename into a file on disk. That gap let
# a real break ship: after the data split into data/ and parsed/, every
# relative argument resolved against parsed/ alone, so the three CSV
# invocations problem.txt documents died with FileNotFoundError from the
# repository root while working from inside the example directory. These run
# the scripts as scripts, from the repository root, the way problem.txt says to.

DOCUMENTED_SCORER_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("scorer.py", "solution.csv"),
    ("scorer.py", "QMC-2.Solution.29.json"),
    ("scorer.py", "solution_bcv.csv", "BCV-3.46.2.json"),
    ("scorer.py", "BCV-3.46.2.Solution.894.json", "BCV-3.46.2.json"),
    ("scorer.py", "ERMGH.Solution.779.json", "ERMGH.json"),
)


def _run_from_root(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke an example script the way problem.txt documents: from the root."""
    return subprocess.run(
        [sys.executable, str(EXAMPLE_DIR / args[0]), *args[1:]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    "command", DOCUMENTED_SCORER_COMMANDS, ids=[" ".join(c) for c in DOCUMENTED_SCORER_COMMANDS]
)
def test_documented_scorer_command_runs_from_the_repository_root(command) -> None:
    result = _run_from_root(*command)

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("script", ["model.py", "model_bounds.py", "model_regular.py"])
def test_model_cli_pins_a_csv_roster_from_the_repository_root(script) -> None:
    """The CSV branch of --fix-roster, which is where the resolution broke.

    Pinned rather than free-searching so this stays a CLI test, not a solve:
    every assignment is fixed, so the run is deterministic and near-instant.
    """
    result = _run_from_root(script, "--fix-roster", "solution.csv", "--time-limit", "5")

    assert result.returncode == 0, result.stderr


def test_model_cli_pinned_to_the_published_roster_reports_its_cost() -> None:
    result = _run_from_root(
        "model.py", "--fix-roster", "QMC-2.Solution.29.json", "--time-limit", "5"
    )

    assert json.loads(result.stdout)["objective"] == PUBLISHED_TOTAL


@pytest.mark.parametrize("script", ["parse_instance.py", "parse_roster.py"])
def test_converter_cli_runs_from_the_repository_root(script) -> None:
    result = _run_from_root(script)

    assert result.returncode == 0, result.stderr
