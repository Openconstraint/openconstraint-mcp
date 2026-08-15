"""Tests for the write guards: cell shape plus the per-format rejections.

These drive the guards through ``write_tabular_data`` — the caller that runs
every one of them, in the order that keeps a refused write from touching the
filesystem.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.schemas.tabular import TabularCell
from openconstraint_mcp.shared.tabular.core import read_tabular_data, write_tabular_data
from openconstraint_mcp.shared.tabular.limits import XLSX_MAX_STRING_LENGTH


def test_write_rejects_a_row_wider_than_the_headers(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="row 0 has 3 cells but there are 2 headers"):
        write_tabular_data(["a", "b"], [["1", "2", "3"]], target)


def test_write_rejects_a_row_narrower_than_the_headers(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="row 0 has 1 cells but there are 2 headers"):
        write_tabular_data(["a", "b"], [["1"]], target)


def test_write_rejects_a_nested_container_cell(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    bad: list[list[TabularCell]] = [[["nested"]]]  # type: ignore[list-item]
    with pytest.raises(ValueError, match="unsupported type list"):
        write_tabular_data(["a"], bad, target)


def test_write_rejects_a_non_finite_float_cell(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="must be a finite number"):
        write_tabular_data(["a"], [[float("inf")]], target)


# --- writing: formula safety ----------------------------------------------------------


@pytest.mark.parametrize("value", ["=1+1", "+1", "-5", "@SUM(A1)"])
def test_csv_write_rejects_a_formula_looking_string(tmp_path: Path, value: str) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="formula"):
        write_tabular_data(["a"], [[value]], target)


def test_csv_write_rejects_a_formula_looking_string_after_leading_whitespace(
    tmp_path: Path,
) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="formula"):
        write_tabular_data(["a"], [["  =1+1"]], target)


def test_csv_write_rejects_a_formula_looking_header(tmp_path: Path) -> None:
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="formula"):
        write_tabular_data(["=total"], [["1"]], target)


def test_csv_write_rejects_a_formula_looking_string_with_a_leading_bom(tmp_path: Path) -> None:
    # A leading U+FEFF is not whitespace, so plain lstrip() alone misses it;
    # written verbatim by the CSV writer, those bytes ARE a file BOM, which a
    # spreadsheet reader consumes before parsing what follows — turning a
    # would-be-rejected formula into one that reads back as live.
    target = tmp_path / "out.csv"
    with pytest.raises(ValueError, match="formula"):
        write_tabular_data(["\ufeff=1+1"], [["x"]], target)


def test_csv_write_accepts_a_negative_number_as_a_numeric_cell(tmp_path: Path) -> None:
    # The escape hatch the rejection message points at: -5 the number is fine;
    # only "-5" the string is refused.
    target = tmp_path / "out.csv"
    write_tabular_data(["a"], [[-5]], target)
    assert target.read_text(encoding="utf-8").splitlines()[1] == "-5"


# --- writing: the XLSX string limit -----------------------------------------------------


def test_xlsx_write_accepts_a_string_at_the_length_limit(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    value = "x" * XLSX_MAX_STRING_LENGTH
    write_tabular_data(["a"], [[value]], target)
    assert read_tabular_data(target).rows == [[value]]


def test_xlsx_write_rejects_a_string_one_character_over_the_limit(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="over the 32767-character limit"):
        write_tabular_data(["a"], [["x" * (XLSX_MAX_STRING_LENGTH + 1)]], target)


def test_an_over_limit_xlsx_string_creates_no_file(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError):
        write_tabular_data(["a"], [["x" * (XLSX_MAX_STRING_LENGTH + 1)]], target)
    assert not target.exists()


def test_an_over_limit_xlsx_string_does_not_replace_an_existing_target(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    write_tabular_data(["a"], [["keep"]], target)
    before = target.read_bytes()
    with pytest.raises(ValueError):
        write_tabular_data(["a"], [["x" * (XLSX_MAX_STRING_LENGTH + 1)]], target, overwrite=True)
    assert target.read_bytes() == before


# --- writing: XLSX round-trip hazards -------------------------------------------------


def test_xlsx_write_rejects_an_empty_string_cell(tmp_path: Path) -> None:
    # openpyxl never emits an inline-string element for "", so it always reads
    # back as null regardless of the cell's declared type.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="empty string"):
        write_tabular_data(["a"], [[""]], target)


def test_xlsx_write_accepts_an_empty_string_header(tmp_path: Path) -> None:
    # A blank header already collapses to a positional name by design (see
    # _normalize_header), so only row cells are subject to the empty-string
    # rejection above.
    target = tmp_path / "out.xlsx"
    write_tabular_data([""], [["x"]], target)
    assert read_tabular_data(target).headers == ["col_1"]


def test_xlsx_write_rejects_a_control_character_illegal_in_xml(tmp_path: Path) -> None:
    # A JSON-valid string may contain e.g. U+0001, which XML forbids. Left to
    # the write itself, openpyxl's Cell.check_string raises
    # IllegalCharacterError for it — not a ValueError — which would otherwise
    # bypass the tools' error translation.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="cannot represent"):
        write_tabular_data(["a"], [["bad" + chr(1) + "char"]], target)


@pytest.mark.parametrize("codepoint", [0xD800, 0xDFFF, 0xFFFE, 0xFFFF])
def test_xlsx_write_rejects_a_character_illegal_in_xml_but_not_caught_by_openpyxl(
    tmp_path: Path, codepoint: int
) -> None:
    # openpyxl's own IllegalCharacterError check only covers C0 controls. A
    # lone surrogate or a BMP noncharacter (U+FFFE/U+FFFF) passes that check,
    # the write "succeeds", but the writer emits a numeric character
    # reference the XML spec forbids, corrupting the file so it cannot be
    # read back at all.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="cannot represent"):
        write_tabular_data(["a"], [["bad" + chr(codepoint) + "char"]], target)


def test_xlsx_write_accepts_a_tab_which_xml_permits(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    value = "col1\tcol2"
    write_tabular_data(["a"], [[value]], target)
    assert read_tabular_data(target).rows == [[value]]


def test_xlsx_write_accepts_an_astral_noncharacter_which_xml_permits(tmp_path: Path) -> None:
    # Unicode marks the last two code points of each supplementary plane as
    # "noncharacters" (e.g. U+1FFFE), but XML 1.0's Char production admits
    # the whole [#x10000-#x10FFFF] range, so these round-trip fine — unlike
    # the BMP noncharacters U+FFFE/U+FFFF, which XML explicitly excludes.
    target = tmp_path / "out.xlsx"
    value = "bad" + chr(0x1FFFE) + "char"
    write_tabular_data(["a"], [[value]], target)
    assert read_tabular_data(target).rows == [[value]]


@pytest.mark.parametrize("value", ["a\r\nb", "a\rb"])
def test_xlsx_write_rejects_a_carriage_return(tmp_path: Path, value: str) -> None:
    # \r is XML-legal, so openpyxl writes it verbatim and the write "succeeds"
    # — but XML 1.0 mandates every parser normalize a lone CR or CRLF to a
    # plain LF while parsing, so the value would silently change on read.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="carriage return"):
        write_tabular_data(["a"], [[value]], target)


def test_xlsx_write_rejects_a_carriage_return_in_a_header(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="carriage return"):
        write_tabular_data(["a\r\nb"], [["x"]], target)


def test_csv_write_round_trips_a_carriage_return(tmp_path: Path) -> None:
    # CSV has no equivalent normalization: this package's reader and writer
    # both pass newline="", so an embedded \r\n inside a quoted field survives
    # exactly.
    target = tmp_path / "out.csv"
    value = "a\r\nb"
    write_tabular_data(["a"], [[value]], target)
    assert read_tabular_data(target).rows == [[value]]


def test_xlsx_write_rejects_an_integer_past_the_16_digit_boundary(tmp_path: Path) -> None:
    # openpyxl serializes every numeric cell through "%.16g", which loses
    # precision past 2**53 with no error of its own.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="16-significant-digit"):
        write_tabular_data(["a"], [[2**53 + 1]], target)


def test_xlsx_write_accepts_an_integer_at_the_16_digit_boundary(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    write_tabular_data(["a"], [[2**53]], target)
    assert read_tabular_data(target).rows == [[2**53]]


def test_xlsx_write_rejects_a_float_needing_a_17th_significant_digit(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="16-significant-digit"):
        write_tabular_data(["a"], [[1.2345678901234567]], target)


def test_xlsx_write_accepts_a_float_at_the_16_digit_boundary(tmp_path: Path) -> None:
    target = tmp_path / "out.xlsx"
    write_tabular_data(["a"], [[1.234567890123456]], target)
    assert read_tabular_data(target).rows == [[1.234567890123456]]


def test_xlsx_write_rejects_an_integral_float_that_would_read_back_as_an_int(
    tmp_path: Path,
) -> None:
    # "%.16g" formats 1.0 as "1", with no decimal point, so it would read back
    # a Python int — a silent type change the value-only check above misses.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="silently changing its type"):
        write_tabular_data(["a"], [[1.0]], target)


def test_xlsx_write_rejects_a_large_int_that_would_read_back_as_a_float(tmp_path: Path) -> None:
    # "%.16g" switches to exponential notation past 16 digits, so it would
    # read back a Python float — a silent type change the other direction.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="silently changing its type"):
        write_tabular_data(["a"], [[10**16]], target)


def test_xlsx_write_rejects_an_integer_beyond_float_range_without_leaking_overflowerror(
    tmp_path: Path,
) -> None:
    # TabularCell's StrictInt has no magnitude bound, but "%.16g" converts
    # through float internally, which raises OverflowError -- not a
    # ValueError -- for a value float() cannot hold at all.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="too large in magnitude"):
        write_tabular_data(["a"], [[10**400]], target)


def test_xlsx_write_rejects_a_columnless_table(tmp_path: Path) -> None:
    # Every row's width must match len(headers) (_validate_cells), so a
    # zero-cell row can only occur when headers itself is empty. openpyxl then
    # has no cells anywhere to derive a dimension from and silently drops
    # every row on read — refuse the write instead.
    target = tmp_path / "out.xlsx"
    with pytest.raises(ValueError, match="at least one column"):
        write_tabular_data([], [[], []], target)
