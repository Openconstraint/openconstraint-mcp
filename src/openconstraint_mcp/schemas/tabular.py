"""Public models for the tabular (``.xlsx``/``.csv``) I/O tools.

The server does mechanical I/O only: it never infers what a column *means*.
A cell is therefore a JSON scalar and nothing more — see ``TabularCell``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

# One spreadsheet cell. Strict members on purpose:
#
# * ``bool`` subclasses ``int``, so a lax union would coerce ``True`` to ``1``
#   and silently retype the cell. Strict members match only their exact type.
# * ``allow_inf_nan=False`` rejects ``inf``/``nan``, which have no JSON form —
#   without it they would fail at *serialization*, i.e. after a write already
#   touched the disk. Rejecting them here keeps the failure pre-I/O.
#
# Lists and objects match no member and are rejected the same way.
type TabularCell = (
    StrictStr | StrictBool | StrictInt | Annotated[StrictFloat, Field(allow_inf_nan=False)] | None
)

TabularFormat = Literal["xlsx", "csv"]
TabularTruncationReason = Literal["max_rows", "max_bytes"]

# The data sheet an XLSX write always produces. Owned here because both the
# writer (which names the sheet) and the diagram specs below (which must refuse
# to collide with it) need the same literal.
RESERVED_SHEET_NAME: str = "Sheet1"

# Excel's own sheet-title rules: at most 31 characters, and none of these.
_MAX_SHEET_NAME_LENGTH: int = 31
_FORBIDDEN_SHEET_NAME_CHARACTERS: str = "[]:*?/\\"


def _check_diagram_sheet_name(value: str) -> str:
    """Reject a diagram sheet name openpyxl would refuse or silently rename.

    The reserved name is compared case-insensitively: openpyxl dedupes sheet
    titles that way, so a ``"sheet1"`` beside the data sheet does not raise —
    it silently becomes ``"sheet11"``. Rejecting here keeps the caller's
    requested name and the written name the same string.
    """
    if not value:
        raise ValueError("sheet_name must not be empty")
    if value.casefold() == RESERVED_SHEET_NAME.casefold():
        raise ValueError(
            f"sheet_name {value!r} is reserved for the data sheet (matched "
            f"case-insensitively, as openpyxl matches it); pick another name"
        )
    if len(value) > _MAX_SHEET_NAME_LENGTH:
        raise ValueError(
            f"sheet_name {value!r} is {len(value)} characters long, over Excel's "
            f"{_MAX_SHEET_NAME_LENGTH}-character sheet-title limit"
        )
    forbidden = [character for character in _FORBIDDEN_SHEET_NAME_CHARACTERS if character in value]
    if forbidden:
        raise ValueError(
            f"sheet_name {value!r} cannot contain any of "
            f"{_FORBIDDEN_SHEET_NAME_CHARACTERS!r}, which Excel forbids in a sheet title"
        )
    return value


# A sheet name a diagram may claim: anything Excel accepts, except the data
# sheet's own reserved name.
DiagramSheetName = Annotated[str, AfterValidator(_check_diagram_sheet_name)]


class TabularData(BaseModel):
    """One bounded page of rows read from a spreadsheet or CSV file.

    ``headers`` are always strings (see ``shared.tabular.reading`` for the
    normalization rules) and are repeated on every page, so a page is
    self-describing. ``row_offset`` is a zero-based offset among *data* rows —
    the header row, when present, is not a data row.

    Pagination invariant: ``truncated`` is true exactly when rows remain, and
    in that case both ``next_row_offset`` (the offset to request next) and
    ``truncation_reason`` (which bound stopped this page) are set. At EOF all
    three are ``None``/``False``.
    """

    headers: list[str]
    rows: list[list[TabularCell]]
    sheet_name: str | None
    available_sheets: list[str]
    row_offset: int
    next_row_offset: int | None
    total_rows: int
    truncated: bool
    truncation_reason: TabularTruncationReason | None

    @model_validator(mode="after")
    def _check_pagination(self) -> TabularData:
        if self.truncated and (self.next_row_offset is None or self.truncation_reason is None):
            raise ValueError(
                "a truncated page must carry both next_row_offset and truncation_reason"
            )
        if not self.truncated and (
            self.next_row_offset is not None or self.truncation_reason is not None
        ):
            raise ValueError(
                "an untruncated page must carry neither next_row_offset nor truncation_reason"
            )
        return self


class ColumnStyle(BaseModel):
    """Display-only overrides for one column: an openpyxl format code, a width."""

    number_format: str | None = None
    width: int | None = Field(default=None, ge=1, le=255)


class TableStyle(BaseModel):
    """The fixed polished-table preset, with per-column overrides keyed by header."""

    preset: Literal["polished"] = "polished"
    columns: dict[str, ColumnStyle] = Field(default_factory=dict)


class GanttSpec(BaseModel):
    """A cell-grid timeline sheet: columns named by header, times discrete integers."""

    task_column: str
    start_column: str
    end_column: str | None = None
    duration_column: str | None = None
    row_column: str | None = None
    lane_column: str | None = None
    sheet_name: DiagramSheetName = "Gantt"
    title: str | None = None

    @model_validator(mode="after")
    def _check_span_column(self) -> GanttSpec:
        if (self.end_column is None) == (self.duration_column is None):
            raise ValueError(
                "a Gantt needs exactly one of end_column or duration_column to size a task"
            )
        return self


class ChartSpec(BaseModel):
    """One chart of the data sheet's own columns; specs sharing a sheet stack on it."""

    kind: Literal["bar", "line", "scatter"]
    x_column: str
    y_columns: list[str] = Field(min_length=1)
    title: str | None = None
    sheet_name: DiagramSheetName = "Charts"


class TabularWriteResult(BaseModel):
    """The outcome of a successful tabular write.

    Only produced on success — every refusal (bad path, non-scalar cell,
    ragged row, refused to overwrite) raises instead. ``sha256`` is the digest
    of the staged file's bytes, computed before the commit publishes them —
    identical to the committed file's bytes, since the commit is a rename/link
    of that same staged file.

    ``sheets_written`` lists every sheet in the written workbook, data sheet
    first — empty for a CSV, which has no sheets at all. ``diagrams_written``
    names the diagrams rendered, in render order, as ``"gantt"`` and
    ``"chart:<kind>"`` tokens (duplicates preserved, so its length is the
    number of diagrams). Styling is not a diagram and adds no token.
    """

    status: Literal["written"] = "written"
    message: str
    target_path: str
    sha256: str
    format: TabularFormat
    rows_written: int
    sheets_written: list[str] = Field(default_factory=lambda: [RESERVED_SHEET_NAME])
    diagrams_written: list[str] = Field(default_factory=list)
