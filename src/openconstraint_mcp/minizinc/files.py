from __future__ import annotations

from pathlib import Path

from ..shared.path_checks import resolve_existing_file

# MiniZinc rejects a `--solution-checker` whose filename does not end in `.mzc`
# or `.mzc.mzn` at argument parsing. Matched on the full `name`, NOT `Path.suffix`
# (which returns `.mzn` for `model.mzc.mzn`), so the validator rejects the wrong
# suffix server-side before the doomed run.
_CHECKER_SUFFIXES: tuple[str, ...] = (".mzc", ".mzc.mzn")


def read_text_utf8(path: Path) -> str:
    """Read ``path`` as UTF-8, surfacing a bad encoding as a clear ValueError.

    The path tools assume UTF-8 source (MiniZinc's convention); wrapping
    ``UnicodeDecodeError`` here turns an opaque traceback into the repo's
    "clear errors" bar, with the offending path named in the message.
    """
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path} is not valid UTF-8") from exc


def validate_model_data_paths(model_path: Path, data_path: Path | None) -> tuple[Path, Path | None]:
    """Resolve, validate, and return the model/data paths before any subprocess.

    Resolves each input to an absolute path (``Path.resolve()`` — following a
    symlink the caller named), then rejects a missing or non-regular-file
    model/data, and an empty/whitespace-only or non-UTF-8 model, with a clear
    ``ValueError`` naming the offending path. The resolved paths are *returned*
    so callers use the same path for read, argv, and cwd — a relative input
    can't then double-count its subdir (``cwd=parent`` + relative argv).

    Model emptiness and UTF-8 are checked here (the model is read for the
    check), so the failure is a clear ``ValueError`` before any run. Data
    emptiness is allowed (a valid "no parameters" input, matching the inline
    ``data`` contract).
    """
    model_path = resolve_existing_file(model_path, arg_name="model_path")
    if not read_text_utf8(model_path).strip():
        raise ValueError(f"model file is empty: {model_path}")
    if data_path is None:
        return model_path, None
    return model_path, resolve_existing_file(data_path, arg_name="data_path")


def validate_checker_path(checker_path: Path) -> Path:
    """Resolve, validate, and return the checker path before any subprocess.

    A sibling of ``validate_model_data_paths`` for the ``--solution-checker``
    argument: resolves to absolute (so the flag is unambiguous regardless of
    cwd), then rejects — with a clear ``ValueError`` naming the path — a checker
    whose filename does not end in ``.mzc``/``.mzc.mzn`` (MiniZinc rejects other
    suffixes at argument parsing; the check is on ``name``, not ``Path.suffix``,
    which returns ``.mzn`` for ``model.mzc.mzn``), a missing or non-regular-file
    checker, and a non-UTF-8 checker. The resolved absolute path is returned so
    the caller uses the same path the validation ran against.
    """
    checker_path = checker_path.expanduser().resolve()
    # Suffix first: MiniZinc rejects a wrong-suffix checker at argument parsing,
    # so that is the more actionable message even when the file is also missing.
    if not checker_path.name.endswith(_CHECKER_SUFFIXES):
        raise ValueError(f"checker_path must end in .mzc or .mzc.mzn: {checker_path}")
    checker_path = resolve_existing_file(checker_path, arg_name="checker_path")
    read_text_utf8(checker_path)  # reject non-UTF-8 with a clear ValueError
    return checker_path
