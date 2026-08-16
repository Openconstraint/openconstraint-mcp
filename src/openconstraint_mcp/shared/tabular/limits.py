"""Fixed caps shared by more than one tabular leaf.

Every value here is a hard constant, not a knob: nothing in this package makes
one of them configurable.
"""

from __future__ import annotations

from ...schemas.tabular import RESERVED_SHEET_NAME as WRITE_SHEET_NAME

# The hard ceiling on a serialized TabularData body, independent of max_rows.
# Only whole rows are ever returned, so a page is trimmed to fit rather than
# a cell being cut in half.
MAX_TABULAR_RESPONSE_BYTES: int = 1_048_576

DEFAULT_MAX_ROWS: int = 1000

# Excel's hard per-cell string limit. openpyxl silently TRUNCATES a longer
# string (see Cell.check_string), which would be a silent data change — so we
# reject the string up front instead of letting it through.
XLSX_MAX_STRING_LENGTH: int = 32_767

# The widest Gantt grid that stays a readable sheet: one column per discrete
# time unit, so a schedule ending at 100_000 would otherwise emit that many
# columns.
GANTT_MAX_HORIZON_COLUMNS: int = 512

# The band a text-fitted column width stays inside, in Excel character units.
# The floor keeps a one-character column clickable; the ceiling stops one long
# value from pushing every other column off screen. Shared by the data sheet's
# per-column fitting and the Gantt sheet's label column, which fit different
# text but must land on the same readable range.
MIN_COLUMN_WIDTH: int = 8
MAX_COLUMN_WIDTH: int = 60

__all__ = [
    "DEFAULT_MAX_ROWS",
    "GANTT_MAX_HORIZON_COLUMNS",
    "MAX_COLUMN_WIDTH",
    "MAX_TABULAR_RESPONSE_BYTES",
    "MIN_COLUMN_WIDTH",
    "WRITE_SHEET_NAME",
    "XLSX_MAX_STRING_LENGTH",
]
