"""Server-owned tracker of in-flight synchronous child processes.

The background-job path tracks its live solver children in the ``JobRegistry``
and terminates them from the lifespan teardown. The *synchronous* tools (the
MiniZinc solve/check/inspect/unsat-core/save tools and the CP-SAT
``run_cpsat_python`` / ``save_verified_cpsat_python`` tools) run their blocking
work in an anyio worker thread and launch a child process-group leader that does
NOT die when the server exits — so an abrupt teardown orphans it. This tracker
gives those in-flight children the same teardown coverage: each launch site
registers its handle while the child runs and unregisters on completion, and the
lifespan terminates whatever is still registered.

Layering: a dependency-light leaf importable by both ``minizinc`` and ``pyexec``
(mirroring ``proc`` / ``save_target``). It depends only on ``proc`` — the process
primitive it is built on — for the actual tree-kill, exactly as ``minizinc.jobs`` does.

Not a module-level singleton: one instance is created per server in
``create_mcp_server`` and owned by that server's lifecycle, the same sanctioned
exception to "no global mutable state" as the ``JobRegistry``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Generator
from contextlib import contextmanager
from subprocess import Popen
from typing import Any

from .proc import terminate_process_tree


class ChildProcessTracker:
    """A thread-safe set of live child-process handles awaiting teardown.

    ``register``/``unregister`` (or the ``track`` context manager) bracket a
    child's lifetime; ``terminate_all`` tears down whatever is still live. All
    mutation is guarded by a lock because the synchronous tools register from
    anyio worker threads while the lifespan tears down from the event-loop thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._handles: set[Popen[Any]] = set()
        self._closed = False

    def register(self, proc: Popen[Any]) -> None:
        """Track ``proc`` until it finishes; if teardown already ran, kill it now.

        A worker that reaches this point after ``terminate_all`` has snapshotted
        (still inside its launch→register window when teardown fired) would
        otherwise leave its handle in a set nobody drains again — orphaned at exit.
        Once the tracker is closed, terminate the late registrant on the spot
        instead, the same launch-window guard ``JobRegistry.shutdown`` applies via
        ``cancel_requested``. The kill runs outside the lock so a slow tree-kill
        cannot block another worker's register/unregister.
        """
        with self._lock:
            if not self._closed:
                self._handles.add(proc)
                return
        terminate_process_tree(proc)

    def unregister(self, proc: Popen[Any]) -> None:
        """Drop ``proc`` from the live set; a handle already gone is a no-op."""
        with self._lock:
            self._handles.discard(proc)

    @contextmanager
    def track(self, proc: Popen[Any]) -> Generator[None]:
        """Register ``proc`` for the duration of the ``with`` block.

        Unregisters on exit whether the body returns or raises, so a child that
        finished (cleanly or by timeout) is never left in the live set to be
        re-terminated at teardown.
        """
        self.register(proc)
        try:
            yield
        finally:
            self.unregister(proc)

    def snapshot(self) -> set[Popen[Any]]:
        """Return a copy of the currently-registered handles (for tests/inspection)."""
        with self._lock:
            return set(self._handles)

    def terminate_all(
        self, *, _terminator: Callable[[Popen[Any]], None] = terminate_process_tree
    ) -> None:
        """Terminate the process tree of every still-registered child, then close.

        Marks the tracker closed and snapshots the live set in one lock hold —
        atomic so a concurrent ``register`` either lands in this snapshot or sees
        the closed flag and self-terminates, never slipping between the two — then
        terminates outside the lock so a slow tree-kill cannot block a worker thread
        trying to unregister. ``terminate_process_tree`` is idempotent and a no-op
        once a handle's whole process group is gone, so a child that races to
        completion here is harmless (and any descendants it left get swept).
        ``_terminator`` is injectable for tests only; production always uses the
        real tree-kill.
        """
        with self._lock:
            self._closed = True
            handles = set(self._handles)
        for proc in handles:
            _terminator(proc)
