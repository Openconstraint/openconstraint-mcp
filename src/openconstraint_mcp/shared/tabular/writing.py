"""The plain table emitters: one CSV writer and one XLSX writer."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ...schemas.tabular import TabularCell
from .limits import WRITE_SHEET_NAME


def _write_csv(path: Path, headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Write the fixed comma/quote dialect. ``None`` becomes an empty field."""
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def build_xlsx_workbook(headers: list[str], rows: list[list[TabularCell]]) -> Any:
    """Build the one-sheet workbook, storing every string as an explicit string cell.

    openpyxl's value setter infers a leading ``=`` as a FORMULA (``data_type``
    ``"f"``). Forcing ``data_type = "s"`` after assignment writes the text as
    an inline string instead, so ``"=1+1"`` round-trips as the literal text it
    was — the server never emits executable spreadsheet code.

    Returned unsaved so a caller that also styles the table or adds diagram
    sheets mutates this workbook and saves it once, instead of saving twice.
    """
    from openpyxl import Workbook

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = WRITE_SHEET_NAME
    records: list[list[TabularCell]] = [[*headers], *rows]
    for row_index, values in enumerate(records, start=1):
        for column_index, value in enumerate(values, start=1):
            cell = worksheet.cell(row=row_index, column=column_index)
            cell.value = value
            if isinstance(value, str):
                cell.data_type = "s"
    return workbook


def _write_xlsx(path: Path, headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Write the plain one-sheet workbook — no styling, no diagram sheets."""
    build_xlsx_workbook(headers, rows).save(path)
