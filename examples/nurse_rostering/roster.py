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

# strict=True to match instance.py's FrozenModel -- no stated reason for the
# two loaders in this migration to have different coercion postures.
_ROSTER_ADAPTER: TypeAdapter[Roster] = TypeAdapter(Roster, config=ConfigDict(strict=True))


def load_roster(json_path: Path) -> Roster:
    return _ROSTER_ADAPTER.validate_json(json_path.read_text())
