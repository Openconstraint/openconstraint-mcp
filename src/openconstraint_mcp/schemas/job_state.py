from __future__ import annotations

from typing import Literal, cast

# A background job's lifecycle state, shared by the MiniZinc solve registry and
# the CP-SAT Python registry. `queued`/`running` are non-terminal;
# `succeeded`/`failed`/`timeout`/`cancelled` are terminal.
JobState = Literal["queued", "running", "succeeded", "failed", "timeout", "cancelled"]

# States with no further transitions.
TERMINAL_STATES: frozenset[JobState] = cast(
    "frozenset[JobState]", frozenset({"succeeded", "failed", "timeout", "cancelled"})
)

# The terminal states that carry a produced result. The load-bearing
# result-presence invariant: a job status has a `result` IFF its state is one of these
# (`result present ⇔ state ∈ {succeeded, timeout}`). For `failed` this is
# one-way only — `failed ⇒ result is None`, but `result is None` also holds for
# `queued`/`running`/`cancelled`.
RESULT_BEARING_STATES: frozenset[JobState] = cast(
    "frozenset[JobState]", frozenset({"succeeded", "timeout"})
)
