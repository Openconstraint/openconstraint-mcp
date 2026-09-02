"""Checker script for model.py.

Wraps the independent scorer in the MCP checker protocol. It re-derives every
penalty from the instance and the emitted roster and rejects unless the model's
reported objective equals the recomputed total -- the disagreement that means
the model's objective and the real scoring rules have drifted apart, regardless
of how good the number looks.

The scoring itself lives in scorer.py and shares no code with model.py. That
independence is the point: a checker built from the model's own penalty
expressions would reproduce the model's misreadings and accept them.

Checker protocol:
- Receives the payload JSON path as sys.argv[1].
- Payload keys: problem (str|null), solution (dict), objective (float|null),
  solver_status (str). Admits solver_status in {"optimal", "feasible",
  "timeout"} and treats every other value as ungradeable. A null `problem`
  selects the bundled QMC-2.ros; a non-null one must be a path to a `.ros` file,
  because grading against a substituted default is worse than not grading.
- Prints exactly one JSON object as its final stdout line:
  {"status": "accepted"|"rejected"|"error", "errors": [...], "details": {}}

The two failing verdicts are split by WHAT failed. "error" means the payload
could not be graded at all -- a missing instance, or a solution that is not a
well-formed roster claim. "rejected" means a well-formed roster WAS graded and
is worse than claimed. Collapsing them is harmful: a client told "rejected" for
a missing `roster` key is pointed at the constraint model when the bug is in the
serializer. ../job_shop/checker.py draws the same line.

Runs standalone: python checker.py <payload.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent))

from parse_instance import Instance, parse_instance  # noqa: E402
from scorer import OFF, Breakdown, score  # noqa: E402

ACCEPT_STATUSES: frozenset[str] = frozenset({"optimal", "feasible", "timeout"})
DEFAULT_INSTANCE: Path = Path(__file__).parent / "QMC-2.ros"


def _verdict(status: str, errors: list[str], details: dict[str, Any]) -> dict[str, Any]:
    return {"status": status, "errors": errors, "details": details}


def _parse_at(path: Path) -> tuple[Instance | None, str | None]:
    if not path.is_file():
        return None, f"instance file not found: {path}"
    try:
        return parse_instance(path), None
    except Exception as exc:  # noqa: BLE001 -- any parse failure is ungradeable
        return None, f"could not parse instance {path}: {exc}"


def _load_instance(problem: object) -> tuple[Instance | None, str | None]:
    """Resolve the instance: a `.ros` path in `problem`, else the sibling QMC-2.ros.

    An ABSENT `problem` means "grade against the bundled QMC-2", which is what a
    `run_cpsat_python_file_checked` call on this example sends. A `problem` that
    is present but names something else is an error, not a licence to substitute
    the default: silently falling back graded a payload against QMC-2's
    contracts, cover blocks and requests, and any same-shaped instance (19
    employees, 28 days, IDs A..S) sails through the roster checks below and
    collects a confident verdict computed from the wrong rules.
    """
    if problem is None:
        return _parse_at(DEFAULT_INSTANCE)
    if not isinstance(problem, str):
        return None, (
            f"payload.problem must be a path to a .ros file or null, got {type(problem).__name__}"
        )

    text: str = problem.strip()
    if not text:
        return _parse_at(DEFAULT_INSTANCE)
    if not text.endswith(".ros"):
        # Truncated: `problem` may carry a whole XML document, and a checker
        # error message is not the place to echo 119 KB of it back.
        shown: str = text if len(text) <= 80 else f"{text[:77]}..."
        return None, (
            f"payload.problem must be a path to a SchedulingPeriod .ros file "
            f"(or null for the bundled {DEFAULT_INSTANCE.name}), got {shown!r}"
        )

    candidate: Path = Path(text)
    return _parse_at(candidate if candidate.is_absolute() else DEFAULT_INSTANCE.parent / candidate)


def _extract_roster(
    solution: object, instance: Instance
) -> tuple[dict[str, list[str]] | None, str | None]:
    if not isinstance(solution, dict):
        return None, "payload.solution is missing or not an object"

    raw: object = solution.get("roster")
    if not isinstance(raw, dict):
        return None, "payload.solution.roster is missing or not an object"

    expected_ids: set[str] = {e.id for e in instance.employees}
    if set(raw) != expected_ids:
        missing: list[str] = sorted(expected_ids - set(raw))
        extra: list[str] = sorted(set(raw) - expected_ids)
        return None, f"roster employees mismatch (missing {missing}, unexpected {extra})"

    valid: set[str] = set(instance.shift_types) | {OFF}
    roster: dict[str, list[str]] = {}
    for employee_id, schedule in raw.items():
        if not isinstance(schedule, list) or len(schedule) != instance.num_days:
            return None, (f"roster row {employee_id} must be a list of {instance.num_days} entries")
        for day, cell in enumerate(schedule):
            # A grid cannot represent a double booking, so the one-shift-per-day
            # rule is enforced by the shape; this rejects anything outside the
            # alphabet that would otherwise score as a silent day off.
            if cell not in valid:
                return None, (
                    f"roster[{employee_id}][{day}] = {cell!r} is not one of {sorted(valid)}"
                )
        roster[employee_id] = list(schedule)
    return roster, None


def check_payload(payload: dict[str, Any]) -> dict[str, Any]:
    solver_status: object = payload.get("solver_status")
    if not isinstance(solver_status, str) or solver_status not in ACCEPT_STATUSES:
        return _verdict("error", [f"solver_status {solver_status!r} is not gradeable"], {})

    instance, instance_error = _load_instance(payload.get("problem"))
    if instance is None:
        return _verdict("error", [instance_error or "instance unavailable"], {})

    roster, roster_error = _extract_roster(payload.get("solution"), instance)
    if roster is None:
        return _verdict("error", [roster_error or "roster unavailable"], {})

    breakdown: Breakdown = score(instance, roster)
    details: dict[str, Any] = {
        "recomputed_total": breakdown.total,
        "by_label": breakdown.by_label,
        "by_cover_type": breakdown.by_cover_type,
        "requests": breakdown.requests,
        "violation_count": len(breakdown.violations),
    }

    errors: list[str] = []
    objective: object = payload.get("objective")
    if not isinstance(objective, int | float) or isinstance(objective, bool):
        errors.append(
            f"objective must be a number equal to the recomputed total "
            f"{breakdown.total}, got {objective!r}"
        )
    elif int(objective) != breakdown.total:
        # The disagreement the whole exercise is designed to catch.
        errors.append(
            f"model objective {int(objective)} does not match the independently "
            f"recomputed total {breakdown.total}"
        )

    return _verdict("accepted" if not errors else "rejected", errors, details)


def main() -> None:
    if len(sys.argv) != 2:
        print(json.dumps(_verdict("error", ["usage: python checker.py <payload.json>"], {})))
        return
    with open(sys.argv[1], encoding="utf-8") as payload_file:
        payload: dict[str, Any] = json.load(payload_file)
    print(json.dumps(check_payload(payload)))


if __name__ == "__main__":
    main()
