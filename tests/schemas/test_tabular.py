from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from openconstraint_mcp.schemas.tabular import (
    ChartSpec,
    ColumnStyle,
    GanttSpec,
    TableStyle,
    TabularData,
    TabularWriteResult,
)


def _page(**overrides: Any) -> TabularData:
    """Build a valid EOF page, overriding only the fields under test."""
    fields: dict[str, Any] = {
        "headers": ["a"],
        "rows": [["x"]],
        "sheet_name": None,
        "available_sheets": [],
        "row_offset": 0,
        "next_row_offset": None,
        "total_rows": 1,
        "truncated": False,
        "truncation_reason": None,
    }
    fields.update(overrides)
    return TabularData(**fields)


# --- TabularCell: accepted scalars ---------------------------------------------


@pytest.mark.parametrize("cell", ["text", 42, 3.5, True, None])
def test_row_cell_accepts_every_json_scalar(cell: object) -> None:
    assert _page(rows=[[cell]]).rows == [[cell]]


def test_row_cell_preserves_bool_rather_than_coercing_it_to_int() -> None:
    # bool subclasses int, so a lax union would retype True as 1.
    assert _page(rows=[[True]]).rows[0][0] is True


def test_row_cell_does_not_coerce_a_numeric_string_to_a_number() -> None:
    assert _page(rows=[["5"]]).rows[0][0] == "5"


# --- TabularCell: rejected values ----------------------------------------------


@pytest.mark.parametrize("cell", [["nested"], {"key": "value"}])
def test_row_cell_rejects_a_nested_container(cell: object) -> None:
    with pytest.raises(ValidationError):
        _page(rows=[[cell]])


@pytest.mark.parametrize("cell", [float("inf"), float("-inf"), float("nan")])
def test_row_cell_rejects_a_non_finite_float(cell: float) -> None:
    with pytest.raises(ValidationError):
        _page(rows=[[cell]])


def test_headers_reject_a_non_string() -> None:
    with pytest.raises(ValidationError):
        _page(headers=[7])


# --- Pagination invariants ------------------------------------------------------


def test_truncated_page_carries_next_offset_and_reason() -> None:
    page = _page(next_row_offset=1, total_rows=2, truncated=True, truncation_reason="max_rows")
    assert (page.next_row_offset, page.truncation_reason) == (1, "max_rows")


def test_truncated_page_without_a_next_offset_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=None, truncated=True, truncation_reason="max_rows")


def test_truncated_page_without_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=True, truncation_reason=None)


def test_untruncated_page_with_a_next_offset_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=False, truncation_reason=None)


def test_untruncated_page_with_a_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=None, truncated=False, truncation_reason="max_bytes")


def test_unknown_truncation_reason_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _page(next_row_offset=1, truncated=True, truncation_reason="too_big")


# --- TabularWriteResult ---------------------------------------------------------


def test_write_result_status_defaults_to_written() -> None:
    result = TabularWriteResult(
        message="wrote 1 row",
        target_path="/tmp/out.csv",
        sha256="ab" * 32,
        format="csv",
        rows_written=1,
    )
    assert result.status == "written"


def test_write_result_rejects_an_unknown_format() -> None:
    with pytest.raises(ValidationError):
        TabularWriteResult(
            message="wrote 1 row",
            target_path="/tmp/out.ods",
            sha256="ab" * 32,
            format="ods",  # type: ignore[arg-type]
            rows_written=1,
        )


def _write_result() -> TabularWriteResult:
    """A result built the way a pre-diagram caller builds it: no new fields."""
    return TabularWriteResult(
        message="wrote 1 row",
        target_path="/tmp/out.xlsx",
        sha256="ab" * 32,
        format="xlsx",
        rows_written=1,
    )


def test_write_result_defaults_to_the_data_sheet_alone() -> None:
    assert _write_result().sheets_written == ["Sheet1"]


def test_write_result_defaults_to_no_diagrams() -> None:
    assert _write_result().diagrams_written == []


# --- ColumnStyle / TableStyle ---------------------------------------------------


def test_column_style_defaults_to_no_format_and_no_width() -> None:
    style = ColumnStyle()
    assert (style.number_format, style.width) == (None, None)


@pytest.mark.parametrize("width", [0, 256])
def test_column_style_rejects_a_width_outside_the_excel_range(width: int) -> None:
    with pytest.raises(ValidationError):
        ColumnStyle(width=width)


def test_table_style_defaults_to_an_empty_column_map() -> None:
    assert TableStyle().columns == {}


def test_table_style_rejects_an_unknown_preset() -> None:
    with pytest.raises(ValidationError):
        TableStyle(preset="fancy")  # type: ignore[arg-type]


# --- GanttSpec ------------------------------------------------------------------


def _gantt(**overrides: Any) -> GanttSpec:
    fields: dict[str, Any] = {
        "task_column": "task",
        "start_column": "start",
        "duration_column": "duration",
    }
    fields.update(overrides)
    return GanttSpec(**fields)


def test_gantt_spec_rejects_both_an_end_and_a_duration_column() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _gantt(end_column="end")


def test_gantt_spec_rejects_neither_an_end_nor_a_duration_column() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        _gantt(duration_column=None)


def test_gantt_spec_defaults_its_sheet_name() -> None:
    assert _gantt().sheet_name == "Gantt"


def test_gantt_spec_rejects_an_empty_sheet_name() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        _gantt(sheet_name="")


def test_gantt_spec_rejects_the_reserved_data_sheet_name() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        _gantt(sheet_name="Sheet1")


def test_gantt_spec_rejects_the_reserved_name_in_another_case() -> None:
    # openpyxl matches sheet titles case-insensitively when it auto-dedupes,
    # so "sheet1" collides with the data sheet just as much as "Sheet1".
    with pytest.raises(ValidationError, match="reserved"):
        _gantt(sheet_name="sheet1")


def test_gantt_spec_rejects_a_sheet_name_past_excels_length_limit() -> None:
    with pytest.raises(ValidationError, match="31"):
        _gantt(sheet_name="T" * 32)


@pytest.mark.parametrize("name", ["a[b", "a]b", "a:b", "a*b", "a?b", "a/b", "a\\b"])
def test_gantt_spec_rejects_a_sheet_name_excel_forbids(name: str) -> None:
    with pytest.raises(ValidationError, match="cannot contain"):
        _gantt(sheet_name=name)


# --- ChartSpec ------------------------------------------------------------------


def test_chart_spec_rejects_an_empty_y_column_list() -> None:
    with pytest.raises(ValidationError):
        ChartSpec(kind="bar", x_column="task", y_columns=[])


def test_chart_spec_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        ChartSpec(kind="pie", x_column="task", y_columns=["qty"])  # type: ignore[arg-type]


def test_chart_spec_defaults_its_sheet_name() -> None:
    assert ChartSpec(kind="bar", x_column="task", y_columns=["qty"]).sheet_name == "Charts"


def test_chart_spec_rejects_the_reserved_data_sheet_name() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        ChartSpec(kind="bar", x_column="task", y_columns=["qty"], sheet_name="Sheet1")
