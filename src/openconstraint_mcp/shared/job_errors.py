"""Shared job-registry primitives, dependency-light so every job registry can import them.

``JobRejectedError`` was previously in ``jobs.py``, which imports ``minizinc.core``.
Moving it here lets ``pyexec/jobs.py`` raise the same type without pulling the
MiniZinc path into the CP-SAT subtree — per AGENTS.md ("extract a shared primitive to a
dependency-light leaf both import", precedent ``runtime_install/errors.py``). ``now_ms``
and ``exception_summary`` were each duplicated identically across ``jobs.py``,
``portfolio_jobs.py``, and/or ``pyexec/jobs.py``; they live here for the same reason.
"""

from __future__ import annotations

import time


class JobRejectedError(RuntimeError):
    """Raised when a submit would exceed the bounded running+queued capacity."""


class UnknownJobError(ValueError):
    """Raised for a ``job_id`` a registry does not hold: never admitted, or evicted.

    A ``ValueError``, so the server's unknown-id translation still applies, yet its own
    type: a caller that skips evicted jobs must not also swallow pydantic's
    ``ValidationError``, which is a ``ValueError`` too.
    """


def now_ms() -> int:
    return int(time.time() * 1000)


def exception_summary(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"
