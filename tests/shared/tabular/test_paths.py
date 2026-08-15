"""Tests for the tabular read/write path validators.

These use real files under ``tmp_path``: the leaf's whole job is filesystem
behavior, so mocking the filesystem would test nothing.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from openconstraint_mcp.shared.tabular.paths import (
    validate_tabular_read_path,
    validate_tabular_write_path,
)


def _write_csv(path: Path, records: list[list[object]]) -> Path:
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(records)
    return path


def test_read_path_rejects_an_unsupported_suffix(tmp_path: Path) -> None:
    target = tmp_path / "data.ods"
    target.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported tabular file type"):
        validate_tabular_read_path(target)


def test_read_path_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        validate_tabular_read_path(tmp_path / "absent.csv")


def test_read_path_rejects_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "a_directory.csv"
    target.mkdir()
    with pytest.raises(ValueError, match="not a regular file"):
        validate_tabular_read_path(target)


def test_read_path_accepts_an_uppercase_suffix(tmp_path: Path) -> None:
    target = _write_csv(tmp_path / "DATA.CSV", [["a"], ["1"]])
    assert validate_tabular_read_path(target) == target


def test_write_path_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_tabular_write_path(Path("out.csv"))


def test_write_path_rejects_a_missing_parent_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="parent directory does not exist"):
        validate_tabular_write_path(tmp_path / "absent" / "out.csv")


def test_write_path_rejects_an_unsupported_suffix(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported tabular file type"):
        validate_tabular_write_path(tmp_path / "out.txt")
