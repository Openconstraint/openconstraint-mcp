"""Parse a SchedulingPeriod-3.0 `.roster` solution file into roster JSON.

Moved out of scorer.py so the roster JSON produced here is the only roster
input scorer.py, checker.py, verify_model.py and every model's `--fix-roster`
need at runtime -- none of them touch XML. See `parse_instance.py` for the
instance side of the same split.

Run from the repository root:
    uv run examples/nurse_rostering/parse_roster.py QMC-2.Solution.29.roster QMC-2.ros
    uv run examples/nurse_rostering/parse_roster.py BCV-3.46.2.Solution.894.roster BCV-3.46.2.ros
    uv run examples/nurse_rostering/parse_roster.py ERMGH.Solution.779.roster ERMGH.ros
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))

from instance import Instance  # noqa: E402
from parse_instance import parse_instance  # noqa: E402
from roster import Roster  # noqa: E402

OFF: str = "-"


def read_roster_xml(path: Path, instance: Instance) -> Roster:
    """Read a published `.roster` solution file into the grid form."""
    root: ET.Element = ET.parse(path).getroot()
    roster: Roster = {e.id: [OFF] * instance.num_days for e in instance.employees}
    for employee in root.findall("Employee"):
        employee_id: str = employee.get("ID") or ""
        for assign in employee.findall("Assign"):
            day: int = int((assign.findtext("Day") or "").strip())
            shift: str = (assign.findtext("Shift") or "").strip()
            if shift and shift != OFF:
                roster[employee_id][day] = shift
    return roster


def main() -> None:
    here: Path = Path(__file__).parent
    roster_arg: str = sys.argv[1] if len(sys.argv) > 1 else "QMC-2.Solution.29.roster"
    roster_path: Path = here / roster_arg
    instance_arg: str = sys.argv[2] if len(sys.argv) > 2 else "QMC-2.ros"
    instance: Instance = parse_instance(here / instance_arg)

    roster: Roster = read_roster_xml(roster_path, instance)
    out_path: Path = roster_path.with_suffix(".json")
    # Compact, matching parse_instance.py's model_dump_json(): the plan commits
    # to the JSON staying smaller than the XML beside it, and indent would
    # break that for ERMGH (a 410 KB instance).
    out_path.write_text(json.dumps(roster) + "\n")
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
