"""Reference CP-SAT model and data contract for Online Printing Shop instances.

Loads a canonical OPS JSON file (default: ``data_sops1.json``), minimizes
makespan, and emits the openconstraint-mcp CP-SAT JSON envelope.
"""

import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal, Self

from ortools.sat.python import cp_model
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)


def _exact_theta(value: Any) -> Any:
    """Widen a JSON integer literal to Decimal, leaving every other type to fail.

    ``read_input`` parses JSON fractions with ``parse_float=Decimal``, so a float
    here means the value already lost precision somewhere else; strict validation
    rejects it rather than silently accepting the rounded number.
    """

    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    return value


Identifier = Annotated[str, StringConstraints(min_length=1)]
TimeTick = Annotated[int, Field(ge=0)]
Theta = Annotated[Decimal, BeforeValidator(_exact_theta), Field(gt=0, le=1)]
CpsatIntVar = cp_model.IntVar
CpsatIntervalVar = cp_model.IntervalVar
CpsatLiteral = cp_model.LiteralT


class ClosedModel(BaseModel):
    """Base for strict objects that reject misspelled or unsupported fields."""

    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Provenance(ClosedModel):
    """Human-readable origin and stated license status for an instance."""

    source: Identifier
    license: Identifier


class UnavailabilityInterval(ClosedModel):
    """A machine outage delimited by nonnegative integer time ticks."""

    start: TimeTick
    end: TimeTick

    @model_validator(mode="after")
    def validate_interval(self) -> Self:
        if self.end <= self.start:
            raise ValueError("unavailability end must be greater than start")
        return self


class SetupTimes(ClosedModel):
    """Explicit setup durations for first and ordered subsequent operations."""

    first: dict[Identifier, TimeTick] = Field(
        description=(
            "Setup duration for each operation when it is the first one processed on this machine."
        )
    )
    transitions: dict[Identifier, dict[Identifier, TimeTick]] = Field(
        description=(
            "Setup duration keyed by predecessor then successor: transitions[a][b] applies when "
            "operation b runs immediately after operation a on this machine."
        )
    )


class Machine(ClosedModel):
    """A machine calendar and its operation-specific setup durations."""

    unavailability: list[UnavailabilityInterval] = Field(
        description=(
            "Ordered machine outages. An operation may finish at an outage start and start "
            "at its end, but may not start at its start or finish at its end. Setups must not "
            "overlap an outage."
        )
    )
    setup_times: SetupTimes

    @model_validator(mode="after")
    def validate_unavailability(self) -> Self:
        for previous, current in zip(self.unavailability, self.unavailability[1:], strict=False):
            if current.start < previous.end:
                raise ValueError("unavailability intervals must be ordered and nonoverlapping")
        return self


class MachineOption(ClosedModel):
    """Processing time when an operation is assigned to this machine."""

    processing_time: TimeTick


class FixedOperation(ClosedModel):
    """A preassigned eligible machine and start time."""

    machine: Identifier
    start: TimeTick


class Operation(ClosedModel):
    """A mandatory OPS operation and its direct precedence successors."""

    job: Identifier
    successors: list[Identifier]
    machine_options: Annotated[dict[Identifier, MachineOption], Field(min_length=1)]
    release_time: TimeTick
    theta: Theta = Field(
        description=(
            "Fraction of this operation's processing required before a direct successor may "
            "start. A successor also may not finish before this operation finishes."
        )
    )
    fixed: FixedOperation | None = None


class OPSInstance(ClosedModel):
    """Versioned, solver-ready Online Printing Shop problem instance."""

    format: Literal["openconstraint.ops.instance"]
    format_version: Literal["1.0"]
    provenance: Provenance
    machines: Annotated[dict[Identifier, Machine], Field(min_length=1)]
    operations: Annotated[dict[Identifier, Operation], Field(min_length=1)]

    @model_validator(mode="after")
    def validate_semantics(self) -> Self:
        operation_ids: set[str] = set(self.operations)
        machine_ids: set[str] = set(self.machines)

        indegree: dict[str, int] = dict.fromkeys(operation_ids, 0)
        for operation_id, operation in self.operations.items():
            if len(operation.successors) != len(set(operation.successors)):
                raise ValueError(f"operation {operation_id!r} contains duplicate successors")
            for successor_id in operation.successors:
                successor: Operation | None = self.operations.get(successor_id)
                if successor is None:
                    raise ValueError(
                        f"operation {operation_id!r} references unknown successor {successor_id!r}"
                    )
                if successor.job != operation.job:
                    raise ValueError(
                        f"operation {operation_id!r} has successor outside job {operation.job!r}"
                    )
                indegree[successor_id] += 1

            unknown_machines: set[str] = set(operation.machine_options) - machine_ids
            if unknown_machines:
                raise ValueError(
                    f"operation {operation_id!r} references unknown machines: "
                    f"{sorted(unknown_machines)}"
                )
            if operation.fixed is not None:
                if operation.fixed.machine not in operation.machine_options:
                    raise ValueError(f"operation {operation_id!r} fixed machine is not eligible")
                if operation.fixed.start < operation.release_time:
                    raise ValueError(
                        f"operation {operation_id!r} fixed start precedes its release time"
                    )

        ready: list[str] = [
            operation_id for operation_id, degree in indegree.items() if degree == 0
        ]
        visited: int = 0
        while ready:
            operation_id = ready.pop()
            visited += 1
            for successor_id in self.operations[operation_id].successors:
                indegree[successor_id] -= 1
                if indegree[successor_id] == 0:
                    ready.append(successor_id)
        if visited != len(operation_ids):
            raise ValueError("operation precedence graph must be acyclic")

        for machine_id, machine in self.machines.items():
            eligible_operation_ids: set[str] = {
                operation_id
                for operation_id, operation in self.operations.items()
                if machine_id in operation.machine_options
            }
            if set(machine.setup_times.first) != eligible_operation_ids:
                raise ValueError(
                    f"machine {machine_id!r} first setup entries must match eligible operations"
                )
            expected_sources: set[str] = (
                eligible_operation_ids if len(eligible_operation_ids) > 1 else set()
            )
            if set(machine.setup_times.transitions) != expected_sources:
                raise ValueError(
                    f"machine {machine_id!r} transition sources must match eligible operations"
                )
            for source_id, targets in machine.setup_times.transitions.items():
                if set(targets) != eligible_operation_ids - {source_id}:
                    raise ValueError(
                        f"machine {machine_id!r} transitions from {source_id!r} must cover "
                        "every other eligible operation"
                    )

        return self


class ScheduledOperation(ClosedModel):
    """One complete operation decision in a solved OPS schedule."""

    operation: Identifier
    job: Identifier
    machine: Identifier
    # Immediate predecessor on the selected machine, not a fixed job predecessor.
    predecessor: Identifier | None
    setup_start: TimeTick
    setup_duration: TimeTick
    start: TimeTick
    processing_time: TimeTick
    theta_completion_time: TimeTick
    end: TimeTick


class Solution(ClosedModel):
    """Typed boundary between the solver and the stdout serializer."""

    status: Literal["optimal", "feasible", "infeasible", "unknown", "error"]
    schedule: list[ScheduledOperation] | None = None
    objective: int | None = None
    best_objective_bound: float | None = None


def read_input(data_path: Path) -> dict[str, Any]:
    """Read one raw OPS instance object from a JSON file."""

    # parse_float matches checker.py, so theta keeps every digit the file states
    # and both sides derive the same ceil(theta * processing_time).
    raw: dict[str, Any] = json.loads(data_path.read_text(encoding="utf-8"), parse_float=Decimal)
    return raw


def parse_input(raw: dict[str, Any]) -> OPSInstance:
    """Validate a raw object and return the typed solver input record."""

    return OPSInstance.model_validate(raw)


def _data_path() -> Path:
    filename: str = sys.argv[1] if len(sys.argv) > 1 else "data_sops1.json"
    return Path(__file__).parent / filename


def _solver_config() -> dict[str, Any]:
    """Return the caller's CP-SAT config object, or an empty one when unset.

    ``run_cpsat_python_*`` writes its ``config`` argument to a JSON file and
    points ``OPENCONSTRAINT_MCP_CPSAT_CONFIG`` at it. Reading it once here keeps
    every honoured key resolving through the same file.
    """

    config_path: str | None = os.environ.get("OPENCONSTRAINT_MCP_CPSAT_CONFIG")
    if not config_path:
        return {}
    config: dict[str, Any] = json.loads(Path(config_path).read_text(encoding="utf-8"))
    return config


def _search_time_limit_seconds(config: dict[str, Any]) -> float | None:
    """Return the caller's CP-SAT SEARCH limit, or None to search unbounded.

    Bounds ``solver.solve`` only — not reading the instance, building the model,
    or serializing the schedule. Without a ``search_time_limit_seconds`` entry
    the search stays unbounded: the small instances prove optimality in well
    under a second, while a larger one needs a limit to return a clean status
    instead of being killed at the tool's ``script_timeout_ms``.
    """

    limit: Any = config.get("search_time_limit_seconds")
    return None if limit is None else float(limit)


def _num_workers(config: dict[str, Any]) -> int:
    """Return the caller's CP-SAT worker count, defaulting to one.

    One worker keeps a seeded run reproducible, and that is enough to prove the
    small instances optimal. The largest instance needs CP-SAT's parallel
    portfolio to find any incumbent at all, so the caller can raise this the
    same way it sets the time limit.
    """

    workers: Any = config.get("num_workers")
    return 1 if workers is None else int(workers)


def _horizon(instance: OPSInstance) -> int:
    anchor: int = max(
        [operation.release_time for operation in instance.operations.values()]
        + [
            operation.fixed.start
            for operation in instance.operations.values()
            if operation.fixed is not None
        ]
        + [gap.end for machine in instance.machines.values() for gap in machine.unavailability]
        + [0]
    )
    work: int = 0
    for operation_id, operation in instance.operations.items():
        processing: int = max(
            option.processing_time for option in operation.machine_options.values()
        )
        setup: int = max(
            duration
            for machine_id in operation.machine_options
            for duration in (
                [instance.machines[machine_id].setup_times.first[operation_id]]
                + [
                    targets[operation_id]
                    for targets in instance.machines[machine_id].setup_times.transitions.values()
                    if operation_id in targets
                ]
            )
        )
        work += processing + setup
    return anchor + work


def _required_processing(theta: Decimal, processing_time: int) -> int:
    """Return ceil(theta * processing_time) exactly, whatever the decimal context.

    Decimal *stores* every digit but *multiplies* at the active context precision
    (28 significant digits by default), which rounds a product just above an
    integer down onto it. Reading theta as a coefficient/exponent ratio keeps the
    ceiling exact for any theta an instance file states, and matches checker.py.
    """

    numerator, denominator = theta.as_integer_ratio()
    return -(-numerator * processing_time // denominator)


def _add_resumable_duration(
    model: cp_model.CpModel,
    start: CpsatIntVar,
    finish: CpsatIntVar,
    duration: int,
    unavailability: list[UnavailabilityInterval],
    presence: CpsatIntVar,
    name: str,
) -> None:
    """Bind elapsed work to a calendar-aware finish when ``presence`` is true."""

    # One expression per outage: its duration when processing crosses the
    # entire outage, otherwise zero. Their sum extends wall-clock duration.
    skipped_time: list[cp_model.LinearExpr] = []
    for gap_index, gap in enumerate(unavailability):
        start_after: CpsatIntVar = model.new_bool_var(f"{name}_start_after_{gap_index}")
        finish_after: CpsatIntVar = model.new_bool_var(f"{name}_finish_after_{gap_index}")

        model.add(start >= gap.end).only_enforce_if(presence, start_after)
        model.add(start < gap.start).only_enforce_if(presence, start_after.Not())
        model.add(finish > gap.end).only_enforce_if(presence, finish_after)
        model.add(finish <= gap.start).only_enforce_if(presence, finish_after.Not())
        skipped_time.append((gap.end - gap.start) * (finish_after - start_after))

    model.add(finish - start == duration + sum(skipped_time)).only_enforce_if(presence)


def solve(instance: OPSInstance) -> Solution:
    """Build and solve the main CP-SAT model for OPS makespan minimization.

    Each operation chooses exactly one eligible machine. Processing may pause
    during that machine's outages, while setup must occupy one uninterrupted
    interval immediately before processing. A circuit constraint orders the
    operations chosen for each machine; its selected arcs also choose the
    sequence-dependent setup durations. Job successors may start after a
    specified fraction of their predecessor is processed, and the objective is
    the latest operation completion time.
    """

    model: cp_model.CpModel = cp_model.CpModel()
    horizon: int = _horizon(instance)
    operation_ids: list[str] = list(instance.operations)

    # Per-operation time variables, all keyed by operation ID. Keeping them in
    # maps lets precedence, machine-order, objective, and output code refer to
    # the same variables after the operation-creation loop has finished.
    processing_starts: dict[str, CpsatIntVar] = {}
    theta_completion_times: dict[str, CpsatIntVar] = {}
    processing_ends: dict[str, CpsatIntVar] = {}
    setup_starts: dict[str, CpsatIntVar] = {}
    setup_durations: dict[str, CpsatIntVar] = {}

    # (operation ID, eligible machine ID) -> Boolean assignment variable.
    assignments: dict[tuple[str, str], CpsatIntVar] = {}

    # (operation ID, eligible machine ID) -> possible incoming machine-sequence
    # arcs. Each entry is (candidate machine predecessor ID, arc literal);
    # predecessor None means the depot/first-operation arc. The selected entry
    # is read back in the output.
    machine_incoming_arcs: dict[tuple[str, str], list[tuple[str | None, CpsatIntVar]]] = {}

    # Machine ID -> optional setup intervals for operations eligible there.
    # Every eligible operation has one, but only assigned intervals are active;
    # they later share NoOverlap with the machine's outages.
    machine_setup_intervals: dict[str, list[CpsatIntervalVar]] = {
        machine_id: [] for machine_id in instance.machines
    }

    # One set of times is shared by all machine alternatives for an operation.
    # The selected alternative activates the calendar and duration constraints
    # for its machine; the other alternatives leave those constraints inactive.
    for operation_index, (operation_id, operation) in enumerate(instance.operations.items()):
        suffix: str = str(operation_index)
        processing_start: CpsatIntVar = model.new_int_var(
            operation.release_time, horizon, f"processing_start_{suffix}"
        )
        theta_completion_time: CpsatIntVar = model.new_int_var(
            operation.release_time, horizon, f"theta_completion_time_{suffix}"
        )
        processing_end: CpsatIntVar = model.new_int_var(
            operation.release_time, horizon, f"processing_end_{suffix}"
        )
        setup_start: CpsatIntVar = model.new_int_var(0, horizon, f"setup_start_{suffix}")
        setup_duration: CpsatIntVar = model.new_int_var(0, horizon, f"setup_duration_{suffix}")
        processing_starts[operation_id] = processing_start
        theta_completion_times[operation_id] = theta_completion_time
        processing_ends[operation_id] = processing_end
        setup_starts[operation_id] = setup_start
        setup_durations[operation_id] = setup_duration
        # These timestamps are ordered landmarks in the same resumable process:
        # processing start <= theta-fraction completion <= full completion.
        model.add(processing_start <= theta_completion_time)
        model.add(theta_completion_time <= processing_end)

        # Assignment literals for this operation, consumed by AddExactlyOne.
        alternatives: list[CpsatIntVar] = []
        for machine_index, (machine_id, option) in enumerate(operation.machine_options.items()):
            is_assigned: CpsatIntVar = model.new_bool_var(f"is_assigned_{suffix}_{machine_index}")
            assignments[operation_id, machine_id] = is_assigned
            machine_incoming_arcs[operation_id, machine_id] = []
            alternatives.append(is_assigned)

            machine: Machine = instance.machines[machine_id]
            # Both completion times advance through active machine time only.
            # Outages crossed after processing starts extend elapsed time.
            _add_resumable_duration(
                model,
                processing_start,
                theta_completion_time,
                _required_processing(operation.theta, option.processing_time),
                machine.unavailability,
                is_assigned,
                f"theta_completion_time_{suffix}_{machine_index}",
            )
            _add_resumable_duration(
                model,
                processing_start,
                processing_end,
                option.processing_time,
                machine.unavailability,
                is_assigned,
                f"processing_end_{suffix}_{machine_index}",
            )
            machine_setup_intervals[machine_id].append(
                # A selected operation's setup ends exactly when processing starts.
                # Its duration is fixed later by the operation's incoming circuit arc.
                model.new_optional_interval_var(
                    setup_start,
                    setup_duration,
                    processing_start,
                    is_assigned,
                    f"setup_{suffix}_{machine_index}",
                )
            )
        # Every operation is mandatory and uses exactly one eligible machine.
        model.add_exactly_one(alternatives)
        if operation.fixed is not None:
            # A fixed operation still participates in its machine's sequence.
            model.add(assignments[operation_id, operation.fixed.machine] == 1)
            model.add(processing_start == operation.fixed.start)

    # These fixed job-precedence relations come directly from JSON successors.
    # A successor may overlap its predecessor after the required theta fraction
    # is processed, but may not finish before its predecessor.
    for operation_id, operation in instance.operations.items():
        for successor_id in operation.successors:
            model.add(processing_starts[successor_id] >= theta_completion_times[operation_id])
            model.add(processing_ends[successor_id] >= processing_ends[operation_id])

    # Build one optional sequence per machine. AddCircuit sees a depot (node 0)
    # plus one node for every operation eligible for the machine. Selected
    # operations form a single 0 -> first -> ... -> last -> 0 cycle; operations
    # assigned to other machines are excluded from that cycle by self-loops.
    for machine_index, (machine_id, machine) in enumerate(instance.machines.items()):
        # Operation IDs that have this machine among their allowed alternatives.
        eligible_operation_ids: list[str] = [
            operation_id
            for operation_id in operation_ids
            if machine_id in instance.operations[operation_id].machine_options
        ]
        # Circuit node 0 is reserved for the depot, so operation nodes start at 1.
        node_index: dict[str, int] = {
            operation_id: eligible_operation_index
            for eligible_operation_index, operation_id in enumerate(eligible_operation_ids, start=1)
        }
        # Assignment literals for all eligible operations on this machine. Their
        # sum is the number actually assigned here, not the number merely eligible.
        machine_assignments: list[CpsatIntVar] = [
            assignments[operation_id, machine_id] for operation_id in eligible_operation_ids
        ]
        machine_unused: CpsatIntVar = model.new_bool_var(f"machine_unused_{machine_index}")
        # Together these implications make machine_unused equivalent to
        # "none of this machine's eligible operations was assigned here."
        model.add(sum(machine_assignments) == 0).only_enforce_if(machine_unused)
        model.add(sum(machine_assignments) >= 1).only_enforce_if(machine_unused.Not())
        # Each arc is (tail node, head node, selected literal). If the machine is
        # unused, the depot's 0 -> 0 self-loop is the complete circuit.
        sequence_arcs: list[tuple[int, int, CpsatLiteral]] = [(0, 0, machine_unused)]

        for operation_id in eligible_operation_ids:
            is_assigned = assignments[operation_id, machine_id]
            operation_node: int = node_index[operation_id]
            # An operation not assigned to this machine takes its own self-loop,
            # removing it from the depot cycle required by AddCircuit.
            sequence_arcs.append((operation_node, operation_node, is_assigned.Not()))

            # A depot -> operation arc makes this the first assigned operation and
            # therefore selects the machine's first-operation setup duration.
            is_first: CpsatIntVar = model.new_bool_var(f"is_first_{machine_index}_{operation_node}")
            sequence_arcs.append((0, operation_node, is_first))
            # Output bookkeeping only: if this arc wins, extraction prints no
            # predecessor. The is_first literal itself still constrains the model.
            machine_incoming_arcs[operation_id, machine_id].append((None, is_first))
            first_setup: int = machine.setup_times.first[operation_id]
            model.add(setup_durations[operation_id] == first_setup).only_enforce_if(is_first)
            model.add(
                setup_starts[operation_id] + first_setup == processing_starts[operation_id]
            ).only_enforce_if(is_first)

            # An operation -> depot arc makes this the last assigned operation.
            is_last: CpsatIntVar = model.new_bool_var(f"is_last_{machine_index}_{operation_node}")
            sequence_arcs.append((operation_node, 0, is_last))

        # Unlike fixed JSON job precedence, these arcs enumerate every possible
        # immediate adjacency on this machine. Selecting one fixes the successor's
        # setup duration and places it after the selected machine predecessor.
        # The resulting circuit order prevents machine work from overlapping.
        for candidate_machine_predecessor_id in eligible_operation_ids:
            for candidate_machine_successor_id in eligible_operation_ids:
                if candidate_machine_predecessor_id == candidate_machine_successor_id:
                    continue
                is_machine_transition: CpsatIntVar = model.new_bool_var(
                    f"arc_{machine_index}_{node_index[candidate_machine_predecessor_id]}_"
                    f"{node_index[candidate_machine_successor_id]}"
                )
                sequence_arcs.append(
                    (
                        node_index[candidate_machine_predecessor_id],
                        node_index[candidate_machine_successor_id],
                        is_machine_transition,
                    )
                )
                # Keep the incoming arc and its predecessor for solution extraction.
                # The arc literal itself also drives the constraints below.
                machine_incoming_arcs[candidate_machine_successor_id, machine_id].append(
                    (candidate_machine_predecessor_id, is_machine_transition)
                )
                machine_transition_setup_duration: int = machine.setup_times.transitions[
                    candidate_machine_predecessor_id
                ][candidate_machine_successor_id]
                model.add(
                    setup_durations[candidate_machine_successor_id]
                    == machine_transition_setup_duration
                ).only_enforce_if(is_machine_transition)
                model.add(
                    setup_starts[candidate_machine_successor_id] + machine_transition_setup_duration
                    == processing_starts[candidate_machine_successor_id]
                ).only_enforce_if(is_machine_transition)
                model.add(
                    processing_ends[candidate_machine_predecessor_id]
                    <= setup_starts[candidate_machine_successor_id]
                ).only_enforce_if(is_machine_transition)

        model.add_circuit(sequence_arcs)
        # Circuit transitions already serialize assigned operations. NoOverlap
        # additionally keeps each non-resumable setup outside machine outages.
        # These fixed intervals are the machine's unavailable calendar blocks.
        outage_intervals: list[CpsatIntervalVar] = [
            model.new_fixed_size_interval_var(
                gap.start,
                gap.end - gap.start,
                f"outage_{machine_index}_{gap_index}",
            )
            for gap_index, gap in enumerate(machine.unavailability)
        ]
        model.add_no_overlap(machine_setup_intervals[machine_id] + outage_intervals)

    # Minimize the latest processing completion across all operations.
    makespan: CpsatIntVar = model.new_int_var(0, horizon, "makespan")
    model.add_max_equality(makespan, list(processing_ends.values()))
    model.minimize(makespan)

    def extract_schedule(
        reader: cp_model.CpSolver | cp_model.CpSolverSolutionCallback,
    ) -> list[ScheduledOperation]:
        """Translate selected assignment and sequence arcs into output records."""

        schedule: list[ScheduledOperation] = []
        for operation_id, operation in instance.operations.items():
            machine_id: str = next(
                machine_id
                for machine_id in operation.machine_options
                if reader.boolean_value(assignments[operation_id, machine_id])
            )
            machine_predecessor: str | None = next(
                candidate_machine_predecessor
                for candidate_machine_predecessor, is_machine_transition in machine_incoming_arcs[
                    operation_id, machine_id
                ]
                if reader.boolean_value(is_machine_transition)
            )
            schedule.append(
                ScheduledOperation(
                    operation=operation_id,
                    job=operation.job,
                    machine=machine_id,
                    predecessor=machine_predecessor,
                    setup_start=reader.value(setup_starts[operation_id]),
                    setup_duration=reader.value(setup_durations[operation_id]),
                    start=reader.value(processing_starts[operation_id]),
                    processing_time=operation.machine_options[machine_id].processing_time,
                    theta_completion_time=reader.value(theta_completion_times[operation_id]),
                    end=reader.value(processing_ends[operation_id]),
                )
            )
        return schedule

    class _BestSolution(cp_model.CpSolverSolutionCallback):
        def on_solution_callback(self) -> None:
            # Stream each improving incumbent; the final return below repeats the
            # terminal solution as the script's last stdout envelope.
            write_output(
                serialize_solution(
                    Solution(
                        status="feasible",
                        schedule=extract_schedule(self),
                        objective=self.value(makespan),
                        best_objective_bound=float(self.best_objective_bound),
                    )
                )
            )

    config: dict[str, Any] = _solver_config()
    solver: cp_model.CpSolver = cp_model.CpSolver()
    solver.parameters.random_seed = int(os.environ.get("OPENCONSTRAINT_MCP_CPSAT_SEED", "42"))
    solver.parameters.num_workers = _num_workers(config)
    search_time_limit_seconds: float | None = _search_time_limit_seconds(config)
    if search_time_limit_seconds is not None:
        solver.parameters.max_time_in_seconds = search_time_limit_seconds
    # Intermediate envelopes exist for one reason: a child killed at the tool's
    # script_timeout_ms leaves only stdout behind, and the executor recovers the
    # last complete block from it. A search limit does NOT make that redundant —
    # it bounds CP-SAT's search alone, not input parsing, model building, or
    # serialization, and nothing validates it against the executor's deadline,
    # which this script cannot observe. So it can never prove the solve returns
    # first, and streaming stays unconditional. The cost is bounded in practice:
    # improvements taper off, so data_lops.json on 8 workers plateaus near
    # 0.75 MiB of the 1 MiB cap, adding one envelope between 120 s and 210 s.
    status_code: cp_model.CpSolverStatus = solver.solve(model, _BestSolution())
    status_map: dict[
        cp_model.CpSolverStatus, Literal["optimal", "feasible", "infeasible", "unknown", "error"]
    ] = {
        cp_model.OPTIMAL: "optimal",
        cp_model.FEASIBLE: "feasible",
        cp_model.INFEASIBLE: "infeasible",
        cp_model.UNKNOWN: "unknown",
    }
    has_solution: bool = status_code in (cp_model.OPTIMAL, cp_model.FEASIBLE)
    bound_states: tuple[cp_model.CpSolverStatus, ...] = (
        cp_model.OPTIMAL,
        cp_model.FEASIBLE,
        cp_model.UNKNOWN,
    )
    return Solution(
        status=status_map.get(status_code, "error"),
        schedule=extract_schedule(solver) if has_solution else None,
        objective=solver.value(makespan) if has_solution else None,
        best_objective_bound=(
            float(solver.best_objective_bound) if status_code in bound_states else None
        ),
    )


def serialize_solution(solution: Solution) -> dict[str, Any]:
    payload_solution: dict[str, Any] = {}
    if solution.schedule is not None:
        payload_solution = {
            "makespan": solution.objective,
            "schedule": [entry.model_dump(mode="json") for entry in solution.schedule],
        }
    return {
        "status": solution.status,
        "objective": solution.objective,
        "solution": payload_solution,
        "best_objective_bound": solution.best_objective_bound,
    }


def write_output(payload: dict[str, Any]) -> None:
    print(json.dumps(payload))


def main() -> None:
    data_path: Path = _data_path()
    raw: dict[str, Any] = read_input(data_path)
    instance: OPSInstance = parse_input(raw)
    solution: Solution = solve(instance)
    payload: dict[str, Any] = serialize_solution(solution)
    write_output(payload)


if __name__ == "__main__":
    main()
