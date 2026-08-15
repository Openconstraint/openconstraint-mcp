"""Absolute-path resolution, as a dependency-light leaf.

Stdlib only. The low-level shape shared by ``save_target.validate_save_target``
(a managed, manifest-gated directory) and
``tabular.paths.validate_tabular_write_path`` (a single plain file) is just
"absolute, right kind if it exists, parent exists" — the manifest/overwrite
policy on top of that is deliberately NOT shared (see the ``tabular`` package's
docstring), so this leaf stops at the part both callers actually agree on.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def resolve_absolute_target(
    path: Path,
    *,
    arg_name: str,
    kind: str,
    is_valid_kind: Callable[[Path], bool],
    error_type: type[ValueError] = ValueError,
) -> Path:
    """Resolve ``path``, requiring absolute, ``kind`` if it exists, and an existing parent.

    Raises ``error_type`` (a ``ValueError`` subclass; plain ``ValueError`` by
    default) naming ``arg_name`` (the caller's parameter name, for a message
    the caller's own client argument maps back to) on any violation.
    ``error_type`` lets a caller with its own classifiable exception type (see
    ``save_target.InvalidSaveTargetError``) opt into it here too, rather than
    catching and re-raising — this stays stdlib-only either way, since the
    caller supplies the type.
    """
    if not path.is_absolute():
        raise error_type(f"{arg_name} must be an absolute path: {path}")
    resolved = path.resolve()
    if resolved.exists() and not is_valid_kind(resolved):
        raise error_type(f"{arg_name} exists but is not a {kind}: {resolved}")
    if not resolved.parent.is_dir():
        raise error_type(f"{arg_name} parent directory does not exist: {resolved.parent}")
    return resolved
