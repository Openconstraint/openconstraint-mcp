"""Compare a legacy Willtl OPS instance with its solver-ready materialization.

UPSTREAM_JSON is the local path to the original Willtl-format instance.
LOCAL_JSON is the local path to the corresponding solver-ready instance.
The audit reads local files only; it does not download URLs.

Run from the repository root with:
    uv run python -m examples.online_printing_shop.audit_instance UPSTREAM_JSON LOCAL_JSON

For sops1:
    uv run python -m examples.online_printing_shop.audit_instance \
        /tmp/online-printing-shop-upstream/instances/small/sops1.json \
        examples/online_printing_shop/data/data_sops1.json

## How a new chat should use the audit tool

1. Work from the repository root.
2. Read and follow AGENTS.md, then run `just --list` before other project commands.
3. Ensure both arguments name existing local files; URLs are not accepted.
4. Run the module command above with the legacy Willtl JSON first and the
   solver-ready openconstraint.ops.instance JSON second.
5. Report `PASS` or every `FAIL` mismatch and its solver consequence.

The tool validates the local file and compares IDs, job membership, precedence,
machine options, releases, overlap, fixed operations, calendars, and every setup
duration. It audits only: it does not download, convert, or modify either file.
"""

import argparse
from collections import deque
from pathlib import Path
from typing import Any

from examples.online_printing_shop.models import parse_input, read_input


def _setup_duration(source: dict[str, Any], target: dict[str, Any], machine: dict[str, Any]) -> int:
    size_costs = machine["setup_size"]
    duration = size_costs[0] if source["size"] > target["size"] else 0
    duration += size_costs[1] if source["size"] < target["size"] else 0
    duration += machine["setup_color"] if source["color"] != target["color"] else 0
    duration += machine["setup_varnish"] if source["varnish"] != target["varnish"] else 0
    return duration


def audit_instance(upstream: dict[str, Any], local: dict[str, Any]) -> list[str]:
    """Return every solver-relevant mismatch between upstream and local data."""

    errors: list[str] = []

    def check(condition: bool, message: str) -> None:
        if not condition:
            errors.append(message)

    def check_equal(path: str, actual: object, expected: object) -> None:
        if actual != expected:
            errors.append(f"{path}: expected {expected!r}, got {actual!r}")

    machines: dict[str, dict[str, Any]] = {}
    for machine in upstream["resources"]:
        machine_id = str(machine["id"])
        check(machine_id not in machines, f"duplicate upstream machine ID {machine_id!r}")
        machines[machine_id] = machine

    operations: dict[str, dict[str, Any]] = {}
    jobs: dict[str, set[str]] = {}
    for job in upstream["jobs"]:
        job_id = str(job["id"])
        check(job_id not in jobs, f"duplicate upstream job ID {job_id!r}")
        members: set[str] = set()
        for operation in job["topology"]:
            operation_id = str(operation["id"])
            check(
                operation_id not in operations,
                f"duplicate upstream operation ID {operation_id!r}",
            )
            operations[operation_id] = operation
            members.add(operation_id)
        jobs[job_id] = members

    check_equal("machines", set(local["machines"]), set(machines))
    check_equal("operations", set(local["operations"]), set(operations))
    if errors:
        return errors

    edges: dict[str, list[str]] = {}
    indegree = dict.fromkeys(operations, 0)
    for job_id, members in jobs.items():
        for operation_id in members:
            operation = operations[operation_id]
            target = local["operations"][operation_id]
            check_equal(f"operations.{operation_id}.job", target["job"], job_id)

            successors = [str(value) for value in operation["sucessors"]]
            edges[operation_id] = successors
            check_equal(f"operations.{operation_id}.successors", target["successors"], successors)
            check(
                len(successors) == len(set(successors)),
                f"upstream operation {operation_id!r} repeats a successor",
            )
            for successor in successors:
                check(
                    successor in members,
                    f"upstream edge {operation_id}->{successor} leaves job {job_id!r}",
                )
                if successor in indegree:
                    indegree[successor] += 1

            resources = [str(value) for value in operation["resources"]]
            times = operation["time"]
            check(
                len(resources) == len(times),
                f"upstream operation {operation_id!r} resources/time lengths differ",
            )
            check(
                len(resources) == len(set(resources)),
                f"upstream operation {operation_id!r} repeats an eligible machine",
            )
            if len(resources) == len(times):
                expected_options = {
                    machine_id: {"processing_time": processing_time}
                    for machine_id, processing_time in zip(resources, times, strict=True)
                }
                check_equal(
                    f"operations.{operation_id}.machine_options",
                    target["machine_options"],
                    expected_options,
                )
            check_equal(
                f"operations.{operation_id}.release_time",
                target["release_time"],
                operation["release"],
            )
            check_equal(f"operations.{operation_id}.theta", target["theta"], operation["overlap"])

            starting = operation["starting"]
            if starting == -1:
                check("fixed" not in target, f"operations.{operation_id}.fixed must be absent")
            else:
                check(
                    len(resources) == 1,
                    f"fixed upstream operation {operation_id!r} must have one eligible machine",
                )
                if resources:
                    check_equal(
                        f"operations.{operation_id}.fixed",
                        target.get("fixed"),
                        {"machine": resources[0], "start": starting},
                    )

    ready = deque(operation_id for operation_id, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        operation_id = ready.popleft()
        visited += 1
        for successor in edges.get(operation_id, []):
            if successor in indegree:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
    check(visited == len(operations), "upstream precedence graph must be acyclic")

    for machine_id, machine in machines.items():
        availability = machine["availability"]
        check(
            len(availability) % 2 == 0,
            f"upstream machine {machine_id!r} availability length must be even",
        )
        check(
            all(left < right for left, right in zip(availability, availability[1:], strict=False)),
            f"upstream machine {machine_id!r} availability must be strictly ordered",
        )
        if not availability or len(availability) % 2:
            continue

        unavailability = []
        if availability[0] > 0:
            unavailability.append({"start": 0, "end": availability[0]})
        unavailability.extend(
            {"start": availability[index - 1], "end": availability[index]}
            for index in range(2, len(availability), 2)
        )
        check_equal(
            f"machines.{machine_id}.unavailability",
            local["machines"][machine_id]["unavailability"],
            unavailability,
        )

        eligible = {
            operation_id: operation
            for operation_id, operation in operations.items()
            if machine["id"] in operation["resources"]
        }
        first_duration = (
            max(machine["setup_size"]) + machine["setup_color"] + machine["setup_varnish"]
        )
        expected_setup = {
            "first": dict.fromkeys(eligible, first_duration),
            "transitions": (
                {
                    source_id: {
                        target_id: _setup_duration(source, target, machine)
                        for target_id, target in eligible.items()
                        if target_id != source_id
                    }
                    for source_id, source in eligible.items()
                }
                if len(eligible) > 1
                else {}
            ),
        }
        check_equal(
            f"machines.{machine_id}.setup_times",
            local["machines"][machine_id]["setup_times"],
            expected_setup,
        )

    return errors


def main() -> int:
    """Run the audit CLI."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("upstream", type=Path, help="legacy Willtl OPS JSON")
    parser.add_argument("local", type=Path, help="solver-ready OPS JSON")
    args = parser.parse_args()

    upstream = read_input(args.upstream)
    local = read_input(args.local)
    parse_input(local)
    errors = audit_instance(upstream, local)
    if errors:
        print("FAIL")
        print("\n".join(f"- {error}" for error in errors))
        return 1

    print(
        f"PASS: {len(local['machines'])} machines and {len(local['operations'])} operations match"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
