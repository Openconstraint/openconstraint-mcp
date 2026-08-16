"""Protocol-level coverage for dual-channel status notifications.

These tests drive a real in-memory MCP client/server pair, so they prove what
fake-context unit tests cannot: the SDK extracts ``_meta.progressToken`` from a
live ``tools/call`` request, progress notifications echo that token, ``info``
log notifications flow with or without a token, and the notification stream
ends with the response.

The in-memory transport still shares one event loop between client and server,
so it cannot observe *when* a notification reaches the wire. The
``integration``-marked stdio test at the bottom covers that: it spawns the
real server process with a slow fake binary and asserts the pre-solve
milestone arrives while the solve is still running.
"""

from __future__ import annotations

import os
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import anyio
import mcp_types as types
import pytest
from mcp.client import Client
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp_types.version import LATEST_PROTOCOL_VERSION

from openconstraint_mcp.server import create_mcp_server
from tests.minizinc.helpers import child_result

_MODEL = "var 1..5: x;\nconstraint x > 2;\nsolve satisfy;"

_EXPECTED_CHECK_MESSAGES = [
    "Validating check request",
    "MiniZinc compile check is running",
    "MiniZinc finished; parsing check result",
    "Check complete",
]


@dataclass
class _ProtocolCapture:
    result: types.CallToolResult
    routed: list[tuple[float, float | None, str | None]]
    raw_progress: list[types.ProgressNotificationParams]
    logs: list[types.LoggingMessageNotificationParams]
    progress_count_at_response: int
    log_count_at_response: int


async def _call_check_over_protocol(
    monkeypatch: pytest.MonkeyPatch, *, with_token: bool
) -> _ProtocolCapture:
    """Call check_minizinc_model through a live in-memory MCP session.

    ``with_token=True`` uses the client SDK's ``progress_callback``, the
    official way a client requests progress: it stamps ``_meta.progressToken``
    on the request and routes only token-matching notifications back to the
    callback — so callback delivery itself proves the token echo.

    ``mode="legacy"`` forces the initialize-handshake, stream-framed transport:
    the default ``"auto"`` mode dispatches an in-process ``MCPServer`` directly
    (no JSON-RPC framing, no notification stream), so raw ``ServerNotification``
    objects never reach ``message_handler`` there — only ``progress_callback``
    fires. These tests assert on the raw notification stream itself (single
    token, no-total, nothing after the response), so they need the real
    streamed transport that ``legacy`` mode provides.
    """
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *a, **k: child_result(stdout="", stderr="", returncode=0),
    )

    routed: list[tuple[float, float | None, str | None]] = []
    raw_progress: list[types.ProgressNotificationParams] = []
    logs: list[types.LoggingMessageNotificationParams] = []

    async def _on_message(message: object) -> None:
        if isinstance(message, types.ProgressNotification):
            raw_progress.append(message.params)

    async def _on_log(params: types.LoggingMessageNotificationParams) -> None:
        logs.append(params)

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        routed.append((progress, total, message))

    async with Client(
        create_mcp_server(),
        mode="legacy",
        message_handler=_on_message,
        logging_callback=_on_log,
    ) as client:
        result = await client.call_tool(
            "check_minizinc_model",
            {"model": _MODEL},
            progress_callback=_on_progress if with_token else None,
        )
        progress_count_at_response = len(raw_progress)
        log_count_at_response = len(logs)
        # Give any stray late notification a chance to arrive before teardown.
        await anyio.sleep(0.05)

    return _ProtocolCapture(
        result=result,
        routed=routed,
        raw_progress=raw_progress,
        logs=logs,
        progress_count_at_response=progress_count_at_response,
        log_count_at_response=log_count_at_response,
    )


@pytest.mark.asyncio
async def test_progress_token_round_trip_delivers_increasing_stage_counters(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    # The client routes only notifications whose token matches the one it sent,
    # so receiving the full schedule here is the token-echo proof.
    assert [progress for progress, _total, _message in captured.routed] == [1, 2, 3, 4]


@pytest.mark.asyncio
async def test_progress_notifications_echo_a_single_token(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    assert len({params.progress_token for params in captured.raw_progress}) == 1


@pytest.mark.asyncio
async def test_progress_notifications_omit_total(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    assert all(params.total is None for params in captured.raw_progress)


@pytest.mark.asyncio
async def test_no_notifications_arrive_after_the_tool_response(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    assert len(captured.raw_progress) == captured.progress_count_at_response
    assert len(captured.logs) == captured.log_count_at_response


@pytest.mark.asyncio
async def test_log_notifications_mirror_progress_milestone_messages(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    assert [log.data for log in captured.logs] == _EXPECTED_CHECK_MESSAGES
    assert all(log.level == "info" for log in captured.logs)


@pytest.mark.asyncio
async def test_tool_result_is_unchanged_when_progress_is_requested(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=True)

    assert captured.result.is_error is False
    assert captured.result.structured_content is not None
    assert captured.result.structured_content["status"] == "ok"


@pytest.mark.asyncio
async def test_request_without_token_sends_no_progress_notifications(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = await _call_check_over_protocol(monkeypatch, with_token=False)

    assert captured.raw_progress == []
    assert captured.result.is_error is False


@pytest.mark.asyncio
async def test_request_without_token_still_receives_info_log_feedback(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The product goal on a handshake-era session: a client that never asks for
    # progress still sees visible status feedback through the log channel.
    # SEP-2577 narrowed this to handshake-era only — the test below pins what a
    # 2026-07-28 client gets instead.
    captured = await _call_check_over_protocol(monkeypatch, with_token=False)

    assert [log.data for log in captured.logs] == _EXPECTED_CHECK_MESSAGES


@pytest.mark.asyncio
async def test_modern_protocol_client_gets_progress_but_no_unsolicited_logs(
    fake_minizinc_binary: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SEP-2577 made `notifications/message` a per-request opt-in at 2026-07-28:
    # a modern client that does not ask for logs gets none, while progress
    # notifications are unaffected. Pinned here because every other test in this
    # module runs `mode="legacy"`, where log delivery is still unconditional —
    # so none of them would notice this channel going silent.
    monkeypatch.setattr(
        "openconstraint_mcp.minizinc.core.execute_child",
        lambda *a, **k: child_result(stdout="", stderr="", returncode=0),
    )

    logs: list[types.LoggingMessageNotificationParams] = []
    milestones: list[str | None] = []

    async def _on_log(params: types.LoggingMessageNotificationParams) -> None:
        logs.append(params)

    async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
        milestones.append(message)

    async with Client(
        create_mcp_server(),
        mode=LATEST_PROTOCOL_VERSION,
        logging_callback=_on_log,
    ) as client:
        await client.call_tool(
            "check_minizinc_model",
            {"model": _MODEL},
            progress_callback=_on_progress,
        )

    assert milestones == _EXPECTED_CHECK_MESSAGES
    assert logs == [], "SEP-2577: logs must not arrive unsolicited on a modern session"


# --- real-transport timing: notifications must flush mid-solve --------------


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform == "win32",
    reason="the fake solver is a POSIX shell-script stub; Windows' loader rejects a "
    "non-PE file named minizinc.exe (WinError 216)",
)
async def test_stdio_delivers_running_milestone_while_solve_is_in_flight(
    tmp_path: Path,
) -> None:
    """The pre-solve milestone must reach a stdio client during the solve.

    Needs no real MiniZinc runtime (the binary is a sleeping stub), but it is
    timing-sensitive and spawns a real server subprocess, so it stays out of
    the default gate. It pins the PR #34 review finding: with the core call
    inline on the event loop, the stdio writer task held the last queued
    notification until the solve finished, so token-less clients saw silence
    for the whole run and a burst at the end.
    """
    bin_dir = tmp_path / "runtime" / "bin"
    bin_dir.mkdir(parents=True)
    binary = bin_dir / "minizinc"
    binary.write_text("#!/bin/sh\nsleep 2\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    log_times: list[tuple[str, float]] = []

    async def _on_log(params: types.LoggingMessageNotificationParams) -> None:
        log_times.append((str(params.data), time.monotonic()))

    server = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from openconstraint_mcp.server import run_stdio; run_stdio()"],
        env={**os.environ, "OPENCONSTRAINT_MCP_RUNTIME_DIR": str(tmp_path / "runtime")},
    )

    async with stdio_client(server) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream, logging_callback=_on_log) as session:
            await session.initialize()
            started = time.monotonic()
            result = await session.call_tool("check_minizinc_model", {"model": "solve satisfy;"})
            finished = time.monotonic()

    assert result.is_error is False
    assert finished - started > 1.5, "the fake solver did not actually block the call"
    running = [t for message, t in log_times if message == "MiniZinc compile check is running"]
    assert running, log_times
    assert running[0] - started < 1.0, (
        f"'running' milestone arrived {running[0] - started:.2f}s after the call started — "
        "it was stranded behind the blocking solve instead of flushing before it"
    )
