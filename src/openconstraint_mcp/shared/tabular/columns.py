"""Resolving a header name to the one column it names.

A dependency-light leaf, like ``limits``: ``style``, ``gantt``, and ``charts``
each name columns by header string and must refuse the same two cases
identically, so the rule lives here once rather than in three copies.
"""

from __future__ import annotations


def column_index(headers: list[str], name: str, role: str) -> int:
    """Return the single column named ``name``, or raise ``ValueError``.

    ``role`` names the caller's field (``"x_column"``, ``"style column"``, …)
    so the message points at the spec that has to change. A repeated header is
    ambiguous rather than resolved to the first match: this package preserves
    duplicate headers by design, so a name that appears twice picks no column.
    """
    matches: list[int] = [index for index, header in enumerate(headers) if header == name]
    if not matches:
        available: str = ", ".join(headers)
        raise ValueError(f"{role} {name!r} is not a header of this table; headers are: {available}")
    if len(matches) > 1:
        raise ValueError(
            f"{role} {name!r} is ambiguous: it names {len(matches)} columns "
            f"(duplicate headers are preserved, so a repeated name cannot pick one)"
        )
    return matches[0]
