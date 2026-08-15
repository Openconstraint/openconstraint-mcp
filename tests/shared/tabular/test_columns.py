"""Tests for the shared header-name → column-index lookup."""

from __future__ import annotations

import pytest

from openconstraint_mcp.shared.tabular.columns import column_index


def test_a_named_header_resolves_to_its_position() -> None:
    assert column_index(["task", "start", "end"], "end", "end_column") == 2


def test_a_missing_header_is_rejected() -> None:
    with pytest.raises(ValueError, match="'absent'"):
        column_index(["task", "start"], "absent", "start_column")


def test_a_missing_header_error_lists_the_available_headers() -> None:
    with pytest.raises(ValueError, match="headers are: task, start"):
        column_index(["task", "start"], "absent", "start_column")


def test_a_duplicated_header_is_rejected_as_ambiguous() -> None:
    # Duplicate headers are preserved by design, so a repeated name picks none.
    with pytest.raises(ValueError, match="ambiguous"):
        column_index(["qty", "qty"], "qty", "x_column")


def test_the_role_names_the_caller_field_in_the_error() -> None:
    with pytest.raises(ValueError, match="x_column 'absent'"):
        column_index(["task"], "absent", "x_column")
