"""Text embedded into MCP tool results.

Unlike ``descriptions.py`` (documentation a client reads *about* a tool before
calling it), these strings are emitted *by* a tool into ``CallToolResult.content``
and read by the model *after* a call, interleaved with runtime data such as the
statistics table or solver inventory. They are model-visible presentation policy,
not tool documentation. This module also holds the pure result-formatting
functions that build that text from a schema result; ``server.py`` keeps only
the ``CallToolResult`` wrappers that call them. This is a leaf module aside from
those schema types: ``server`` imports it, and it imports nothing else internal.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..schemas.cpsat import CpsatPythonExperimentResult
    from ..schemas.diagnostics import Diagnostic
    from ..schemas.minizinc import SaveVerifiedModelResult, SolveResult, SolverList
    from ..schemas.tabular import TabularData


def _diagnostic_prefix(diagnostic: Diagnostic | None) -> str:
    """Lead model-visible text with a stable ``Diagnostic:`` line, or nothing.

    A client can branch on ``structuredContent``'s ``diagnostic.category``; this
    line makes the same signal visible in the prose the model reads, so a
    non-clean outcome is never buried below stdout/stderr. Clean successes
    (``diagnostic is None``) add no line.
    """
    if diagnostic is None:
        return ""
    return f"Diagnostic: {diagnostic.category} — {diagnostic.message}\n\n"


STATS_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the entire Statistics section below into "
    "the user-facing answer. Do not omit it, summarize it, or replace it with "
    "only selected fields."
)
SOLVER_INVENTORY_PRESENTATION_REQUIREMENT = (
    "Final answer requirement: copy the solver inventory table below into the "
    "user-facing answer. Do not omit rows, convert it to bullets or prose, "
    'summarize, group, or replace rows with phrases like "additional solvers".'
)
SOLVER_NUM_SOLUTIONS_NOTE = (
    "`num_solutions` is supported only by `org.chuffed.chuffed` and "
    "`org.gecode.gecode`; the default `cp-sat` solver does not support it."
)
SOLVER_RUNTIME_CONFIG_CAUTION = (
    "Caution: solver entries come from the MiniZinc runtime configuration. "
    "Commercial or external MIP solvers (CPLEX, Gurobi, Xpress, SCIP, COIN-BC) "
    "may still require separate installed binaries, licenses, or "
    "solver-specific setup to run."
)
SOLUTION_CHECK_NON_ADJUDICATION_NOTE = (
    "Note: checker `CORRECT`/`INCORRECT` text is author output, surfaced "
    "verbatim in each `checks` entry and NOT interpreted by the server; only a "
    "nested UNSATISFIABLE (a constraint-style rejection) counts as a `violation`. "
    "`solve.solutions` includes checker-rejected solutions — on a violation, "
    "consult each solution's `checks`. Only `solve.status` proves "
    "completeness/optimality."
)
SOLVER_CAPABILITY_METADATA_NOTE = (
    "The structured result includes `capabilities.supports_all_solutions`, "
    "`supports_free_search`, `supports_parallel`, `supports_random_seed`, "
    "`supports_num_solutions`, and advisory `std_flags` for each solver."
)

_PREFERRED_STAT_KEYS = (
    "objective",
    "objectiveBound",
    "nSolutions",
    "failures",
    "propagations",
    "solveTime",
)


def format_solve_result_content(result: SolveResult) -> str:
    """Return model-visible solve output that leads with the solution, stats last."""
    lines = [
        f"Status: {result.status}",
        f"Solver: {result.solver}",
        f"Return code: {result.return_code}",
        f"Timed out: {str(result.timed_out).lower()}",
        f"Elapsed: {result.elapsed_ms} ms",
    ]

    if result.stdout:
        lines.extend(["", "Stdout:", result.stdout.rstrip()])
    if result.stderr:
        lines.extend(["", "Stderr:", result.stderr.rstrip()])

    if result.statistics:
        lines.extend(["", STATS_PRESENTATION_REQUIREMENT, "Statistics:"])
        preferred = [k for k in _PREFERRED_STAT_KEYS if k in result.statistics]
        others = [k for k in result.statistics if k not in _PREFERRED_STAT_KEYS]
        lines.extend([f"- {key}: {result.statistics[key]}" for key in (preferred + others)])

    solve_text = "\n".join(lines)
    prefix = _diagnostic_prefix(result.diagnostic)
    if result.checker is None:
        return prefix + solve_text

    violations = sum(1 for check in result.checker.checks if check.violation)
    return prefix + "\n".join(
        [
            f"Checker status: {result.checker.status}",
            f"Solve status: {result.status}",
            f"Solutions produced: {len(result.solutions)}",
            f"Violations: {violations}",
            "",
            SOLUTION_CHECK_NON_ADJUDICATION_NOTE,
            "",
            solve_text,
        ]
    )


def format_save_result_content(result: SaveVerifiedModelResult) -> str:
    """Return model-visible save output: the outcome and target first, files after.

    Deliberately concise — the verifying ``SolveResult`` (solutions, statistics,
    checker transcript) rides in ``structuredContent``; the text content states
    what happened, where, and which files exist.
    """
    lines = [
        f"Status: {result.status}",
        f"Target directory: {result.target_dir}",
        result.message,
    ]
    if result.files:
        lines.extend(["", "Saved files:"])
        lines.extend(
            f"- {artifact.path} ({artifact.role}, sha256 {artifact.sha256})"
            for artifact in result.files
        )
    lines.extend(["", f"Check status: {result.check.status}"])

    prefix = _diagnostic_prefix(result.diagnostic)
    solve = result.solve
    if solve is None:
        return prefix + "\n".join(lines)

    lines.append(f"Solve status: {solve.status}")
    if solve.checker is not None:
        lines.append(f"Checker status: {solve.checker.status}")
    return prefix + "\n".join(lines)


def format_cpsat_experiment_content(result: CpsatPythonExperimentResult) -> str:
    """Return model-visible experiment output: winner first, then the attempt table.

    Concise — the full per-attempt ``CpsatPythonResult`` winner and metadata ride in
    ``structuredContent``. Marks path-backed attempts and notes when a ``timeout``
    winner is not savable.
    """
    if result.status == "winner":
        assert result.winner is not None  # guaranteed by the result's status invariant
        lines = [
            f"Experiment status: winner ({result.winner_name!r}, "
            f"objective {result.winner.objective}, status {result.winner.status})",
        ]
        if result.winner.status == "timeout":
            lines.append(
                "Note: the winner is a best-so-far incumbent (status=timeout), NOT "
                "savable as-is — save_verified_cpsat_python requires "
                "optimal/feasible. Re-run this attempt with a larger script_timeout_ms "
                "until it reports optimal/feasible, then save."
            )
    else:
        lines = ["Experiment status: no_winner (no attempt was accepted)"]

    lines.extend(
        [
            "",
            (
                f"Objective sense: {result.objective_sense}"
                if result.objective_sense is not None
                else "Mode: feasibility"
            ),
            f"Selection policy: {result.selection_policy}",
            "",
            "Attempts:",
        ]
    )
    for attempt in result.attempts:
        verdict = "accepted" if attempt.accepted else "rejected"
        origin = ", via=script_path" if attempt.used_script_path else ""
        checker = f", checker={attempt.checker_status}" if attempt.checker_status else ""
        reason = f" — {attempt.message}" if attempt.message and not attempt.accepted else ""
        bound = (
            f", best_bound={attempt.best_objective_bound}"
            if attempt.best_objective_bound is not None
            else ""
        )
        lines.append(
            f"- {attempt.name!r} (seed {attempt.seed}{origin}): status={attempt.status}, "
            f"objective={attempt.objective}{bound}, {verdict}{checker}{reason}"
        )
    if result.warnings:
        lines.append("")
        lines.append("Warnings:")
        lines.extend(f"- {w}" for w in result.warnings)
    return _diagnostic_prefix(result.diagnostic) + "\n".join(lines)


def format_solver_list_content(result: SolverList) -> str:
    """Return model-visible solver inventory: a complete id/name/version table.

    Leads with a presentation requirement so a client renders every row instead
    of summarizing or grouping, then appends advisory notes. The full
    ``capabilities`` object is deliberately kept out of this text — it lives in
    ``structuredContent`` and is surfaced only on request — so the default
    presentation stays compact and never dumps raw ``std_flags``.
    """
    return "\n".join(
        [
            SOLVER_INVENTORY_PRESENTATION_REQUIREMENT,
            "",
            "| id | name | version |",
            "| --- | --- | --- |",
            *(
                f"| {solver.id} | {solver.name} | "
                f"{solver.version if solver.version is not None else '<unknown version>'} |"
                for solver in result.solvers
            ),
            "",
            result.capability_note,
            SOLVER_CAPABILITY_METADATA_NOTE,
            "",
            SOLVER_NUM_SOLUTIONS_NOTE,
            "",
            SOLVER_RUNTIME_CONFIG_CAUTION,
        ]
    )


# Caps a joined-name-list line's own size, independent of the row-data bound
# above: a header-only page (or a workbook with many/long sheet names) can
# sit right under MAX_TABULAR_RESPONSE_BYTES on the list alone, and joining
# it here too would otherwise duplicate nearly all of it a second time in
# TextContent.
_MAX_JOINED_NAMES_SUMMARY_CHARS: int = 2000


def _bounded_name_list(names: list[str], *, noun: str) -> str:
    """Join ``names`` if the result stays short, else report only their count."""
    joined = ", ".join(names)
    if len(joined) <= _MAX_JOINED_NAMES_SUMMARY_CHARS:
        return joined
    return (
        f"{len(names)} {noun} (names omitted here, over "
        f"{_MAX_JOINED_NAMES_SUMMARY_CHARS} characters joined; see structuredContent)"
    )


def format_tabular_data_content(result: TabularData) -> str:
    """Return a bounded summary of a tabular page — never the row data itself.

    The low-level MCP SDK's default handling of a plain dict/model return
    would otherwise put the full page in content TWICE — once as indent=2
    JSON text, once as structuredContent — pushing a page already sized up to
    ``tabular.limits.MAX_TABULAR_RESPONSE_BYTES`` well past that ceiling on the
    wire. Row values ride only in structuredContent, same as every other
    large-payload result in this module (e.g. a solve's solutions).
    """
    if not result.headers:
        columns_line = "Columns: (none)"
    else:
        columns_line = f"Columns: {_bounded_name_list(result.headers, noun='columns')}"
    lines = [
        columns_line,
        f"Rows returned: {len(result.rows)} (offset {result.row_offset})",
        f"Total data rows in file: {result.total_rows}",
    ]
    if result.sheet_name is not None:
        lines.append(f"Sheet: {result.sheet_name}")
    if result.available_sheets:
        sheets = _bounded_name_list(result.available_sheets, noun="sheets")
        lines.append(f"Available sheets: {sheets}")
    if result.truncated:
        lines.append(
            f"Truncated: yes ({result.truncation_reason}); next_row_offset={result.next_row_offset}"
        )
    else:
        lines.append("Truncated: no (this is the final page)")
    return "\n".join(lines)
