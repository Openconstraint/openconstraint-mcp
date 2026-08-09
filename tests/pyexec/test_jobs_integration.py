"""Integration tests for pyexec/jobs.py — real child processes.

Mirrors tests/test_jobs_integration.py for the MiniZinc job registry.
Tagged @pytest.mark.integration so they run only under ``just integration``.

Per AGENTS.md: "solver-flag/status changes need a real-binary integration
test" — the cancel kill must be asserted against a real child, not just
mocked argv.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from openconstraint_mcp.pyexec.jobs import CpsatJobRegistry


def _wait_until_terminal(registry: CpsatJobRegistry, job_id: str, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


_TRIVIAL_SOURCE = """
import json, sys
print(json.dumps({"status": "optimal", "objective": 42, "solution": {"x": 42}}))
"""

_SLEEP_SOURCE = """
import time, sys
sys.stdout.flush()
time.sleep(60)
print("done")
"""


@pytest.mark.integration
def test_submit_source_real_child_reaches_succeeded() -> None:
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_TRIVIAL_SOURCE)
        state = _wait_until_terminal(registry, job_id)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
        assert status.result.solution == {"x": 42}
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_cancel_running_real_child_terminates_and_reports_cancelled() -> None:
    """A real cancel kills the child process tree and finalizes as 'cancelled'."""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_SLEEP_SOURCE)
        # Wait for the child to start
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if registry.get(job_id).state == "running":
                break
            time.sleep(0.05)
        registry.cancel(job_id)
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_cancel_during_real_checker_child_reports_cancelled_without_result(
    tmp_path: Path,
) -> None:
    """Cancelling while the CHECKER child runs kills it and finalizes as
    'cancelled' with result=None — the completed solver result is discarded."""
    marker = tmp_path / "checker-started"
    checker = f"""
import time, pathlib
pathlib.Path({str(marker)!r}).write_text("started", encoding="utf-8")
time.sleep(60)
print('{{"status":"accepted","errors":[]}}')
"""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source(_TRIVIAL_SOURCE, checker=checker)
        # The solver child is trivial; the marker file appearing means the job
        # is now inside its checker phase (still state 'running').
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "checker child never started"
        assert registry.get(job_id).state == "running"
        registry.cancel(job_id)
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
        assert status.checker is None
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_submit_file_real_child_reaches_succeeded(tmp_path: Path) -> None:
    script = tmp_path / "sol.py"
    script.write_text(_TRIVIAL_SOURCE, encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script)
        state = _wait_until_terminal(registry, job_id)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
    finally:
        registry.shutdown()


_EXAMPLES = Path(__file__).parent.parent / "fixtures" / "cpsat_python" / "social_golfers_best"


@pytest.mark.integration
def test_submit_file_with_real_checker_reaches_optimal_and_accepted() -> None:
    """End-to-end: a real example's solver + checker, run as a background job."""
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(
            _EXAMPLES / "solution.py",
            # solution.py's own solver_time_limit_seconds defaults to 60s; keep our
            # subprocess cap and wait window comfortably above that so the
            # example's internal search cap can never trip our timeout first.
            script_timeout_ms=90_000,
            checker=(_EXAMPLES / "checker.py").read_text(encoding="utf-8"),
        )
        state = _wait_until_terminal(registry, job_id, timeout=120.0)
        assert state == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.result.status == "optimal"
        assert status.checker is not None
        assert status.checker.status == "accepted"
        assert status.checker.errors == []
    finally:
        registry.shutdown()


# --- checker_path: an on-disk checker run in its own directory ---------------

# Resolves `problem` as a BARE FILENAME next to itself — the whole point of
# `checker_path`. Run from a temp copy (inline `checker`) this cannot work.
_SIBLING_CHECKER = """
import json, pathlib, sys
payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
expected = json.loads(pathlib.Path(payload["problem"]).read_text(encoding="utf-8"))
errors = [] if payload["solution"] == expected else ["solution does not match the reference"]
print(json.dumps({"status": "rejected" if errors else "accepted", "errors": errors}))
"""

_SLOW_TRIVIAL_SOURCE = """
import json, time
time.sleep(2)
print(json.dumps({"status": "optimal", "objective": 42, "solution": {"x": 42}}))
"""


def _sibling_checker_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A solver script plus a checker whose reference data sits beside it."""
    script = tmp_path / "sol.py"
    script.write_text(_TRIVIAL_SOURCE, encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text(_SIBLING_CHECKER, encoding="utf-8")
    (tmp_path / "reference.json").write_text('{"x": 42}', encoding="utf-8")
    return script, checker


@pytest.mark.integration
def test_checker_path_job_resolves_a_sibling_reference_file(tmp_path: Path) -> None:
    """The feature test: a checker_path checker runs in place, so a bare-filename
    `problem` resolves next to it and the verdict is `accepted`."""
    script, checker = _sibling_checker_pair(tmp_path)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker, problem="reference.json")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.checker is not None
        assert status.checker.status == "accepted"
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_inline_checker_cannot_resolve_the_same_sibling_reference_file(tmp_path: Path) -> None:
    """The contrast that makes the test above meaningful: the identical checker
    source passed inline runs from a temp copy and cannot find the file."""
    script, checker = _sibling_checker_pair(tmp_path)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(
            script, checker=checker.read_text(encoding="utf-8"), problem="reference.json"
        )
        assert _wait_until_terminal(registry, job_id) == "succeeded"
        status = registry.get(job_id)
        assert status.checker is not None
        # Not merely "some error": `status == "error"` is also what a spawn
        # failure, a truncation, or a malformed verdict produces. Pin it to the
        # crash of a checker that RAN and could not open the sibling file.
        assert status.checker.errors == ["checker exited with non-zero code: 1"]
        assert "reference.json" in status.checker.stderr
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_cancel_during_real_checker_path_child_reports_cancelled_without_result(
    tmp_path: Path,
) -> None:
    """Cancelling while a checker_path CHECKER child runs kills it — the proof
    that the new branch forwards `on_start` so `record.handle` tracks it."""
    script = tmp_path / "sol.py"
    script.write_text(_TRIVIAL_SOURCE, encoding="utf-8")
    marker = tmp_path / "checker-started"
    checker = tmp_path / "checker.py"
    checker.write_text(
        f"""
import time, pathlib
pathlib.Path({str(marker)!r}).write_text("started", encoding="utf-8")
time.sleep(60)
print('{{"status":"accepted","errors":[]}}')
""",
        encoding="utf-8",
    )
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker)
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists(), "checker child never started"
        assert registry.get(job_id).state == "running"
        registry.cancel(job_id)
        assert _wait_until_terminal(registry, job_id) == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
        assert status.checker is None
    finally:
        registry.shutdown()


@pytest.mark.integration
def test_checker_path_deleted_after_admission_finalizes_with_an_error_report(
    tmp_path: Path,
) -> None:
    """A checker file that vanishes between admission and the checker phase must
    finalize the job with an `error` checker report, never hang in `running` —
    the regression guard for `run_checker_file`'s revalidation raising outside
    the worker's own try/except."""
    script = tmp_path / "sol.py"
    script.write_text(_SLOW_TRIVIAL_SOURCE, encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text(_SIBLING_CHECKER, encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker, problem="reference.json")
        # The solver child sleeps 2 s, so the checker phase has not started yet.
        checker.unlink()
        assert _wait_until_terminal(registry, job_id, timeout=30.0) == "succeeded"
        status = registry.get(job_id)
        assert status.result is not None
        assert status.checker is not None
        assert status.checker.status == "error"
    finally:
        registry.shutdown()
