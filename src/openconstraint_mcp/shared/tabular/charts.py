"""Native bar/line/scatter charts plotted from the data sheet's own columns.

Split like ``style`` and ``gantt``: ``resolve_charts`` validates without
touching a workbook, so ``core`` can refuse a bad spec before staging a file;
``render_charts`` only draws what was resolved.

Charts reference the data sheet directly — no hidden helper range, no synthetic
series — so a chart sheet holds drawings and no rows of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...schemas.tabular import ChartSpec, TabularCell
from .columns import column_index
from .guards import _check_no_carriage_return, _check_no_illegal_xml_characters
from .limits import WRITE_SHEET_NAME

# Vertical spacing between two charts anchored on the same sheet, in rows.
_CHART_ROW_SPAN: int = 16


@dataclass(frozen=True)
class ResolvedChart:
    """One chart's plot, with every column already resolved to an index."""

    kind: str
    sheet_name: str
    title: str | None
    x_index: int
    y_indexes: tuple[int, ...]
    row_count: int


def _require_numeric_column(
    rows: list[list[TabularCell]], index: int, *, column_name: str
) -> None:
    """Reject a column holding anything a chart cannot plot as a number."""
    for row_index, row in enumerate(rows):
        cell = row[index]
        if isinstance(cell, bool) or not isinstance(cell, int | float):
            raise ValueError(
                f"column {column_name!r} at row {row_index} is {cell!r}; a charted column "
                f"must hold numbers only"
            )


def resolve_charts(
    headers: list[str], rows: list[list[TabularCell]], specs: list[ChartSpec]
) -> list[ResolvedChart]:
    """Resolve every spec against the table, or raise ``ValueError``.

    Two specs may share a ``sheet_name`` — their charts then stack on that one
    sheet. Two names differing only by case are refused instead: openpyxl
    dedupes sheet titles case-insensitively, so one of them would silently be
    renamed.

    ``sheet_name`` and ``title`` are user-controlled strings bound for the same
    XML writer as a cell, so both go through the same character guards the data
    sheet's own values do — the schema's sheet-name rules cover Excel's
    structural restrictions, not XML's.
    """
    if specs and not rows:
        raise ValueError(
            "a chart plots the data sheet's own rows, and this table has none; a chart "
            "over zero rows would reference an empty range. Write the table without "
            "charts, or send at least one data row"
        )

    by_folded: dict[str, str] = {}
    for spec in specs:
        _check_no_illegal_xml_characters(spec.sheet_name, f"chart sheet_name {spec.sheet_name!r}")
        _check_no_carriage_return(spec.sheet_name, f"chart sheet_name {spec.sheet_name!r}")
        if spec.title is not None:
            _check_no_illegal_xml_characters(spec.title, "a chart title")
            _check_no_carriage_return(spec.title, "a chart title")
        folded = spec.sheet_name.casefold()
        claimed = by_folded.setdefault(folded, spec.sheet_name)
        if claimed != spec.sheet_name:
            raise ValueError(
                f"chart sheet names {claimed!r} and {spec.sheet_name!r} are ambiguous: they "
                f"differ only by case, and openpyxl matches sheet titles case-insensitively; "
                f"use one spelling to share a sheet, or two clearly different names"
            )

    resolved: list[ResolvedChart] = []
    for spec in specs:
        x_index = column_index(headers, spec.x_column, "x_column")
        if spec.kind == "scatter":
            # ScatterChart plots x on a numeric axis; bar/line treat x as
            # plain category labels, so only scatter constrains it.
            _require_numeric_column(rows, x_index, column_name=spec.x_column)
        y_indexes: list[int] = []
        for name in spec.y_columns:
            y_index = column_index(headers, name, "y_column")
            _require_numeric_column(rows, y_index, column_name=name)
            y_indexes.append(y_index)
        resolved.append(
            ResolvedChart(
                kind=spec.kind,
                sheet_name=spec.sheet_name,
                title=spec.title,
                x_index=x_index,
                y_indexes=tuple(y_indexes),
                row_count=len(rows),
            )
        )
    return resolved


def _build_chart(data_sheet: Any, resolved: ResolvedChart) -> Any:
    """Build one chart object referencing ``data_sheet``'s columns."""
    from openpyxl.chart import BarChart, LineChart, Reference, ScatterChart, Series

    last_row = resolved.row_count + 1  # row 1 is the header row.
    if resolved.kind == "scatter":
        chart: Any = ScatterChart()
        x_values = Reference(
            data_sheet, min_col=resolved.x_index + 1, min_row=2, max_row=last_row
        )
        for y_index in resolved.y_indexes:
            values = Reference(data_sheet, min_col=y_index + 1, min_row=1, max_row=last_row)
            chart.series.append(Series(values, x_values, title_from_data=True))
    else:
        chart = BarChart() if resolved.kind == "bar" else LineChart()
        for y_index in resolved.y_indexes:
            values = Reference(data_sheet, min_col=y_index + 1, min_row=1, max_row=last_row)
            chart.add_data(values, titles_from_data=True)
        chart.set_categories(
            Reference(data_sheet, min_col=resolved.x_index + 1, min_row=2, max_row=last_row)
        )
    if resolved.title is not None:
        chart.title = resolved.title
    return chart


def render_charts(workbook: Any, resolved: list[ResolvedChart]) -> list[str]:
    """Add one sheet per distinct ``sheet_name`` and return those names in order."""
    data_sheet = workbook[WRITE_SHEET_NAME]
    grouped: dict[str, list[ResolvedChart]] = {}
    for chart in resolved:
        grouped.setdefault(chart.sheet_name, []).append(chart)

    for sheet_name, charts in grouped.items():
        worksheet = workbook.create_sheet(sheet_name)
        for position, chart in enumerate(charts):
            worksheet.add_chart(
                _build_chart(data_sheet, chart), f"A{position * _CHART_ROW_SPAN + 1}"
            )
    return list(grouped)
