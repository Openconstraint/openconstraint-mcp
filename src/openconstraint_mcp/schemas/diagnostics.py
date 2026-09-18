"""Stable, client-branchable diagnostic surface.

``diagnostic=None`` on a result/job model is the clean-success signal; a
``Diagnostic`` exists only when there is something actionable or noteworthy for
the client. ``category`` is the stable enum a client branches on *before*
scraping raw stdout/stderr/transcripts.

This is the dependency-light shared leaf every layer imports (like
``schemas/job_state.py``): it imports only stdlib, Pydantic, and
``schemas.job_state``. Its helpers take PRIMITIVE field values (``str``,
``bool``), never sibling result models or sibling ``Literal`` types like
``CheckerStatus`` — those modules import ``Diagnostic``, so any reverse import
would be a cycle. Backend-specific classification (MiniZinc stderr parsing,
CP-SAT status mapping) lives one layer left, in the ``minizinc`` / ``pyexec``
packages.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, JsonValue

from .job_state import RESULT_BEARING_STATES

# The stable client-branching enum. Intentionally coarse: a MiniZinc compiler
# error that cannot be safely split into syntax vs type uses
# ``syntax_or_compile_error`` or ``unknown``, never a guessed subtype. There is
# deliberately NO ``none`` member — clean success is signalled by
# ``diagnostic=None``, not a category.
DiagnosticCategory = Literal[
    # modeling / compile failures
    "syntax_or_compile_error",
    "missing_data",
    "type_error",
    # solver / feasibility outcomes
    "solver_unavailable",
    "infeasible",
    "unbounded",
    "infeasible_or_unbounded",
    # time-limited outcomes
    "timeout_no_incumbent",
    "timeout_with_incumbent",
    # job lifecycle
    "cancelled",
    "job_failed",
    # child-process / output problems
    "child_process_error",
    "output_truncated",
    # save / verification
    "invalid_save_target",
    "not_verified",
    "checker_failed",
    # environment / request
    "runtime_missing",
    "unsupported_feature",
    "invalid_request",
    # exploration
    "no_winner",
    # fallback
    "unknown",
]


class Diagnostic(BaseModel):
    """A compact structured summary of a non-clean or noteworthy outcome.

    ``category`` is the stable enum for client branching; ``message`` is a
    concise human-readable summary; ``details`` is an optional compact dict of
    JSON scalars and short lists (``return_code``, ``timed_out``, ``truncated``,
    ``solver``, ``state``, ``checker_status``, ``attempt_index``, ``artifact``,
    …). ``details`` must NOT duplicate full stdout/stderr/transcripts/source —
    those stay in the enclosing model's raw fields.
    """

    category: DiagnosticCategory
    message: str
    details: dict[str, JsonValue] | None = None


# --- pre-result exception types feeding the two message-shaped categories --
#
# ``server._classify_domain_error`` maps a pre-result exception to a
# ``Diagnostic`` by type where it can, and only falls back to a plain
# ``ValueError`` -> ``invalid_request`` for everything else. These two
# categories cannot use a message-substring/prefix marker at all, unlike
# ``runtime_missing`` (an actual type already): every message in both
# categories embeds caller-controlled text (a solver id, a filesystem path)
# ahead of the only fixed words in the message, so no marker — anchored or
# not — can be 100% immune to a coincidental match. Raising a distinct type
# at the source removes the whole class of collision rather than chasing it
# with ever-narrower string matching.
class UnsupportedFeatureError(ValueError):
    """A pre-result rejection because the resolved solver lacks a requested feature.

    Still a ``ValueError`` (existing ``except ValueError`` catches keep
    working); distinct only so classification can key off type.
    """


class InvalidSaveTargetError(ValueError):
    """A pre-result rejection of a managed, manifest-gated save/verification directory.

    Still a ``ValueError`` for the same reason as ``UnsupportedFeatureError``.
    A single output file (the tabular write tools) has no manifest and no
    managed-directory policy, so it never raises this — only
    ``shared.save_target.validate_save_target`` does.
    """


# --- generic, backend-agnostic classifiers ---------------------------------
# Pure functions over primitive field values. Message/detail construction that
# is backend-specific stays at the call sites; these cover the invariants shared
# across MiniZinc and CP-SAT so the two backends cannot drift.

_CLEAN_CHECKER_STATUSES: frozenset[str] = frozenset({"accepted", "completed"})


def timeout_diagnostic(
    *, has_incumbent: bool, details: dict[str, JsonValue] | None = None
) -> Diagnostic:
    """Build the time-limit diagnostic, split by whether an incumbent survived.

    ``has_incumbent`` is True when the timed-out run still carries at least one
    parsed solution — ``timeout_with_incumbent`` (a reportable best) — else
    ``timeout_no_incumbent`` (nothing found before the cap).
    """
    if has_incumbent:
        return Diagnostic(
            category="timeout_with_incumbent",
            message="solver hit the time limit; returning the best incumbent found",
            details=details,
        )
    return Diagnostic(
        category="timeout_no_incumbent",
        message="solver hit the time limit before finding any solution",
        details=details,
    )


def checker_status_is_failure(status: str) -> bool:
    """True when a checker verdict is anything other than a clean pass.

    Clean is ``accepted`` (CP-SAT) or ``completed`` (MiniZinc); every other
    verdict — ``violation``, ``no_solution``, ``rejected``, ``error``,
    ``timeout`` — is a failure the enclosing result should surface as
    ``checker_failed``.
    """
    return status not in _CLEAN_CHECKER_STATUSES


def checker_diagnostic(
    checker_status: str, *, details: dict[str, JsonValue] | None = None
) -> Diagnostic | None:
    """Return a ``checker_failed`` Diagnostic for a non-clean verdict, else None.

    The verdict is always recorded under ``details["checker_status"]``; any
    caller-supplied ``details`` are merged on top (e.g. ``attempt_index``).
    """
    if not checker_status_is_failure(checker_status):
        return None
    merged: dict[str, JsonValue] = {"checker_status": checker_status}
    if details:
        merged.update(details)
    return Diagnostic(
        category="checker_failed",
        message=f"solution checker reported {checker_status!r}",
        details=merged,
    )


def wrapper_job_diagnostic_category(state: str) -> DiagnosticCategory | None:
    """Map a background-job state to its WRAPPER diagnostic category, or None.

    Returns ``cancelled``/``job_failed`` for the two non-result-bearing terminal
    states. Returns None for result-bearing terminal states — their diagnostic
    must be DERIVED from the embedded result (a result-derived
    ``timeout_with_incumbent`` beats a generic wrapper timeout) — and for the
    non-terminal ``queued``/``running`` states. Consumes
    ``RESULT_BEARING_STATES`` so the "derive from result instead" set stays
    defined in exactly one place.
    """
    if state in RESULT_BEARING_STATES:
        return None
    if state == "cancelled":
        return "cancelled"
    if state == "failed":
        return "job_failed"
    return None


def wrapper_job_diagnostic(
    state: str, *, message: str, details: dict[str, JsonValue] | None = None
) -> Diagnostic | None:
    """Build the wrapper diagnostic for a job state, or None when it must derive.

    Thin builder over :func:`wrapper_job_diagnostic_category`: None means the
    caller should derive the diagnostic from the embedded result (or there is
    none yet).
    """
    category = wrapper_job_diagnostic_category(state)
    if category is None:
        return None
    return Diagnostic(category=category, message=message, details=details)
