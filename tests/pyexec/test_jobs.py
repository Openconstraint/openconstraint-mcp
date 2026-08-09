"""Unit tests for pyexec/jobs.py — executor mocked for speed."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from openconstraint_mcp.pyexec.jobs import CpsatJobRegistry
from openconstraint_mcp.schemas.cpsat import CpsatCheckerReport, CpsatPythonResult, CpsatStatus
from openconstraint_mcp.shared.childrun import ChildSpawnError
from openconstraint_mcp.shared.job_errors import JobRejectedError


def _cpsat_result(
    status: CpsatStatus = "optimal",
    *,
    timed_out: bool = False,
    solution: dict | None = None,
) -> CpsatPythonResult:
    return CpsatPythonResult(
        status=status,
        solution=solution or {"x": 1},
        objective=None,
        stdout="",
        stderr="",
        return_code=None if timed_out else 0,
        timed_out=timed_out,
        truncated=False,
        duration_ms=10,
    )


class _FakeProc:
    """Stand-in Popen handle passed through on_start/terminate.

    It has no ``pid`` and backs no real process, so the real (group-aware)
    ``_terminate_process_tree`` must never see it — the autouse
    ``_never_terminate_for_real`` fixture below guarantees that.

    Defaults to looking unreaped (``returncode = None``); set ``returncode = 0``
    to model a child the registry has already reaped.
    """

    returncode: Any = None

    def poll(self) -> Any:
        return self.returncode


@pytest.fixture(autouse=True)
def _never_terminate_for_real(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every proc in this file is a ``_FakeProc``; the real group-aware
    terminate would probe ``os.getpgid``/``os.killpg`` on it. Tests that
    assert termination re-patch a recorder over this via ``_patch_terminate``.
    """
    monkeypatch.setattr(
        "openconstraint_mcp.pyexec.jobs._terminate_process_tree",
        lambda proc, **kwargs: None,
    )


def _patch_run_source(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_cpsat_python", fake)


def _patch_run_file(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_cpsat_python_file", fake)


def _patch_terminate(monkeypatch: pytest.MonkeyPatch, recorder: list[Any]) -> None:
    def _fake(proc: Any, **_: Any) -> None:
        recorder.append(proc)

    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs._terminate_process_tree", _fake)


def _wait_until_terminal(registry: CpsatJobRegistry, job_id: str, timeout: float = 3.0) -> str:
    deadline = time.monotonic() + timeout
    terminal = {"succeeded", "failed", "timeout", "cancelled"}
    while time.monotonic() < deadline:
        state = registry.get(job_id).state
        if state in terminal:
            return state
        time.sleep(0.005)
    raise AssertionError(f"job {job_id} did not reach a terminal state within {timeout}s")


# --- submit_source happy path -----------------------------------------------


def test_submit_source_returns_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("print('x')")
        assert isinstance(job_id, str) and job_id
    finally:
        registry.shutdown()


def test_submit_source_reaches_succeeded_with_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result("optimal"))
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("print('x')")
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.state == "succeeded"
        assert status.result is not None
        assert status.result.status == "optimal"
    finally:
        registry.shutdown()


def test_submit_source_error_status_yields_succeeded_job(monkeypatch: pytest.MonkeyPatch) -> None:
    # D3: error → succeeded (a structured verdict, not a job-machinery failure).
    _patch_run_source(
        monkeypatch,
        lambda source, *, on_start, **kw: _cpsat_result("error", solution=None),
    )
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("raise ValueError('boom')")
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.state == "succeeded"
        assert status.result is not None
        assert status.result.status == "error"
    finally:
        registry.shutdown()


@pytest.mark.parametrize("job_kind", ["source", "file"])
def test_spawn_failure_reaches_failed_without_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job_kind: str
) -> None:
    def _fail_spawn(*args: Any, **kwargs: Any) -> None:
        raise ChildSpawnError(24, "Too many open files")

    monkeypatch.setattr("openconstraint_mcp.pyexec.core.execute_child", _fail_spawn)
    registry = CpsatJobRegistry()
    try:
        if job_kind == "source":
            job_id = registry.submit_source("print('x')")
        else:
            script = tmp_path / "model.py"
            script.write_text("print('x')", encoding="utf-8")
            job_id = registry.submit_file(script)
        assert _wait_until_terminal(registry, job_id) == "failed"
        status = registry.get(job_id)
        assert status.result is None
        assert status.message is not None
        assert "Too many open files" in status.message
    finally:
        registry.shutdown()


def test_submit_source_echoes_timeout_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1", script_timeout_ms=45000)
        assert registry.get(job_id).script_timeout_ms == 45000
    finally:
        registry.shutdown()


def test_submit_source_clears_cpsat_protocol_env_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _fake(source: str, *, on_start: Any, **kw: object) -> CpsatPythonResult:
        seen["env"] = kw.get("env")
        return _cpsat_result()

    _patch_run_source(monkeypatch, _fake)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1")
        _wait_until_terminal(registry, job_id)
        assert seen["env"] == {
            "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
            "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
        }
    finally:
        registry.shutdown()


# --- submit_file happy path -------------------------------------------------


def test_submit_file_reaches_succeeded_with_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    _patch_run_file(
        monkeypatch,
        lambda path, *, on_start, **kw: _cpsat_result("feasible"),
    )
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script)
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.state == "succeeded"
        assert status.result is not None
        assert status.result.status == "feasible"
    finally:
        registry.shutdown()


def test_submit_file_clears_cpsat_protocol_env_vars(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    seen: dict[str, object] = {}

    def _fake(path: Path, *, on_start: Any, **kw: object) -> CpsatPythonResult:
        seen["env"] = kw.get("env")
        return _cpsat_result()

    _patch_run_file(monkeypatch, _fake)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script)
        _wait_until_terminal(registry, job_id)
        assert seen["env"] == {
            "OPENCONSTRAINT_MCP_CPSAT_SEED": None,
            "OPENCONSTRAINT_MCP_CPSAT_CONFIG": None,
        }
    finally:
        registry.shutdown()


def test_submit_file_forwards_args_to_the_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    seen: dict[str, object] = {}

    def _fake(path: Path, *, on_start: Any, **kw: object) -> CpsatPythonResult:
        seen["args"] = kw.get("args")
        return _cpsat_result()

    _patch_run_file(monkeypatch, _fake)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, args=["data_ft10.json"])
        _wait_until_terminal(registry, job_id)
        assert seen["args"] == ["data_ft10.json"]
    finally:
        registry.shutdown()


def test_submit_file_args_are_snapshotted_against_caller_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A queued job's argv must not stay live-linked to the caller's list: the
    # values supplied at submission are what runs, however the caller mutates
    # its list while the job waits for a slot.
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    release = threading.Event()
    seen: list[object] = []

    def _fake(path: Path, *, on_start: Any, **kw: object) -> CpsatPythonResult:
        if kw.get("args") is None:
            release.wait(3.0)
        else:
            seen.append(kw.get("args"))
        return _cpsat_result()

    _patch_run_file(monkeypatch, _fake)
    registry = CpsatJobRegistry(max_running_jobs=1)
    try:
        blocker_id = registry.submit_file(script)
        caller_args = ["data_ft10.json"]
        queued_id = registry.submit_file(script, args=caller_args)
        assert registry.get(queued_id).state == "queued"  # precondition for the mutation below
        caller_args.append("injected")
        release.set()
        _wait_until_terminal(registry, blocker_id)
        _wait_until_terminal(registry, queued_id)
        assert seen == [["data_ft10.json"]]
    finally:
        release.set()
        registry.shutdown()


# --- path validation before admission ---------------------------------------


def test_submit_file_rejects_missing_path_before_creating_job(tmp_path: Path) -> None:
    missing = tmp_path / "nope.py"
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="does not exist"):
            registry.submit_file(missing)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_nul_arg_before_creating_job(tmp_path: Path) -> None:
    """A NUL arg fails the submit call, not the spawn of an already-admitted job."""
    script = tmp_path / "model.py"
    script.write_text("print('x')", encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match=r"args\[1\] contains a NUL character"):
            registry.submit_file(script, args=["data.json", "bad\0arg"])
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_empty_script_before_creating_job(tmp_path: Path) -> None:
    empty = tmp_path / "empty.py"
    empty.write_text("   \n", encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="empty"):
            registry.submit_file(empty)
        assert registry.list() == []
    finally:
        registry.shutdown()


# --- admission bounds -------------------------------------------------------


def test_queue_overflow_raises_job_rejected_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # 1 running slot, 0 queued → second submit is rejected immediately.
    block = {"wait": True}
    released: list[None] = []

    def _blocking_run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(_FakeProc())
        while block["wait"]:
            time.sleep(0.005)
        released.append(None)
        return _cpsat_result()

    _patch_run_source(monkeypatch, _blocking_run)
    registry = CpsatJobRegistry(max_running_jobs=1, max_queued_jobs=0)
    try:
        registry.submit_source("print('a')")
        time.sleep(0.05)  # let the worker start
        with pytest.raises(JobRejectedError):
            registry.submit_source("print('b')")
    finally:
        block["wait"] = False
        registry.shutdown()


# --- cancel running job → cancelled, NOT succeeded (D4) --------------------


def test_cancel_running_job_finalizes_as_cancelled_not_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled running job must finalize as 'cancelled', not 'succeeded'.

    When the child is killed mid-run, _execute_cpsat returns an error result
    (nonzero exit). Without the cancel_requested check in _run_job, D3 would
    map that to 'succeeded'. The check must override it.
    """
    running_event = threading.Event()
    cancel_event = threading.Event()

    def _slow_run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        proc = _FakeProc()
        on_start(proc)
        running_event.set()
        cancel_event.wait(timeout=3.0)
        return _cpsat_result("error", solution=None)  # simulates kill → error

    _patch_run_source(monkeypatch, _slow_run)
    killed: list[Any] = []
    _patch_terminate(monkeypatch, killed)

    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("time.sleep(60)")
        running_event.wait(timeout=3.0)
        registry.cancel(job_id)
        cancel_event.set()
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
    finally:
        cancel_event.set()
        registry.shutdown()


# --- cancel before start → cancelled ----------------------------------------


def test_cancel_queued_job_before_start(monkeypatch: pytest.MonkeyPatch) -> None:
    block: dict[str, bool] = {"wait": True}

    def _slow(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(_FakeProc())
        while block["wait"]:
            time.sleep(0.005)
        return _cpsat_result()

    _patch_run_source(monkeypatch, _slow)
    registry = CpsatJobRegistry(max_running_jobs=1, max_queued_jobs=4)
    try:
        _first = registry.submit_source("x=1")
        time.sleep(0.05)  # let worker start
        second = registry.submit_source("y=2")  # queued
        registry.cancel(second)
        state = _wait_until_terminal(registry, second)
        assert state == "cancelled"
    finally:
        block["wait"] = False
        registry.shutdown()


# --- terminal eviction ------------------------------------------------------


def test_terminal_eviction_respects_max_retained(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result())
    registry = CpsatJobRegistry(max_retained_terminal=2, max_running_jobs=4)
    try:
        for i in range(4):
            registry.submit_source(f"x={i}")
        # Poll until the registry stabilizes below or at the cap.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if len(registry.list()) <= 2:
                break
            time.sleep(0.01)
        assert len(registry.list()) <= 2
    finally:
        registry.shutdown()


# --- shutdown terminates live children --------------------------------------


def test_shutdown_terminates_running_child(monkeypatch: pytest.MonkeyPatch) -> None:
    running_event = threading.Event()
    stop_event = threading.Event()

    def _blocking_run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(_FakeProc())
        running_event.set()
        stop_event.wait(timeout=5.0)
        return _cpsat_result()

    _patch_run_source(monkeypatch, _blocking_run)
    killed: list[Any] = []

    # The real terminator kills the child, which unblocks its run; model that so
    # the executor join inside shutdown() returns immediately instead of waiting
    # out the worker's 5s stop_event timeout.
    def _kill(proc: Any, **_: Any) -> None:
        killed.append(proc)
        stop_event.set()

    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs._terminate_process_tree", _kill)

    registry = CpsatJobRegistry()
    registry.submit_source("time.sleep(60)")
    running_event.wait(timeout=3.0)
    registry.shutdown()
    assert len(killed) >= 1


def test_shutdown_retries_a_terminal_unreaped_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # A job can finalize (terminal) while its leader survived termination and was
    # never reaped (returncode None). shutdown must still terminate that handle,
    # not skip it just because the record is terminal.
    handle = _FakeProc()
    handle.returncode = None  # type: ignore[attr-defined]
    terminated: list[Any] = []

    def _unreaped_run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(handle)
        return _cpsat_result()

    _patch_run_source(monkeypatch, _unreaped_run)
    _patch_terminate(monkeypatch, terminated)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1")
        assert _wait_until_terminal(registry, job_id) == "succeeded"
    finally:
        registry.shutdown()

    assert terminated == [handle]


def test_shutdown_terminates_an_evicted_unreaped_child(monkeypatch: pytest.MonkeyPatch) -> None:
    # Evicting a terminal record that still owns an unreaped leader must not drop
    # the only reference to that child: the handle is kept for the shutdown sweep.
    unreaped = _FakeProc()
    unreaped.returncode = None  # type: ignore[attr-defined]
    reaped = _FakeProc()
    reaped.returncode = 0  # type: ignore[attr-defined]
    handles = iter([unreaped, reaped])
    terminated: list[Any] = []

    def _run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(next(handles))
        return _cpsat_result()

    _patch_run_source(monkeypatch, _run)
    _patch_terminate(monkeypatch, terminated)
    # max_running=1 forces sequential completion so "oldest terminal" is the first
    # job; cap=1 evicts it the moment the second finalizes.
    registry = CpsatJobRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=1)
    try:
        first = registry.submit_source("x=0")
        assert _wait_until_terminal(registry, first) == "succeeded"
        second = registry.submit_source("x=1")
        assert _wait_until_terminal(registry, second) == "succeeded"
        with pytest.raises(ValueError, match="unknown job_id"):
            registry.get(first)  # evicted
    finally:
        registry.shutdown()

    # first's unreaped handle was kept and swept; second's reaped handle was not.
    assert terminated == [unreaped]


def test_eviction_reaps_a_leader_that_exited_before_eviction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A leader can exit on its own between finalize and eviction (eviction may run
    # long after the job that owns it finished, once the retention cap is next
    # exceeded). Eviction must poll() to reap it there rather than trusting the
    # stale post-finalize returncode and stashing an already-dead handle.
    class _LateExitProc(_FakeProc):
        def __init__(self) -> None:
            self.returncode = None

        def poll(self) -> Any:
            self.returncode = 0  # exits the moment eviction checks it
            return self.returncode

    late_exit = _LateExitProc()
    reaped = _FakeProc()
    reaped.returncode = 0  # type: ignore[attr-defined]
    handles = iter([late_exit, reaped])
    terminated: list[Any] = []

    def _run(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(next(handles))
        return _cpsat_result()

    _patch_run_source(monkeypatch, _run)
    _patch_terminate(monkeypatch, terminated)
    registry = CpsatJobRegistry(max_running_jobs=1, max_queued_jobs=8, max_retained_terminal=1)
    try:
        first = registry.submit_source("x=0")
        assert _wait_until_terminal(registry, first) == "succeeded"
        second = registry.submit_source("x=1")
        assert _wait_until_terminal(registry, second) == "succeeded"
    finally:
        registry.shutdown()

    # Reaped at eviction time, so it was never stashed for the shutdown sweep.
    assert terminated == []


# --- get/list reflect the job -----------------------------------------------


def test_list_reflects_submitted_job(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1")
        _wait_until_terminal(registry, job_id)
        statuses = registry.list()
        assert any(s.job_id == job_id for s in statuses)
    finally:
        registry.shutdown()


def test_get_unknown_job_id_raises_value_error(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="unknown job_id"):
            registry.get("no-such-id")
    finally:
        registry.shutdown()


def test_timeout_result_yields_timeout_job_state(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(
        monkeypatch,
        lambda source, *, on_start, **kw: _cpsat_result("timeout", timed_out=True, solution=None),
    )
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1")
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.state == "timeout"
        assert status.result is not None
        assert status.result.timed_out is True
    finally:
        registry.shutdown()


# --- optional diagnostic checker ---------------------------------------------

_CHECKER = "import sys, json; print(json.dumps({'status':'accepted','errors':[]}))"


def _checker_report(
    status: str = "accepted", errors: list[str] | None = None
) -> CpsatCheckerReport:
    return CpsatCheckerReport(
        status=status,  # type: ignore[arg-type]
        errors=errors or [],
        stdout="",
        stderr="",
        duration_ms=5,
        timed_out=status == "timeout",
        truncated=False,
    )


def _patch_run_checker(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_checker", fake)


def _patch_run_checker_file(monkeypatch: pytest.MonkeyPatch, fake: Any) -> None:
    monkeypatch.setattr("openconstraint_mcp.pyexec.jobs.run_checker_file", fake)


def _run_checked_job(
    monkeypatch: pytest.MonkeyPatch,
    *,
    solver_result: CpsatPythonResult,
    checker_behavior: Any,
    **submit_kwargs: Any,
) -> Any:
    """Submit one checked inline job, wait for terminal, return its status."""
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: solver_result)
    _patch_run_checker(monkeypatch, checker_behavior)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1", checker=_CHECKER, **submit_kwargs)
        _wait_until_terminal(registry, job_id)
        return registry.get(job_id)
    finally:
        registry.shutdown()


@pytest.mark.parametrize("checker_status", ["accepted", "rejected", "error", "timeout"])
def test_checked_job_attaches_checker_report(
    monkeypatch: pytest.MonkeyPatch, checker_status: str
) -> None:
    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("optimal"),
        checker_behavior=lambda *a, **kw: _checker_report(checker_status),
    )
    assert status.state == "succeeded"
    assert status.result is not None
    assert status.checker is not None
    assert status.checker.status == checker_status
    assert status.checker_skipped_reason is None


def test_unchecked_job_has_no_checker_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1")
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.checker is None
        assert status.checker_skipped_reason is None
        assert status.checker_timeout_ms is None
    finally:
        registry.shutdown()


def test_checker_skipped_for_ineligible_result_sets_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []

    def _never(*a: Any, **kw: Any) -> CpsatCheckerReport:
        calls.append(None)
        return _checker_report()

    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("infeasible", solution=None),
        checker_behavior=_never,
    )
    assert status.state == "succeeded"
    assert status.checker is None
    assert status.checker_skipped_reason == "status='infeasible'"
    assert calls == []


def test_checker_skipped_for_empty_solution_sets_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    empty = CpsatPythonResult(
        status="feasible",
        solution={},
        objective=None,
        stdout="",
        stderr="",
        return_code=0,
        timed_out=False,
        truncated=False,
        duration_ms=10,
    )
    status = _run_checked_job(
        monkeypatch,
        solver_result=empty,
        checker_behavior=lambda *a, **kw: _checker_report(),
    )
    assert status.checker is None
    assert status.checker_skipped_reason == "solution is missing or empty"


def test_timeout_incumbent_is_checked_and_job_stays_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("timeout", timed_out=True, solution={"x": 2}),
        checker_behavior=lambda *a, **kw: _checker_report("accepted"),
    )
    assert status.state == "timeout"
    assert status.result is not None
    assert status.checker is not None
    assert status.checker.status == "accepted"


def test_checker_infrastructure_exception_preserves_solver_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*a: Any, **kw: Any) -> CpsatCheckerReport:
        raise OSError("tempdir vanished")

    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("optimal"),
        checker_behavior=_boom,
    )
    assert status.state == "succeeded"
    assert status.result is not None
    assert status.result.status == "optimal"
    assert status.checker is not None
    assert status.checker.status == "error"
    assert status.checker.diagnostic is not None
    assert status.checker.diagnostic.category == "checker_failed"
    assert any("tempdir vanished" in e for e in status.checker.errors)


def test_checker_receives_problem_and_effective_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def _spy(checker: str, result: CpsatPythonResult, **kw: Any) -> CpsatCheckerReport:
        seen.update(kw, checker=checker)
        return _checker_report()

    _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("optimal"),
        checker_behavior=_spy,
        problem="pack the boxes",
        checker_timeout_ms=7000,
        script_timeout_ms=45000,
    )
    assert seen["checker"] == _CHECKER
    assert seen["problem"] == "pack the boxes"
    assert seen["timeout_ms"] == 7000


def test_checker_timeout_ms_echo_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("optimal"),
        checker_behavior=lambda *a, **kw: _checker_report(),
        checker_timeout_ms=7000,
        script_timeout_ms=45000,
    )
    assert status.checker_timeout_ms == 7000


def test_checker_timeout_ms_echo_defaults_to_timeout_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    status = _run_checked_job(
        monkeypatch,
        solver_result=_cpsat_result("optimal"),
        checker_behavior=lambda *a, **kw: _checker_report(),
        script_timeout_ms=45000,
    )
    assert status.checker_timeout_ms == 45000


def test_submit_source_rejects_checker_timeout_without_checker() -> None:
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="without checker"):
            registry.submit_source("x=1", checker_timeout_ms=5000)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_source_rejects_empty_checker() -> None:
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="non-empty"):
            registry.submit_source("x=1", checker="   ")
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_non_positive_checker_timeout(tmp_path: Path) -> None:
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="positive"):
            registry.submit_file(script, checker=_CHECKER, checker_timeout_ms=0)
        assert registry.list() == []
    finally:
        registry.shutdown()


# --- checker_path (on-disk checker, file jobs only) --------------------------


def _checker_pair(tmp_path: Path) -> tuple[Path, Path]:
    """A valid solver script and a valid on-disk checker beside it."""
    script = tmp_path / "sol.py"
    script.write_text("print('x')", encoding="utf-8")
    checker = tmp_path / "checker.py"
    checker.write_text(_CHECKER, encoding="utf-8")
    return script, checker


def test_submit_file_rejects_both_checker_forms_before_creating_job(tmp_path: Path) -> None:
    script, checker = _checker_pair(tmp_path)
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="mutually exclusive"):
            registry.submit_file(script, checker=_CHECKER, checker_path=checker)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_missing_checker_path_before_creating_job(tmp_path: Path) -> None:
    script, _ = _checker_pair(tmp_path)
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="checker_path does not exist"):
            registry.submit_file(script, checker_path=tmp_path / "nope.py")
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_non_file_checker_path_before_creating_job(tmp_path: Path) -> None:
    script, _ = _checker_pair(tmp_path)
    directory = tmp_path / "checkers"
    directory.mkdir()
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="checker_path is not a file"):
            registry.submit_file(script, checker_path=directory)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_empty_checker_path_before_creating_job(tmp_path: Path) -> None:
    script, checker = _checker_pair(tmp_path)
    checker.write_text("   \n", encoding="utf-8")
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="checker_path file is empty"):
            registry.submit_file(script, checker_path=checker)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_rejects_non_utf8_checker_path_before_creating_job(tmp_path: Path) -> None:
    script, checker = _checker_pair(tmp_path)
    checker.write_bytes(b"print('\xff\xfe')")
    registry = CpsatJobRegistry()
    try:
        with pytest.raises(ValueError, match="checker_path is not valid UTF-8"):
            registry.submit_file(script, checker_path=checker)
        assert registry.list() == []
    finally:
        registry.shutdown()


def test_submit_file_snapshots_the_resolved_checker_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A relative checker_path is resolved at admission, so a cwd change while
    the job is still solving cannot redirect which file the checker runs."""
    script, checker = _checker_pair(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    release = threading.Event()
    seen: list[Path] = []

    def _blocking_run(path: Path, *, on_start: Any, **kw: object) -> CpsatPythonResult:
        release.wait(3.0)
        return _cpsat_result()

    def _spy(checker_path: Path, result: CpsatPythonResult, **kw: Any) -> CpsatCheckerReport:
        seen.append(checker_path)
        return _checker_report()

    _patch_run_file(monkeypatch, _blocking_run)
    _patch_run_checker_file(monkeypatch, _spy)
    monkeypatch.chdir(tmp_path)
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=Path("checker.py"))
        monkeypatch.chdir(elsewhere)
        release.set()
        _wait_until_terminal(registry, job_id)
        assert seen == [checker.resolve()]
    finally:
        release.set()
        registry.shutdown()


def test_checker_path_job_echoes_a_non_none_effective_checker_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression guard: `effective_checker_timeout_ms` must treat a
    checker_path job as checked, or `_run_checker_phase`'s assertion fires and
    the job never finalizes."""
    script, checker = _checker_pair(tmp_path)
    _patch_run_file(monkeypatch, lambda path, *, on_start, **kw: _cpsat_result())
    _patch_run_checker_file(monkeypatch, lambda *a, **kw: _checker_report())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker, script_timeout_ms=45000)
        _wait_until_terminal(registry, job_id)
        assert registry.get(job_id).checker_timeout_ms == 45000
    finally:
        registry.shutdown()


def test_checker_path_job_attaches_the_checker_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    script, checker = _checker_pair(tmp_path)
    _patch_run_file(monkeypatch, lambda path, *, on_start, **kw: _cpsat_result())
    _patch_run_checker_file(monkeypatch, lambda *a, **kw: _checker_report("rejected"))
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker)
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.checker is not None
        assert status.checker.status == "rejected"
    finally:
        registry.shutdown()


def test_checker_path_job_skips_the_checker_for_an_ineligible_result(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The eligibility/skip rule is the same for both checker forms."""
    script, checker = _checker_pair(tmp_path)
    _patch_run_file(monkeypatch, lambda path, *, on_start, **kw: _cpsat_result("infeasible"))
    _patch_run_checker_file(monkeypatch, lambda *a, **kw: _checker_report())
    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_file(script, checker_path=checker)
        _wait_until_terminal(registry, job_id)
        status = registry.get(job_id)
        assert status.checker_skipped_reason == "status='infeasible'"
    finally:
        registry.shutdown()


def test_cancel_during_checker_finalizes_cancelled_and_discards_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel landing in the checker phase wins over the checker report AND
    discards the already-completed solver result (the registry's cancelled
    invariant: cancelled never carries a result)."""
    checker_running = threading.Event()
    cancel_done = threading.Event()

    _patch_run_source(monkeypatch, lambda source, *, on_start, **kw: _cpsat_result("optimal"))

    def _blocking_checker(*a: Any, **kw: Any) -> CpsatCheckerReport:
        on_start = kw.get("on_start")
        if on_start is not None:
            on_start(_FakeProc())
        checker_running.set()
        cancel_done.wait(timeout=3.0)
        return _checker_report("error")  # simulates kill mid-check

    _patch_run_checker(monkeypatch, _blocking_checker)
    killed: list[Any] = []
    _patch_terminate(monkeypatch, killed)

    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1", checker=_CHECKER)
        assert checker_running.wait(timeout=3.0)
        registry.cancel(job_id)
        cancel_done.set()
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        status = registry.get(job_id)
        assert status.result is None
        assert status.checker is None
        assert status.checker_skipped_reason is None
    finally:
        cancel_done.set()
        registry.shutdown()


def test_cancel_requested_before_checker_skips_checker_entirely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancel observed after the solver but before the checker starts must
    finalize as cancelled without ever spawning the checker child."""
    solver_running = threading.Event()
    cancel_done = threading.Event()
    checker_calls: list[None] = []

    def _slow_solver(source: str, *, on_start: Any, **kw: Any) -> CpsatPythonResult:
        on_start(_FakeProc())
        solver_running.set()
        cancel_done.wait(timeout=3.0)
        return _cpsat_result("optimal")

    def _never_checker(*a: Any, **kw: Any) -> CpsatCheckerReport:
        checker_calls.append(None)
        return _checker_report()

    _patch_run_source(monkeypatch, _slow_solver)
    _patch_run_checker(monkeypatch, _never_checker)
    killed: list[Any] = []
    _patch_terminate(monkeypatch, killed)

    registry = CpsatJobRegistry()
    try:
        job_id = registry.submit_source("x=1", checker=_CHECKER)
        assert solver_running.wait(timeout=3.0)
        registry.cancel(job_id)
        cancel_done.set()
        state = _wait_until_terminal(registry, job_id)
        assert state == "cancelled"
        assert checker_calls == []
    finally:
        cancel_done.set()
        registry.shutdown()
