"""Deterministic, domain-agnostic mutations for CP-SAT checker self-tests."""

from __future__ import annotations

import copy
from typing import Any, TypeGuard

from pydantic import BaseModel, ConfigDict

from ..schemas.cpsat import CpsatMutationName

# The fixed mutation names; `CPSAT_MUTATION_NAMES` carries their generation order.
OBJECTIVE_PERTURBED: CpsatMutationName = "objective_perturbed"
ELEMENT_DROPPED: CpsatMutationName = "element_dropped"
ELEMENT_DUPLICATED: CpsatMutationName = "element_duplicated"
NUMERIC_FIELD_PERTURBED: CpsatMutationName = "numeric_field_perturbed"

_NO_LIST_REASON = "no non-empty list among the solution's top-level values"


class SolutionMutation(BaseModel):
    """One applied or skipped ``(solution, objective)`` mutation.

    A skipped mutation has no usable payload.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    name: CpsatMutationName
    solution: dict[str, Any] | None = None
    objective: float | int | None = None
    skipped_reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.skipped_reason is None


def _is_plain_int(value: object) -> TypeGuard[int]:
    """True for an ``int`` other than ``bool``; booleans are flipped, not incremented."""
    return isinstance(value, int) and not isinstance(value, bool)


def _select_list_key(solution: dict[str, Any]) -> str | None:
    """Return the longest non-empty top-level list; ties break by sorted key."""
    best_key: str | None = None
    best_len = 0
    for key in sorted(solution):
        value = solution[key]
        if not isinstance(value, list) or not value:
            continue
        if len(value) > best_len:
            best_key, best_len = key, len(value)
    return best_key


def generate_mutations(
    solution: dict[str, Any] | None, objective: float | int | None
) -> list[SolutionMutation]:
    """Return four fixed-order mutations without mutating ``solution``.

    Deep-copying deeply nested input can raise ``RecursionError``.
    """
    original = solution or {}
    list_key = _select_list_key(original)
    return [
        _objective_perturbed(original, objective),
        _element_dropped(original, objective, list_key),
        _element_duplicated(original, objective, list_key),
        _numeric_field_perturbed(original, objective, list_key),
    ]


def _objective_perturbed(
    solution: dict[str, Any], objective: float | int | None
) -> SolutionMutation:
    # Core already normalizes this to ``None`` or a finite numeric objective.
    if objective is None:
        return SolutionMutation(
            name=OBJECTIVE_PERTURBED,
            skipped_reason="the run reported no objective",
        )
    perturbed = objective + 1
    # Skip a float mutation when ``+ 1`` leaves the payload unchanged.
    if perturbed == objective:
        return SolutionMutation(
            name=OBJECTIVE_PERTURBED,
            skipped_reason=(
                f"objective {objective!r} is too large for +1 to change its value, "
                "so the mutated payload would be identical to the accepted one"
            ),
        )
    return SolutionMutation(
        name=OBJECTIVE_PERTURBED,
        solution=copy.deepcopy(solution),
        objective=perturbed,
    )


def _element_dropped(
    solution: dict[str, Any], objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    if list_key is None:
        return SolutionMutation(name=ELEMENT_DROPPED, skipped_reason=_NO_LIST_REASON)
    mutated = copy.deepcopy(solution)
    mutated[list_key].pop()
    return SolutionMutation(name=ELEMENT_DROPPED, solution=mutated, objective=objective)


def _element_duplicated(
    solution: dict[str, Any], objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    if list_key is None:
        return SolutionMutation(name=ELEMENT_DUPLICATED, skipped_reason=_NO_LIST_REASON)
    mutated = copy.deepcopy(solution)
    mutated[list_key].append(copy.deepcopy(mutated[list_key][0]))
    return SolutionMutation(name=ELEMENT_DUPLICATED, solution=mutated, objective=objective)


class _NumericTarget(BaseModel):
    """Where ``_apply_numeric_target`` should mutate.

    ``list_key=None`` addresses the top-level solution and ``field`` is one of
    its own keys. Otherwise ``field`` is a dict key of ``solution[list_key][0]``
    when that element is a dict, or the literal index ``0`` into the list
    itself when the element is a bare scalar — the list then doubles as its
    own single-item container, so ``container[field]`` reaches the element
    either way without a separate scalar-vs-dict case at the call site.
    """

    model_config = ConfigDict(frozen=True, strict=True)

    list_key: str | None
    field: object


def _find_numeric_target(
    solution: dict[str, Any], list_key: str | None, *, want_bool: bool
) -> _NumericTarget | None:
    """Search the list head before the top level for a matching field.

    Looks for a plain int unless ``want_bool``, in which case it looks for a
    bool instead; ``_numeric_field_perturbed`` always exhausts the int search
    (both candidates) before trying a bool one, so a solution with any int
    anywhere is never perturbed by flipping an unrelated bool.
    """
    candidates: list[tuple[object, str | None]] = [(solution, None)]
    if list_key is not None:
        candidates.insert(0, (solution[list_key][0], list_key))

    matches = (lambda value: isinstance(value, bool)) if want_bool else _is_plain_int
    for candidate, candidate_list_key in candidates:
        fields = candidate.items() if isinstance(candidate, dict) else ((0, candidate),)
        for field, value in fields:
            if matches(value):
                return _NumericTarget(list_key=candidate_list_key, field=field)
    return None


def _apply_numeric_target(
    solution: dict[str, Any], target: _NumericTarget, *, flip: bool
) -> dict[str, Any]:
    """Deep-copy ``solution`` and bump (or, if ``flip``, negate) the targeted field.

    Re-resolves ``target`` against the fresh copy rather than mutating through
    a reference captured during the search, so the caller's ``solution`` is
    never touched.
    """
    mutated = copy.deepcopy(solution)
    container: Any
    if target.list_key is None:
        container = mutated
    elif isinstance(solution[target.list_key][0], dict):
        container = mutated[target.list_key][0]
    else:
        container = mutated[target.list_key]
    container[target.field] = not container[target.field] if flip else container[target.field] + 1
    return mutated


def _numeric_field_perturbed(
    solution: dict[str, Any], objective: float | int | None, list_key: str | None
) -> SolutionMutation:
    for want_bool in (False, True):
        target = _find_numeric_target(solution, list_key, want_bool=want_bool)
        if target is not None:
            mutated = _apply_numeric_target(solution, target, flip=want_bool)
            return SolutionMutation(
                name=NUMERIC_FIELD_PERTURBED, solution=mutated, objective=objective
            )

    return SolutionMutation(
        name=NUMERIC_FIELD_PERTURBED,
        skipped_reason=(
            "the solution has no top-level integer- or boolean-valued field"
            if list_key is None
            else (
                f"neither the first element of solution[{list_key!r}] nor the solution's "
                "top level yields an integer or boolean to perturb"
            )
        ),
    )
