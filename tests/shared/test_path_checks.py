"""Tests for the absolute-path/existing-file resolution leaf.

Real paths under ``tmp_path``: the leaf's whole job is filesystem behavior, so
mocking the filesystem would test nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openconstraint_mcp.shared.path_checks import (
    require_absolute_path,
    resolve_absolute_target,
    resolve_existing_file,
)


class _CustomError(ValueError):
    """A caller-supplied error type, as ``save_target`` supplies its own."""


# --- require_absolute_path ------------------------------------------------------


def test_require_absolute_path_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="thing must be an absolute path"):
        require_absolute_path(Path("out.csv"), arg_name="thing")


def test_require_absolute_path_expands_a_tilde() -> None:
    # "~/out.csv".is_absolute() is False, so expansion must happen FIRST or a
    # path naming an absolute location is rejected as relative.
    assert require_absolute_path(Path("~/out.csv"), arg_name="thing") == (
        Path("~").expanduser() / "out.csv"
    )


def test_require_absolute_path_names_the_unexpanded_input_in_its_message() -> None:
    # The caller passed "out.csv"; echoing a resolved path they never wrote
    # would point at a directory they did not name.
    with pytest.raises(ValueError, match=r"out\.csv"):
        require_absolute_path(Path("out.csv"), arg_name="thing")


def test_require_absolute_path_raises_the_callers_error_type() -> None:
    with pytest.raises(_CustomError):
        require_absolute_path(Path("out.csv"), arg_name="thing", error_type=_CustomError)


# --- resolve_existing_file ------------------------------------------------------


def test_resolve_existing_file_returns_the_resolved_path(tmp_path: Path) -> None:
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    assert resolve_existing_file(target, arg_name="thing") == target.resolve()


def test_resolve_existing_file_rejects_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="thing does not exist"):
        resolve_existing_file(tmp_path / "absent.csv", arg_name="thing")


def test_resolve_existing_file_rejects_a_directory(tmp_path: Path) -> None:
    target = tmp_path / "a_directory"
    target.mkdir()
    with pytest.raises(ValueError, match="thing is not a regular file"):
        resolve_existing_file(target, arg_name="thing")


def test_resolve_existing_file_accepts_a_relative_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This leaf carries no absolute policy: a caller that wants one layers
    # require_absolute_path on top, so minizinc's relative inputs keep working.
    target = tmp_path / "data.csv"
    target.write_text("a\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert resolve_existing_file(Path("data.csv"), arg_name="thing") == target.resolve()


# --- resolve_absolute_target ----------------------------------------------------


def test_resolve_absolute_target_accepts_a_tilde_path() -> None:
    # Regression: is_absolute() ran BEFORE expansion, so "~/out.xlsx" was
    # refused as relative while the read path expanded the same input happily.
    resolved = resolve_absolute_target(
        Path("~/out.xlsx"), arg_name="target_path", kind="regular file", is_valid_kind=Path.is_file
    )
    assert resolved == (Path("~").expanduser() / "out.xlsx").resolve()


def test_resolve_absolute_target_still_rejects_a_relative_path() -> None:
    with pytest.raises(ValueError, match="target_path must be an absolute path"):
        resolve_absolute_target(
            Path("out.xlsx"), arg_name="target_path", kind="regular file",
            is_valid_kind=Path.is_file,
        )
