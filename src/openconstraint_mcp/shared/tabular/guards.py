"""Every write guard: cell-shape validation plus the per-format rejections."""

from __future__ import annotations

import math
import re
from collections.abc import Callable

from ...schemas.tabular import TabularCell
from .limits import XLSX_MAX_STRING_LENGTH

# A leading one of these makes a spreadsheet treat text as a formula. XLSX can
# store the text as an explicit string cell, so it is written verbatim there.
# CSV has no way to encode "this is literal text", so such strings are refused.
CSV_FORMULA_PREFIXES: tuple[str, ...] = ("=", "+", "-", "@")


def _validate_cells(headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Reject non-scalar cells and rows whose width does not match the headers.

    The width check has no schema equivalent (Pydantic's ``TabularCell`` union
    validates one cell in isolation; it cannot see ``headers`` to catch a
    ragged row) and is load-bearing on every path. The per-cell type check
    duplicates what the MCP tool's ``TabularCell`` schema already enforces for
    an MCP-originated call, but this function is also called directly (see the
    unit tests) with values that never passed through that schema — so it
    stays as this leaf's own contract, not dead code.
    """
    for row_index, row in enumerate(rows):
        if len(row) != len(headers):
            raise ValueError(
                f"row {row_index} has {len(row)} cells but there are {len(headers)} headers; "
                f"every row must have exactly one cell per header"
            )
        for column_index, cell in enumerate(row):
            if cell is None or isinstance(cell, str | bool | int):
                continue
            if isinstance(cell, float):
                if math.isfinite(cell):
                    continue
                raise ValueError(
                    f"cell at row {row_index}, column {column_index} is {cell!r}; "
                    f"a cell must be a finite number"
                )
            raise ValueError(
                f"cell at row {row_index}, column {column_index} has unsupported type "
                f"{type(cell).__name__}; a cell must be a string, number, boolean, or null"
            )


def _walk_rows(rows: list[list[TabularCell]], check: Callable[[TabularCell, str], None]) -> None:
    """Call ``check(cell, where)`` for every data cell, in row-major order."""
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            check(cell, f"the cell at row {row_index}, column {column_index}")


def _walk_headers_and_rows(
    headers: list[str],
    rows: list[list[TabularCell]],
    check: Callable[[TabularCell, str], None],
) -> None:
    """Call ``check(cell, where)`` for every header, then every data cell."""
    for index, header in enumerate(headers):
        check(header, f"header {index}")
    _walk_rows(rows, check)


def _reject_csv_formulas(headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Reject strings a spreadsheet would read back as a formula.

    A CSV field carries no type, so a leading ``=``/``+``/``-``/``@`` is
    indistinguishable from a formula when the file is reopened in Excel or
    Calc. Since the alternative would be to alter the value (quoting or
    prefixing it), the write is refused instead. Numbers are unaffected: send
    ``-5`` as the number ``-5``, not the string ``"-5"``.
    """

    def check(value: TabularCell, where: str) -> None:
        if not isinstance(value, str):
            return
        # A leading U+FEFF is not whitespace, so plain .lstrip() leaves it in
        # place; written verbatim by the utf-8 (non -sig) writer below, those
        # bytes ARE a file BOM, which spreadsheet readers consume before
        # parsing what follows — silently turning a would-be-rejected formula
        # into one that reads back as executable.
        stripped = value.lstrip("\ufeff").lstrip()
        if stripped.startswith(CSV_FORMULA_PREFIXES):
            prefixes = "".join(CSV_FORMULA_PREFIXES)
            raise ValueError(
                f"{where} is the string {value!r}, which a spreadsheet would read back as a "
                f"formula (a CSV field cannot say 'this is literal text'). CSV rejects strings "
                f"starting with any of {prefixes!r}. Send a number as a numeric cell rather "
                f"than a string, or write .xlsx, which stores the text literally."
            )

    _walk_headers_and_rows(headers, rows, check)


def _reject_oversized_xlsx_strings(headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Reject strings past Excel's per-cell limit, which openpyxl would silently truncate."""

    def check(value: TabularCell, where: str) -> None:
        if isinstance(value, str) and len(value) > XLSX_MAX_STRING_LENGTH:
            raise ValueError(
                f"{where} is {len(value)} characters long, over the {XLSX_MAX_STRING_LENGTH}-"
                f"character limit for one XLSX cell; shorten it rather than let it be truncated"
            )

    _walk_headers_and_rows(headers, rows, check)


# XML 1.0's Char production admits only #x9 | #xA | #xD | [#x20-#xD7FF] |
# [#xE000-#xFFFD] | [#x10000-#x10FFFF] — so this is every C0 control other
# than tab/LF/CR, plus the surrogate range and the two BMP noncharacters.
# openpyxl's own ILLEGAL_CHARACTERS_RE (used by Cell.check_string, which
# raises IllegalCharacterError) only covers the C0 controls: a character like
# U+FFFF or a lone surrogate sails through that check, the write "succeeds",
# and the writer emits a numeric character reference the XML spec forbids —
# so the file cannot be re-parsed at all. This is the full excluded range, so
# nothing here can silently produce an unreadable workbook.
_XML_ILLEGAL_CHARACTERS_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")


def _check_no_illegal_xml_characters(value: str, where: str) -> None:
    """Reject one string with a character XML cannot represent at all.

    Also used by the ``style``/``gantt``/``charts`` leaves: a sheet name, a
    title, or a number-format code reaches the same XML writer as a cell does,
    so an unchecked one corrupts the workbook exactly the same way.
    """
    if _XML_ILLEGAL_CHARACTERS_RE.search(value):
        raise ValueError(
            f"{where} contains a character XML (and therefore XLSX) cannot "
            f"represent; remove it before writing"
        )


def _reject_illegal_xlsx_characters(headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Reject a string with a character XML cannot represent at all."""

    def check(value: TabularCell, where: str) -> None:
        if isinstance(value, str):
            _check_no_illegal_xml_characters(value, where)

    _walk_headers_and_rows(headers, rows, check)


def _check_no_carriage_return(value: str, where: str) -> None:
    """Reject one string containing a carriage return.

    Shared with the ``style``/``gantt``/``charts`` leaves for the same reason
    as ``_check_no_illegal_xml_characters``; see
    ``_reject_xlsx_carriage_returns`` for why a bare ``\\r`` cannot survive.
    """
    if "\r" in value:
        raise ValueError(
            f"{where} contains a carriage return, which XML normalizes to a plain "
            f"newline on read (XLSX cannot store \\r or \\r\\n as written); use \\n "
            f"instead, or write .csv, which preserves it exactly"
        )


def _reject_xlsx_carriage_returns(headers: list[str], rows: list[list[TabularCell]]) -> None:
    """Reject a string containing a carriage return, which XML normalizes away on read.

    A bare ``\\r`` is XML-legal (unlike the characters
    ``_reject_illegal_xlsx_characters`` rejects), so openpyxl writes it verbatim and the
    write "succeeds" — but XML 1.0's end-of-line handling (Char production, section 2.11)
    requires every conformant parser to normalize a lone CR or a CRLF pair to a single LF
    while parsing, not just this file's own read path. ``"a\\r\\nb"`` and ``"a\\rb"``
    therefore both come back as ``"a\\nb"`` from any XML-compliant reader, openpyxl
    included. Since the alternative would be to silently change the value on write, the
    write is refused instead. CSV has no such normalization (this package's writer/reader
    pair both pass ``newline=""``), so this is XLSX-specific.
    """

    def check(value: TabularCell, where: str) -> None:
        if isinstance(value, str):
            _check_no_carriage_return(value, where)

    _walk_headers_and_rows(headers, rows, check)


def _reject_columnless_xlsx(headers: list[str]) -> None:
    """Reject a zero-column XLSX write: openpyxl drops every row of it on read.

    ``_validate_cells`` already requires every row's width to match
    ``len(headers)``, so a row with zero cells can only occur when ``headers``
    itself is empty — every row, including the header, is then a bare ``<row>``
    element with no ``<c>`` children. openpyxl derives its saved ``<dimension>``
    purely from cells that were actually assigned a value; with none anywhere
    in the sheet, it falls back to a 1x1 ``"A1:A1"``, and the read-only reader
    trusts that declared bound to cap iteration — silently dropping every row
    past the first. CSV has no such bound (a blank line reads back as ``[]``
    just fine), so this is XLSX-specific.
    """
    if not headers:
        raise ValueError(
            "an XLSX write needs at least one column: a zero-column table has no cells "
            "anywhere in the sheet, and openpyxl silently drops every row of it on read"
        )


def _reject_xlsx_empty_strings(rows: list[list[TabularCell]]) -> None:
    """Reject an empty-string row cell, which XLSX cannot tell apart from null.

    openpyxl's writer never emits an inline-string element for a ``""`` cell
    (``etree_write_cell``/``lxml_write_cell`` skip any cell whose value is
    ``None`` or ``""``), so it always reads back as ``None`` regardless of the
    cell's declared type. Since the alternative would be to silently change
    the value on write, the write is refused instead — send ``null`` for "no
    value". Headers are exempt: ``_normalize_header`` already documents that a
    blank header becomes a positional name, so that collapse is intentional,
    not silent loss.
    """

    def check(cell: TabularCell, where: str) -> None:
        if cell == "":
            raise ValueError(
                f"{where} is an empty string, which XLSX cannot distinguish from null "
                f"and always reads back as null; send null instead of an empty string"
            )

    _walk_rows(rows, check)


def _reject_lossy_xlsx_numbers(rows: list[list[TabularCell]]) -> None:
    """Reject a number XLSX's numeric write format cannot hold exactly, or whose
    int/float type would silently flip on read-back.

    openpyxl serializes every numeric cell — int or float alike — through
    ``"%.16g" % value`` (``openpyxl.compat.strings.safe_string``), a fixed
    16-significant-digit text format, with no error when a value needs more
    digits than that to round-trip: an integer past 2**53 or a float needing a
    17th significant digit comes back changed. Detect it by formatting through
    the same ``%.16g`` openpyxl will use and checking the value survives, and
    refuse the write rather than let it through.

    XLSX also has no separate int/float cell type: whether a value reads back
    as ``int`` or ``float`` is inferred purely from whether that same
    ``%.16g`` text contains a ``.``/``e`` — so an integral float like ``1.0``
    formats as ``"1"`` and reads back an ``int``, and a large int like
    ``10**16`` formats as ``"1e+16"`` and reads back a ``float``. There is no
    write-time knob to force the other shape (a cell's number_format is
    cosmetic display only; it does not change the stored ``<v>`` text), so
    this is refused too rather than silently changing the cell's type.
    """

    def check(cell: TabularCell, where: str) -> None:
        if isinstance(cell, bool) or not isinstance(cell, int | float):
            return
        try:
            formatted = f"{cell:.16g}"
        except OverflowError as exc:
            # An int with no float equivalent at all (e.g. 10**400):
            # "%.16g" converts through float internally, which raises
            # OverflowError — not a ValueError — for a magnitude beyond
            # what an IEEE 754 double can hold.
            raise ValueError(
                f"{where} is {cell!r}, too large in magnitude for XLSX's numeric format "
                f"(an IEEE 754 double) to represent at all; send it as a string instead"
            ) from exc
        if float(formatted) != cell:
            raise ValueError(
                f"{where} is {cell!r}, which XLSX's 16-significant-digit numeric format "
                f"cannot represent exactly; send it as a string instead, or reduce its "
                f"precision"
            )
        looks_integral = not any(marker in formatted for marker in ".eE")
        if looks_integral != isinstance(cell, int):
            was = "an int" if isinstance(cell, int) else "a float"
            becomes = "an int" if looks_integral else "a float"
            raise ValueError(
                f"{where} is {cell!r}, {was}, but XLSX would write it as {formatted!r} and "
                f"read it back as {becomes}, silently changing its type; send it as a "
                f"string instead if the type must be preserved exactly"
            )

    _walk_rows(rows, check)
