"""The cell-grid Gantt sheet: resolve a ``GanttSpec``, then render a timeline.

Split like ``style``: ``resolve_gantt`` validates and computes without touching
a workbook, so ``core`` can refuse a bad spec before staging a file, and
``render_gantt`` only draws what was resolved.

Times are discrete non-negative integers — the native shape of CP-SAT and
MiniZinc scheduling output. A float, a numeric string, or a null is refused
rather than coerced, matching this package's refuse-don't-coerce posture in
``guards``.

Lane colours are the ``dataviz`` skill's reference categorical palette, in its
documented slot order. That order is the colour-blindness safety mechanism, so
it is never reordered. The CVD ΔE check run during design compared arbitrary
pairs across the first three slots only; beyond three lanes the separation rests
on the palette's own published guarantees, not on anything measured here. Lanes
cycle through the eight slots because a lane set comes from the caller's data
and cannot be folded into an "Other" bucket the way a chart series can; the
legend prints every lane's name beside its swatch, so identity never rests on
colour alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...schemas.tabular import GanttSpec, TabularCell
from .columns import column_index
from .guards import (
    _check_no_carriage_return,
    _check_no_illegal_xml_characters,
    _check_not_oversized_xlsx_string,
)
from .limits import GANTT_MAX_HORIZON_COLUMNS

# The dataviz reference palette's categorical slots, light mode, in order.
_LANE_FILL_RGB: tuple[str, ...] = (
    "FF2A78D6",
    "FFEB6834",
    "FF1BAF7A",
    "FFEDA100",
    "FFE87BA4",
    "FF008300",
    "FF4A3AA7",
    "FFE34948",
)

# Narrow enough that a long horizon still reads as a timeline.
_TIME_COLUMN_WIDTH: int = 3

_TASK_COLUMN_HEADER: str = "Task"

# A null label or lane renders as an explicit placeholder rather than "": an
# empty string writes a degenerate cell openpyxl reads back as null (the same
# reason ``guards._reject_xlsx_empty_strings`` refuses one), and a blank-named
# legend swatch would put lane identity back on colour alone.
_MISSING_TASK_LABEL: str = "(untitled task)"
_MISSING_LANE_NAME: str = "(no lane)"


@dataclass(frozen=True)
class ResolvedTask:
    """One task's rendered row: its label, its half-open span, and its lane."""

    label: str
    start: int
    end: int
    lane: str | None


@dataclass(frozen=True)
class ResolvedGantt:
    """Everything ``render_gantt`` needs, with nothing left to validate."""

    sheet_name: str
    title: str | None
    tasks: tuple[ResolvedTask, ...]
    lanes: tuple[str | None, ...]
    horizon: int


def _require_time(value: TabularCell, *, row_index: int, role: str) -> int:
    """Return ``value`` as a discrete time, or raise ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"the {role} at row {row_index} is {value!r}; a Gantt needs a discrete "
            f"integer time, and values are never coerced"
        )
    return int(value)


def _rendered_text(value: TabularCell, *, placeholder: str) -> str:
    """Render one cell as the label text a Gantt sheet shows."""
    return placeholder if value is None else str(value)


def resolve_gantt(
    headers: list[str], rows: list[list[TabularCell]], spec: GanttSpec
) -> ResolvedGantt:
    """Resolve ``spec`` against the table, or raise ``ValueError``.

    ``spec.title`` is free text, not a column reference, so it is not checked
    against ``headers``. It and ``spec.sheet_name`` are still user-controlled
    strings bound for the same XML writer as a cell, so both go through the
    same character guards the data sheet's own values do — the schema's
    sheet-name rules cover Excel's structural restrictions, not XML's. The
    title additionally lands in a real cell (``A1``), so it also gets the
    per-cell length and empty-string guards a data cell gets; a sheet name is
    not a cell and the schema already bounds it at 31 characters.
    """
    _check_no_illegal_xml_characters(spec.sheet_name, f"gantt sheet_name {spec.sheet_name!r}")
    _check_no_carriage_return(spec.sheet_name, f"gantt sheet_name {spec.sheet_name!r}")
    if spec.title is not None:
        if spec.title == "":
            raise ValueError(
                "the gantt title is an empty string, which renders as a blank first row "
                "while still shifting the grid down; omit title entirely instead of "
                "sending an empty string"
            )
        _check_no_illegal_xml_characters(spec.title, "the gantt title")
        _check_no_carriage_return(spec.title, "the gantt title")
        _check_not_oversized_xlsx_string(spec.title, "the gantt title")

    task_index = column_index(headers, spec.task_column, "task_column")
    start_index = column_index(headers, spec.start_column, "start_column")
    end_index = (
        None if spec.end_column is None else column_index(headers, spec.end_column, "end_column")
    )
    duration_index = (
        None
        if spec.duration_column is None
        else column_index(headers, spec.duration_column, "duration_column")
    )
    lane_index = (
        None if spec.lane_column is None else column_index(headers, spec.lane_column, "lane_column")
    )

    tasks: list[ResolvedTask] = []
    lanes: list[str | None] = []
    for row_index, row in enumerate(rows):
        start = _require_time(row[start_index], row_index=row_index, role="start")
        if start < 0:
            raise ValueError(
                f"the start at row {row_index} is {start}; a Gantt time must be >= 0"
            )
        if duration_index is not None:
            duration = _require_time(row[duration_index], row_index=row_index, role="duration")
            if duration < 1:
                raise ValueError(
                    f"the duration at row {row_index} is {duration}; a Gantt task must "
                    f"last at least one time unit"
                )
            end = start + duration
        else:
            assert end_index is not None  # GanttSpec requires exactly one of the two.
            end = _require_time(row[end_index], row_index=row_index, role="end")
            if end <= start:
                raise ValueError(
                    f"the end at row {row_index} is {end}, which is not after its start "
                    f"{start}; a Gantt task must last at least one time unit"
                )
        lane: str | None = (
            None
            if lane_index is None or row[lane_index] is None
            else str(row[lane_index])
        )
        if lane_index is not None and lane not in lanes:
            lanes.append(lane)
        label = _rendered_text(row[task_index], placeholder=_MISSING_TASK_LABEL)
        tasks.append(ResolvedTask(label=label, start=start, end=end, lane=lane))

    horizon = max((task.end for task in tasks), default=0)
    if horizon > GANTT_MAX_HORIZON_COLUMNS:
        raise ValueError(
            f"this schedule needs a horizon of {horizon} time columns, over the "
            f"{GANTT_MAX_HORIZON_COLUMNS}-column Gantt limit; scale the times down or "
            f"write fewer tasks"
        )
    return ResolvedGantt(
        sheet_name=spec.sheet_name,
        title=spec.title,
        tasks=tuple(tasks),
        lanes=tuple(lanes),
        horizon=horizon,
    )


def _write_text(worksheet: Any, *, row: int, column: int, text: str) -> None:
    """Write literal text, never a formula.

    openpyxl infers a leading ``=`` as a formula, so every user-derived string
    this module writes forces ``data_type = "s"`` — the same rule the plain
    XLSX writer applies to the data sheet.
    """
    cell = worksheet.cell(row=row, column=column)
    cell.value = text
    cell.data_type = "s"


def render_gantt(workbook: Any, resolved: ResolvedGantt) -> str:
    """Add the Gantt sheet to ``workbook`` and return its name.

    No cell is ever merged: the read path exposes only a merge's top-left
    value, so a merge would silently lose data.
    """
    from openpyxl.styles import PatternFill
    from openpyxl.utils import get_column_letter

    def solid(rgb: str) -> Any:
        return PatternFill(start_color=rgb, end_color=rgb, fill_type="solid")

    # Keyed by lane, or by None for the single default fill of a laneless Gantt.
    fills: dict[str | None, Any] = {
        lane: solid(_LANE_FILL_RGB[index % len(_LANE_FILL_RGB)])
        for index, lane in enumerate(resolved.lanes)
    }
    fills.setdefault(None, solid(_LANE_FILL_RGB[0]))

    worksheet = workbook.create_sheet(resolved.sheet_name)
    header_row = 1
    if resolved.title is not None:
        _write_text(worksheet, row=1, column=1, text=resolved.title)
        header_row = 2

    _write_text(worksheet, row=header_row, column=1, text=_TASK_COLUMN_HEADER)
    for offset in range(resolved.horizon):
        column = offset + 2
        worksheet.cell(row=header_row, column=column).value = offset
        worksheet.column_dimensions[get_column_letter(column)].width = _TIME_COLUMN_WIDTH

    for task_index, task in enumerate(resolved.tasks):
        row = header_row + 1 + task_index
        _write_text(worksheet, row=row, column=1, text=task.label)
        for offset in range(task.start, task.end):
            worksheet.cell(row=row, column=offset + 2).fill = fills[task.lane]

    # The legend is what keeps lane identity off colour alone.
    legend_row = header_row + len(resolved.tasks) + 2
    for lane_index, lane in enumerate(resolved.lanes):
        _write_text(
            worksheet,
            row=legend_row + lane_index,
            column=1,
            text=_rendered_text(lane, placeholder=_MISSING_LANE_NAME),
        )
        worksheet.cell(row=legend_row + lane_index, column=2).fill = fills[lane]
    return str(worksheet.title)
