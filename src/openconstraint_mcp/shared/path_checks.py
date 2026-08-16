"""Path resolution primitives, as a dependency-light leaf.

Stdlib only. Three primitives, deliberately separate so a caller composes the
policy it wants instead of inheriting one it does not:

* ``require_absolute_path`` — the absolute-only policy on its own. An MCP
  server's working directory is the CLIENT's choice and is unknowable to the
  caller, so a relative path names an unpredictable location; refusing it up
  front beats failing later against a directory nobody wrote.
* ``resolve_existing_file`` — "resolve, must exist, must be a regular file",
  carrying NO absolute policy, because ``minizinc.files`` documents relative
  inputs as merely discouraged rather than refused.
* ``resolve_absolute_target`` — an output target: absolute, right kind if it
  exists, parent exists.

Every one of them expands ``~`` before judging the result. ``~/out.xlsx`` is
not ``is_absolute()``, so expanding second would refuse a path that names a
perfectly absolute location.

The manifest/overwrite policy that ``save_target.validate_save_target`` layers
on top is deliberately NOT shared (see the ``tabular`` package's docstring), so
this leaf stops at the part its callers actually agree on.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path


def require_absolute_path(
    path: Path, *, arg_name: str, error_type: type[ValueError] = ValueError
) -> Path:
    """Expand ``~`` in ``path`` and require the result to be absolute.

    The message echoes the caller's ORIGINAL input, not the expansion: a caller
    who wrote ``out.csv`` should not be shown a resolved path they never named.
    """
    expanded = path.expanduser()
    if not expanded.is_absolute():
        raise error_type(f"{arg_name} must be an absolute path: {path}")
    return expanded


def resolve_existing_file(
    path: Path, *, arg_name: str, error_type: type[ValueError] = ValueError
) -> Path:
    """Resolve ``path`` and require it to be an existing regular file.

    Follows a symlink the caller named (``Path.resolve``) and returns the
    resolved path, so a caller uses the same path for its read, its argv, and
    its cwd — a relative input cannot then double-count its subdirectory.
    """
    resolved = path.expanduser().resolve()
    if not resolved.exists():
        raise error_type(f"{arg_name} does not exist: {resolved}")
    if not resolved.is_file():
        raise error_type(f"{arg_name} is not a regular file: {resolved}")
    return resolved


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
    resolved = require_absolute_path(path, arg_name=arg_name, error_type=error_type).resolve()
    if resolved.exists() and not is_valid_kind(resolved):
        raise error_type(f"{arg_name} exists but is not a {kind}: {resolved}")
    if not resolved.parent.is_dir():
        raise error_type(f"{arg_name} parent directory does not exist: {resolved.parent}")
    return resolved
