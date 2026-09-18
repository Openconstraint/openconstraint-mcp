"""Real-runtime checks for the background solve-job registry.

These exercise the actual managed MiniZinc binary through ``JobRegistry``, so
they prove what the mocked unit tests cannot: that a submitted job runs the
real solver and returns the same ``SolveResult`` as a direct ``solve_model``,
and — critically — that cancelling a running job terminates the whole managed
process tree (MiniZinc *and* its solver children) without leaving an orphan.
Per repo policy, process-control/flag behavior gets a real-binary test because
mocks can only prove a signal was *requested*, not that the OS tore the tree
down. Marked ``integration`` and excluded from ``just check``; run with
``just integration`` on a machine where ``install-runtime`` placed a runtime.
"""

from __future__ import annotations

import os
import sys
import time

import pytest

from openconstraint_mcp.jobs.registry import JobRegistry
from openconstraint_mcp.minizinc.core import solve_model
from openconstraint_mcp.schemas.minizinc import SolveJobStatus

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("require_real_runtime")]

_TERMINAL_STATES = {"succeeded", "failed", "timeout", "cancelled"}

# Unique-optimum maximization (x forced to 5): deterministic status, objective,
# and solution, so a job's result can be compared field-for-field to a direct
# solve of the same source.
_DETERMINISTIC_MODEL = (
    'var 1..5: x;\nconstraint x > 2;\nsolve maximize x;\noutput ["x=\\(x)\\n"];\n'
)

# A Golomb ruler of order 13 (optimal length 106): proving optimality is hard
# enough that the solve reliably stays `running` for far longer than the test's
# capture window, so the cancel path always has a live child to terminate. The
# job carries its own bounded timeout as a backstop in case cancel ever fails.
_HARD_MODEL = (
    'include "globals.mzn";\n'
    "int: m = 13;\n"
    "int: n = m * m;\n"
    "array[1..m] of var 0..n: mark;\n"
    "array[int] of var 0..n: differences =\n"
    "    [ mark[j] - mark[i] | i in 1..m, j in i+1..m ];\n"
    "constraint mark[1] = 0;\n"
    "constraint forall(i in 1..m-1)(mark[i] < mark[i+1]);\n"
    "constraint alldifferent(differences);\n"
    "constraint forall(d in differences)(d > 0);\n"
    "solve minimize mark[m];\n"
)


def _wait_until_terminal(
    registry: JobRegistry, job_id: str, timeout: float = 60.0
) -> SolveJobStatus:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = registry.get(job_id)
        if status.state in _TERMINAL_STATES:
            return status
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


def test_submitted_job_result_matches_direct_solve() -> None:
    # The off-request job path must produce exactly the result the synchronous
    # path does on the same model — same verdict, objective, and solution.
    direct = solve_model(_DETERMINISTIC_MODEL)

    registry = JobRegistry()
    try:
        job_id = registry.submit(model=_DETERMINISTIC_MODEL)
        status = _wait_until_terminal(registry, job_id)

        assert status.state == "succeeded"
        assert status.result is not None
        job_result = status.result
        assert job_result.status == direct.status == "optimal"
        assert job_result.objective == direct.objective == 5
        assert job_result.solution == direct.solution == {"x": 5}
        assert job_result.solutions == direct.solutions
        assert job_result.timed_out is False
    finally:
        registry.shutdown()


def test_submitted_job_result_includes_solve_statistics() -> None:
    # A completed background job is the only place a client can read solve
    # statistics (a `running` job exposes none), so they MUST survive the
    # off-request path — the same `--statistics` vocabulary the synchronous solve
    # produces, not an empty dict.
    direct = solve_model(_DETERMINISTIC_MODEL)

    registry = JobRegistry()
    try:
        job_id = registry.submit(model=_DETERMINISTIC_MODEL)
        status = _wait_until_terminal(registry, job_id)
    finally:
        registry.shutdown()

    assert status.state == "succeeded"
    assert status.result is not None
    assert status.result.statistics
    assert direct.statistics.keys() & status.result.statistics.keys()


def _wait_for_running_child(registry: JobRegistry, job_id: str, timeout: float = 30.0) -> int:
    """Return the pgid of the live solve child once the worker has launched it.

    Reaches into registry internals (the live ``Popen`` handle) — the only seam
    that exposes the process group an orphan check needs. Because the runner
    starts a new session, the leader's pgid equals its pid and covers the
    forked solver children too.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with registry._lock:  # noqa: SLF001 - white-box: the handle is internal
            record = registry._records.get(job_id)
            handle = record.handle if record is not None else None
            state = record.state if record is not None else None
        if handle is not None and state == "running":
            try:
                return os.getpgid(handle.pid)
            except ProcessLookupError:
                # The child exited between snapshot and lookup; with _HARD_MODEL
                # this should not happen, but keep polling rather than flake.
                pass
        time.sleep(0.02)
    raise AssertionError("the solve child was never observed running")


def _assert_process_group_gone(pgid: int, timeout: float = 15.0) -> None:
    # `killpg(pgid, 0)` is an existence probe: it raises ProcessLookupError once
    # the group has no members. A SIGKILL'd solver child reparented to init
    # lingers as a zombie (keeping the group alive) until init reaps it, so poll
    # for the group to disappear rather than demanding it be instant.
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process group {pgid} still has members after {timeout}s (orphaned)")


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="orphan-free cancellation is proven via POSIX process groups",
)
def test_cancel_running_job_leaves_no_orphaned_process_tree() -> None:
    registry = JobRegistry()
    try:
        # A bounded job timeout backstops the test if cancel ever fails to stop
        # the (intentionally hard) solve.
        job_id = registry.submit(model=_HARD_MODEL, timeout_ms=120_000)
        pgid = _wait_for_running_child(registry, job_id)

        # The process group is alive while the solve runs.
        os.killpg(pgid, 0)

        registry.cancel(job_id)
        status = _wait_until_terminal(registry, job_id)

        assert status.state == "cancelled"
        assert status.result is None
        # The MiniZinc leader and its solver children are all gone — no orphan.
        _assert_process_group_gone(pgid)
    finally:
        registry.shutdown()
