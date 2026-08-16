"""Suffix-to-format mapping and the read/write path validators."""

from __future__ import annotations

from pathlib import Path

from ...schemas.tabular import TabularFormat
from ..path_checks import require_absolute_path, resolve_absolute_target, resolve_existing_file

_SUFFIX_FORMATS: dict[str, TabularFormat] = {".xlsx": "xlsx", ".csv": "csv"}


def _format_for(path: Path) -> TabularFormat:
    """Return the tabular format for ``path``'s suffix, or raise ``ValueError``."""
    fmt = _SUFFIX_FORMATS.get(path.suffix.lower())
    if fmt is None:
        accepted = ", ".join(sorted(_SUFFIX_FORMATS))
        raise ValueError(
            f"unsupported tabular file type {path.suffix!r} for {path}; expected one of {accepted}"
        )
    return fmt


def validate_tabular_read_path(path: Path) -> Path:
    """Resolve ``path`` and require an existing absolute ``.xlsx``/``.csv`` regular file.

    Absolute is required here for the same reason the write path requires it:
    the server's working directory belongs to the MCP client, so a relative
    path names a location the caller cannot predict. The suffix is checked
    before existence so a ``.ods`` typo reports the unsupported type rather
    than a missing file.
    """
    expanded = require_absolute_path(path, arg_name="path")
    _format_for(expanded)
    return resolve_existing_file(expanded, arg_name="tabular file")


def validate_tabular_write_path(path: Path) -> Path:
    """Resolve ``path`` and require an absolute ``.xlsx``/``.csv`` target in an existing dir."""
    resolved = resolve_absolute_target(
        path, arg_name="target_path", kind="regular file", is_valid_kind=Path.is_file
    )
    _format_for(resolved)
    return resolved
