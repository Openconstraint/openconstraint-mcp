"""Protocol-agnostic capped child-process executor.

The shared timeout / output-cap / process-tree-kill run loop, used by every
managed child the server spawns: the CP-SAT runner and checker
(``pyexec``) and the MiniZinc runners (``minizinc``). It lives in ``shared``
so both subtrees can import it without coupling through the orchestrator or
crossing the ``pyexec``/``minizinc`` boundary.

This module knows nothing about any result protocol. ``execute_child`` returns
a raw ``ChildExecutionResult`` (stdout/stderr/return_code plus the
timeout/truncation flags and wall-clock duration); each caller parses that raw
output into its own contract — ``pyexec.core`` into ``CpsatPythonResult``,
``pyexec.checker`` into ``CpsatCheckerReport``, ``minizinc.core`` into its
``_RunOutcome`` and the solve/check/inspect/unsat-core results.

Imports only: ``proc`` (process-group launch + tree-kill) and ``childproc``
(``ChildProcessTracker`` type). Never imports ``schemas``, ``pyexec``,
``minizinc``, or ``runtime``.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from subprocess import Popen

from .childproc import ChildProcessTracker
from .proc import popen_process_group, terminate_process_tree

MAX_OUTPUT_BYTES: int = 1 * 1024 * 1024  # 1 MiB

_POLL_INTERVAL_S: float = 0.05


class ChildSpawnError(OSError):
    """The child could NEVER be launched — raised only from the ``Popen`` call.

    ``execute_child`` can raise ``OSError`` from several points, and they mean
    opposite things. Before the spawn there is no child (``E2BIG`` from an
    oversized argv, ``EMFILE`` from fd exhaustion, ``ENOMEM``); after it, a
    child exists and MAY STILL BE ALIVE — ``tracker.register`` on a closed
    tracker tree-kills and can surface a transient error, a caller's
    ``on_start`` hook can fail, and the ``finally``'s
    ``terminate_process_tree`` can raise from the Windows ``proc.terminate()``
    fallback. A caller that catches bare ``OSError`` cannot tell the two apart
    and would report a live child as a zero-duration launch failure.

    This type marks ONLY the first case, so callers normalize a genuine
    never-started child into their own error contract while a post-launch
    failure keeps propagating and stays visible.

    It subclasses ``OSError`` deliberately: ``minizinc.core`` catches ``OSError``
    around its launch and must keep catching this unchanged.
    """


def _as_spawn_error(exc: OSError) -> ChildSpawnError:
    """Re-key a launch ``OSError`` as ``ChildSpawnError``, preserving its detail.

    Message-only errors keep their original args. Structured errors carry over
    ``errno``/``strerror``/filenames and Windows ``winerror``, so ``str()``
    renders identically and callers can still branch on the platform error code.
    """
    if exc.errno is None:
        return ChildSpawnError(*exc.args)
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return ChildSpawnError(exc.errno, exc.strerror, exc.filename, winerror, exc.filename2)
    spawn_error = ChildSpawnError(exc.errno, exc.strerror)
    if exc.filename is not None:
        spawn_error.filename = exc.filename
    if exc.filename2 is not None:
        spawn_error.filename2 = exc.filename2
    return spawn_error


@dataclass(frozen=True)
class ChildExecutionResult:
    """Raw outcome of one child-process run, with no protocol parsing applied.

    When non-``None``, ``return_code`` is the child's actual exit status (so a
    killed child reports its signal-derived negative code, e.g. ``-15`` for
    SIGTERM on POSIX). ``None`` means termination could not reap the leader; when
    a tracker is supplied, that handle stays registered for teardown to retry.
    Callers that need a contract-level value map it themselves. ``timed_out`` is
    set when the deadline elapsed while the child was last observed running — it
    classifies that branch and does not prove the termination signal caused the exit.
    ``truncated`` means the
    combined output overran ``MAX_OUTPUT_BYTES`` — usually via a tree-kill, but a
    burst writer can overrun the cap and exit on its own (with ANY return code)
    before the poll loop sees it. ``truncation_killed`` is set when the poll
    loop's cap check REQUESTED process-tree termination while the child had most
    recently been observed running — the branch under which callers may treat the
    exit code as the executor's artifact and mask it. It does NOT prove the
    termination signal caused the exit (see the KNOWN, ACCEPTED residual race at
    the cap branch): the child may have exited on its own in the gap before
    ``terminate_process_tree`` looked at it. A burst writer that overran the cap
    and exited BEFORE the poll loop observed it keeps ``truncation_killed`` False
    and its genuine return code — that verdict is the child's and must survive.
    ``stdout``/``stderr`` are already capped so their combined size never exceeds
    ``MAX_OUTPUT_BYTES`` (stdout is read first; stderr gets the remaining budget).
    """

    stdout: str
    stderr: str
    return_code: int | None
    timed_out: bool
    truncated: bool
    duration_ms: int
    truncation_killed: bool = False


def validate_timeout_ms(timeout_ms: int, *, label: str = "timeout_ms") -> None:
    """Reject a non-positive child timeout.

    ``label`` names the caller's own parameter in the message, so a CP-SAT tool
    (whose cap is ``script_timeout_ms``) does not report a name its signature no
    longer has. Mirrors ``validate_cpsat_random_seed``'s ``label``.
    """
    if timeout_ms <= 0:
        raise ValueError(f"{label} must be positive")


def _read_capped(path: Path, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, int]:
    """Return capped text plus the file's byte length (from ``stat``, pre-cap).

    Reads at most ``limit`` bytes from the file, so a child that overran the
    cap between poll checks cannot force the parent to materialize the whole file
    in memory: the cap is applied at the read boundary, not after a full slurp.
    The pre-cap length comes from ``stat`` because the truncation flag needs the
    true on-disk size, which the capped read no longer reflects.
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            data = f.read(limit)
    except OSError:
        return "", 0
    return data.decode("utf-8", errors="replace"), size


def execute_child(
    argv: list[str],
    cwd: Path,
    *,
    timeout_ms: int,
    tracker: ChildProcessTracker | None,
    on_start: Callable[[Popen[str]], None] | None = None,
    env: dict[str, str | None] | None = None,
) -> ChildExecutionResult:
    """Run a prepared child command, capturing bounded output.

    The shared core for every managed-child caller (the CP-SAT ``core``/``checker``
    entry points and the MiniZinc runners). Handles the timeout / output-cap /
    tree-kill run loop and returns the raw process result; protocol parsing is the
    caller's job. Raises ``ValueError`` on a non-positive ``timeout_ms`` — the
    single gate, so a zero/negative cap is rejected before any child is spawned.

    ``tracker`` wiring is handled here: the child is registered after launch and
    unregistered only after its leader is reaped. A leader that survives
    termination stays registered so lifespan teardown can retry.

    ``env`` is an optional overlay merged ON TOP of the parent's ``os.environ``
    (the child still inherits the server's full environment, with these keys
    overriding/adding). The seeded save replay uses it to inject
    ``OPENCONSTRAINT_MCP_CPSAT_SEED``. A key mapped to ``None`` is explicitly
    deleted from the inherited environment instead of being left alone — this is
    what lets a caller force-clear a protocol var the *parent* process happens to
    have inherited from its own launch environment, rather than silently letting
    it pass through to the child. ``env=None`` (as opposed to a dict with ``None``
    values) leaves the inherited environment completely untouched (the default
    subprocess behaviour).
    """
    validate_timeout_ms(timeout_ms)
    child_env: dict[str, str] | None = None
    if env is not None:
        merged_env = dict(os.environ)
        for key, value in env.items():
            if value is None:
                merged_env.pop(key, None)
            else:
                merged_env[key] = value
        child_env = merged_env
    timeout_s = timeout_ms / 1000.0
    start = time.monotonic()
    timed_out = False
    truncated = False
    truncation_killed = False

    # ignore_cleanup_errors: on Windows a child that outlived the bounded
    # terminate_process_tree() wait still holds these capture files open, and you
    # cannot delete a file another process has open — so rmtree would raise
    # PermissionError out of execute_child before the result is even built. Making
    # cleanup best-effort matches proc.py's documented "Windows child cleanup is
    # best-effort" posture and, unlike a trailing proc.wait(), keeps teardown within
    # the 2x-grace worst-case budget (a blocking reap here would either hang the
    # server on a kernel-stuck child or overrun that budget). The surviving child is
    # not orphaned: the finally leaves it tracker-registered for lifespan teardown.
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp_dir:
        tmp = Path(tmp_dir)
        stdout_path = tmp / "stdout.txt"
        stderr_path = tmp / "stderr.txt"

        with (
            stdout_path.open("w", encoding="utf-8") as stdout_f,
            stderr_path.open("w", encoding="utf-8") as stderr_f,
        ):
            try:
                proc = popen_process_group(
                    argv,
                    stdout=stdout_f,
                    stderr=stderr_f,
                    # No stdin: over stdio the server's stdin is the JSON-RPC channel, so
                    # a script that reads input()/sys.stdin would steal protocol bytes or
                    # block the server. DEVNULL gives the child an immediate EOF instead.
                    stdin=subprocess.DEVNULL,
                    cwd=str(cwd),
                    env=child_env,
                )
            except OSError as exc:
                # ONLY this call is re-keyed: past it a child exists, so a later
                # OSError must not be mistaken for "never launched".
                raise _as_spawn_error(exc) from exc

            registered = False
            try:
                # register and on_start both fire inside the reaping guard so a
                # raise from either cannot orphan the just-spawned child: a
                # closed-tracker register self-terminates and can surface a
                # transient tree-kill error, and the job registry's cancel hook can
                # fail mid-terminate while running on_start. On any raise the finally
                # below still kills and reaps the process. ``registered`` records
                # that register completed, so the finally unregisters only a handle
                # that actually entered the live set.
                if tracker is not None:
                    tracker.register(proc)
                    registered = True
                if on_start is not None:
                    on_start(proc)
                deadline = start + timeout_s
                while proc.poll() is None:
                    now = time.monotonic()
                    if now >= deadline:
                        # Only flag and break — the finally performs the SINGLE
                        # termination. Killing here too would run two full
                        # SIGTERM→SIGKILL sequences against an unreapable child,
                        # double the teardown grace, and overrun the budget
                        # process_tree_terminate_worst_case_ms() accounts for.
                        timed_out = True
                        break
                    # Cap on the *combined* stdout+stderr size so neither stream
                    # alone, nor the two together, can balloon the parent's memory.
                    try:
                        combined = stdout_path.stat().st_size + stderr_path.stat().st_size
                    except OSError:
                        combined = 0
                    if combined > MAX_OUTPUT_BYTES:
                        # Flag and break; the finally performs the single termination
                        # (a second kill here would double the teardown grace for an
                        # unreapable child, past the admission budget).
                        #
                        # KNOWN, ACCEPTED residual race — do not "fix" with another
                        # poll: it would only narrow, never close, the window. The
                        # loop last observed the child running (``proc.poll()`` is
                        # None above), but it may exit on its own before the finally's
                        # ``terminate_process_tree`` checks it, making the signal a
                        # no-op. ``truncation_killed`` therefore records that the cap
                        # branch REQUESTED termination against a child last seen
                        # running — NOT that our signal caused the exit. ``truncated``
                        # stays authoritative (the returned output is partial either
                        # way); MiniZinc solve and unsat-core read ``truncation_killed``
                        # to mask a return code that may be the executor's artifact.
                        truncated = True
                        truncation_killed = True
                        break
                    time.sleep(_POLL_INTERVAL_S)

            finally:
                # The SOLE process-tree termination for every exit path: the
                # timeout/output-cap branches only set flags and break, so this one
                # call bounds teardown to a single SIGTERM→SIGKILL sequence (the 2x
                # grace process_tree_terminate_worst_case_ms() budgets), never two.
                # It also sweeps descendants left by an exited leader and handles a
                # child still running when register or on_start raises.
                terminate_process_tree(proc)
                # poll() reaps an exited leader without waiting; an unreapable leader
                # returns None and stays registered for the lifespan's teardown retry.
                if proc.poll() is not None and tracker is not None and registered:
                    tracker.unregister(proc)

        return_code = proc.returncode
        elapsed_ms = max(int((time.monotonic() - start) * 1000), 0)

        # The cap is on the COMBINED size: stderr only gets whatever budget the
        # (already capped) stdout read left over, so a child that filled both
        # streams before the poll loop caught it cannot return ~2x the cap.
        raw_stdout, stdout_len = _read_capped(stdout_path)
        stderr_budget = MAX_OUTPUT_BYTES - min(stdout_len, MAX_OUTPUT_BYTES)
        raw_stderr, stderr_len = _read_capped(stderr_path, limit=stderr_budget)

    # A burst writer can overrun the cap before the poll loop observes it — on a
    # clean exit, or on a timeout where the deadline (checked first) fires before
    # the size check. Recompute from the on-disk size so ``truncated`` is reliable;
    # ``_read_capped`` has already capped the returned text, so a False here would
    # mislabel partial output as complete. ``truncation_killed`` deliberately stays
    # False here: the output-cap branch never ran, so it makes no claim about the
    # exit — the return code is governed by the ``timed_out`` policy (genuine only
    # when the child truly exited on its own; the executor's artifact when the
    # deadline branch terminated it).
    if stdout_len + stderr_len > MAX_OUTPUT_BYTES:
        truncated = True

    return ChildExecutionResult(
        stdout=raw_stdout,
        stderr=raw_stderr,
        return_code=return_code,
        timed_out=timed_out,
        truncated=truncated,
        truncation_killed=truncation_killed,
        duration_ms=elapsed_ms,
    )
