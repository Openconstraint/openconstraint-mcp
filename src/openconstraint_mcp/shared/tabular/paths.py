"""Suffix-to-format mapping and the read/write path validators."""

from __future__ import annotations

from pathlib import Path

from ...schemas.tabular import TabularFormat
from ..path_checks import resolve_absolute_target

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
    """Resolve ``path`` and require an existing ``.xlsx``/``.csv`` regular file."""
    resolved = path.expanduser().resolve()
    _format_for(resolved)
    if not resolved.exists():
        raise ValueError(f"tabular file does not exist: {resolved}")
    if not resolved.is_file():
        raise ValueError(f"tabular path is not a regular file: {resolved}")
    return resolved


def validate_tabular_write_path(path: Path) -> Path:
    """Resolve ``path`` and require an absolute ``.xlsx``/``.csv`` target in an existing dir."""
    resolved = resolve_absolute_target(
        path, arg_name="target_path", kind="regular file", is_valid_kind=Path.is_file
    )
    _format_for(resolved)
    return resolved
