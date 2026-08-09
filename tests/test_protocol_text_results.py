"""The model-visible result text leads with a `Diagnostic:` line when the result
carries a diagnostic, and adds nothing on a clean success."""

from __future__ import annotations

from openconstraint_mcp.protocol_text.results import (
    format_cpsat_experiment_content,
    format_solve_result_content,
    format_tabular_data_content,
)
from openconstraint_mcp.schemas.cpsat import (
    CpsatPythonExperimentAttemptResult,
    CpsatPythonExperimentResult,
    CpsatPythonResult,
)
from openconstraint_mcp.schemas.diagnostics import Diagnostic
from openconstraint_mcp.schemas.minizinc import CheckerReport, SolveResult
from openconstraint_mcp.schemas.tabular import TabularData


def _solve(status: str, diagnostic: Diagnostic | None) -> SolveResult:
    return SolveResult(
        status=status,  # type: ignore[arg-type]
        solver="cp-sat",
        return_code=0,
        timed_out=False,
        stdout="",
        stderr="",
        elapsed_ms=5,
        diagnostic=diagnostic,
    )


def test_solve_text_leads_with_diagnostic_when_present() -> None:
    text = format_solve_result_content(
        _solve("unsatisfiable", Diagnostic(category="infeasible", message="no solution exists"))
    )
    assert text.startswith("Diagnostic: infeasible — no solution exists\n\n")


def test_solve_text_clean_success_has_no_diagnostic_line() -> None:
    text = format_solve_result_content(_solve("satisfied", None))
    assert not text.startswith("Diagnostic:")
    assert text.startswith("Status: satisfied")


def test_checked_solve_text_carries_the_whole_non_adjudication_rule() -> None:
    # These two clauses used to be stated a second time in the solve tool
    # descriptions. They are not, so this in-band note is now their ONLY
    # carrier: a client that reads a `CORRECT` line as a server verdict, or an
    # unflagged checker as a proof of optimality, draws the wrong conclusion.
    result = _solve("satisfied", None)
    result.checker = CheckerReport(status="completed", checks=[], transcript="")

    text = format_solve_result_content(result)

    assert "NOT interpreted by the server" in text
    assert "only a nested UNSATISFIABLE" in text
    assert "Only `solve.status` proves" in text
    assert "completeness/optimality" in text


def test_experiment_text_leads_with_no_winner_diagnostic() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[],
        elapsed_ms=5,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
        diagnostic=Diagnostic(category="no_winner", message="no attempt was accepted"),
    )
    text = format_cpsat_experiment_content(result)
    assert text.startswith("Diagnostic: no_winner — no attempt was accepted\n\n")


def test_experiment_timeout_winner_is_best_so_far_and_not_savable_until_rerun() -> None:
    # A timeout winner is only an incumbent: the text must flag it as best-so-far,
    # not savable, and direct a rerun *until* the status gate is met — "rerun once"
    # is not enough, a rerun that times out again is still not savable.
    winner = CpsatPythonResult(
        status="timeout",
        solution={"x": 4},
        objective=4,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=True,
        truncated=False,
        duration_ms=1000,
    )
    result = CpsatPythonExperimentResult(
        status="winner",
        winner_index=0,
        winner_name="baseline",
        winner=winner,
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="baseline",
                seed=None,
                config_sha256=None,
                source_sha256="abc123",
                script_timeout_ms=1000,
                status="timeout",
                objective=4,
                accepted=True,
                timed_out=True,
                truncated=False,
                duration_ms=1000,
            )
        ],
        elapsed_ms=1000,
        objective_sense="maximize",
        selection_policy=(
            "best_accepted_incumbent_objective_then_status_then_duration_then_attempt_order"
        ),
        source_sha256=["abc123"],
    )

    text = format_cpsat_experiment_content(result)

    assert "best-so-far" in text
    assert "NOT savable" in text
    assert "until it reports optimal/feasible" in text


def test_experiment_text_marks_script_path_attempts() -> None:
    result = CpsatPythonExperimentResult(
        status="no_winner",
        attempts=[
            CpsatPythonExperimentAttemptResult(
                index=0,
                name="from-file",
                source_sha256="abc123",
                used_script_path=True,
                script_timeout_ms=1000,
                status="infeasible",
                objective=None,
                accepted=False,
                timed_out=False,
                truncated=False,
                duration_ms=5,
            )
        ],
        elapsed_ms=5,
        objective_sense=None,
        selection_policy="accepted_status_then_duration_then_attempt_order",
        source_sha256=["abc123"],
    )

    text = format_cpsat_experiment_content(result)

    assert "'from-file' (seed None, via=script_path)" in text


def _tabular(headers: list[str], available_sheets: list[str] | None = None) -> TabularData:
    return TabularData(
        headers=headers,
        rows=[],
        sheet_name=None,
        available_sheets=available_sheets or [],
        row_offset=0,
        next_row_offset=None,
        total_rows=0,
        truncated=False,
        truncation_reason=None,
    )


def test_tabular_columns_summary_lists_names_when_short() -> None:
    text = format_tabular_data_content(_tabular(["name", "age"]))
    assert "Columns: name, age" in text


def test_tabular_columns_summary_is_bounded_for_many_long_headers() -> None:
    # A header-only page can sit right under the structuredContent byte
    # ceiling on many/long header names alone; joining every name here too
    # would duplicate nearly all of it a second time in TextContent.
    headers = [f"very_long_column_header_name_{i}" * 5 for i in range(200)]
    text = format_tabular_data_content(_tabular(headers))
    assert "Columns: 200 columns" in text
    assert headers[0] not in text


def test_tabular_sheets_summary_lists_names_when_short() -> None:
    text = format_tabular_data_content(_tabular(["a"], available_sheets=["Sheet1", "Sheet2"]))
    assert "Available sheets: Sheet1, Sheet2" in text


def test_tabular_sheets_summary_is_bounded_for_many_sheets() -> None:
    # Same duplication risk as the columns line, for a workbook with many
    # (valid, 31-character) sheet names.
    sheets = [f"very_long_sheet_name_number_{i:05d}"[:31] for i in range(20000)]
    text = format_tabular_data_content(_tabular(["a"], available_sheets=sheets))
    assert "Available sheets: 20000 sheets" in text
    assert sheets[0] not in text
