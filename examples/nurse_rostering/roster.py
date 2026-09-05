"""The roster shape every consumer reads: `roster[employee_id][day]`.

`Roster` is a plain grid, not a record class -- there is no invariant beyond
"dict of lists of strings" for a class to enforce, so a `TypeAdapter` loads it
without inventing a wrapper type. `parse_roster.py` builds the JSON this reads
from a `.roster` XML file; nothing downstream of `load_roster` touches XML.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import ConfigDict, TypeAdapter

Roster = dict[str, list[str]]

EXAMPLE_DIR: Path = Path(__file__).parent

# strict=True to match instance.py's FrozenModel -- no stated reason for the
# two loaders in this migration to have different coercion postures.
_ROSTER_ADAPTER: TypeAdapter[Roster] = TypeAdapter(Roster, config=ConfigDict(strict=True))


def load_roster(json_path: Path) -> Roster:
    return _ROSTER_ADAPTER.validate_json(json_path.read_text())


def resolve_roster_path(name: str) -> Path:
    """Locate a roster argument, which may be JSON or CSV.

    Converted rosters live in `parsed/`, but the two sample CSV rosters stayed
    at the example root when the data split into `data/` and `parsed/`. Trying
    only `parsed/` broke every documented CSV invocation from the repository
    root -- `scorer.py solution.csv` and `model.py --fix-roster solution.csv`
    died with FileNotFoundError while the same command worked from inside this
    directory. Both roots are tried, so where a roster lives is not something a
    caller has to know.

    A path that already resolves against the working directory wins, and an
    unresolvable name still returns the `parsed/` candidate so the failure
    names the directory a converted roster is expected in.
    """
    candidate: Path = Path(name)
    if candidate.exists():
        return candidate
    for base in (EXAMPLE_DIR / "parsed", EXAMPLE_DIR):
        resolved: Path = base / name
        if resolved.exists():
            return resolved
    return EXAMPLE_DIR / "parsed" / name
