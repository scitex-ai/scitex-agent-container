"""Tests for runner.run() / _parse_argv() / main() — the lifecycle wrapper.

The existing test_claude_session.py covers _run_conversation and
in-process state I/O. This file adds coverage for the surrounding
asyncio orchestration: run() with no mission, mission-only, a2a-only,
autonomous-driven, foreground print_stream, and SIGTERM-induced
shutdown of the heartbeat / convo / http tasks.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import claude_session as runner


@pytest.fixture(autouse=True)
def _home_to_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))


# ---------------------------------------------------------------------------
# _parse_argv
# ---------------------------------------------------------------------------


class TestParseArgv:
    def test_requires_name(self) -> None:
        with pytest.raises(SystemExit):
            runner._parse_argv([])

    def test_minimal_args(self) -> None:
        ns = runner._parse_argv(["--name", "alpha"])
        assert ns.name == "alpha"
        assert ns.state_root is None
        assert ns.tick_seconds == runner.DEFAULT_TICK_SECONDS
        assert ns.mission is None
        assert ns.resume_session_id is None
        assert ns.print_stream is False
        assert ns.a2a_port is None
        assert ns.a2a_host == "127.0.0.1"
        assert ns.autonomous_enabled is False
        assert ns.autonomous_drive_until == "DONE"
        assert ns.autonomous_max_turns == 50
        assert ns.max_restarts == 0

    def test_full_args(self) -> None:
        ns = runner._parse_argv(
            [
                "--name",
                "ag",
                "--state-root",
                "/tmp/sr",
                "--tick-seconds",
                "5",
                "--mission",
                "hi",
                "--resume-session-id",
                "abc",
                "--a2a-port",
                "9999",
                "--a2a-host",
                "0.0.0.0",
                "--print-stream",
                "--autonomous-enabled",
                "--autonomous-drive-until",
                "END",
                "--autonomous-max-turns",
                "7",
                "--autonomous-kick-text",
                "go",
                "--max-restarts",
                "3",
                "--restart-backoff-s",
                "0.25",
            ]
        )
        assert ns.name == "ag"
        assert ns.state_root == Path("/tmp/sr")
        assert ns.tick_seconds == 5.0
        assert ns.mission == "hi"
        assert ns.resume_session_id == "abc"
        assert ns.a2a_port == 9999  # stx-allow: STX-NL001
        assert ns.a2a_host == "0.0.0.0"
        assert ns.print_stream is True
        assert ns.autonomous_enabled is True
        assert ns.autonomous_drive_until == "END"
        assert ns.autonomous_max_turns == 7
        assert ns.autonomous_kick_text == "go"
        assert ns.max_restarts == 3
        assert ns.restart_backoff_s == 0.25


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_routes_args_through_run(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    async def _fake_run(name, **kw):
        captured["name"] = name
        captured.update(kw)
        return 0

    monkeypatch.setattr(runner, "run", _fake_run)
    rc = runner.main(["--name", "alpha", "--tick-seconds", "0.01"])
    assert rc == 0
    assert captured["name"] == "alpha"
    assert captured["tick_seconds"] == 0.01


# ---------------------------------------------------------------------------
# run() — minimal heartbeat-only path
# ---------------------------------------------------------------------------


def test_run_no_mission_writes_pid_and_heartbeat(tmp_path: Path) -> None:
    """run() with no mission / no a2a-port should write pid + heartbeat,
    install signal handlers, and exit cleanly on SIGTERM."""

    async def _scenario() -> int:
        loop = asyncio.get_running_loop()

        async def _stop_soon():
            # let run() install its signal handlers, then send SIGTERM to self.
            await asyncio.sleep(0.05)
            # The runner registers SIGTERM via loop.add_signal_handler; we
            # can invoke the registered callback by raising the signal.
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-run-1",
            state_root=tmp_path,
            tick_seconds=0.01,
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    state_dir = tmp_path / "ag-run-1"
    assert (state_dir / "pid").is_file()
    assert (state_dir / "heartbeat.json").is_file()
    hb = runner.read_heartbeat(state_dir)
    assert hb is not None
    assert hb["state"] == runner.STATE_STOPPING


def test_run_stop_via_event_after_mission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a mission, run() should seed the inbox and spawn the convo
    task, then idle until stop. We patch _run_conversation so the test
    doesn't need the SDK."""

    drained: list[str] = []

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        # Drain inbox until ShutdownEnvelope.
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
        )

        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if isinstance(env, TurnEnvelope):
                drained.append(env.text)
                if not env.response.done():
                    env.response.set_result("ack")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-run-2",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hello",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    # mission was placed onto the inbox and consumed by the fake convo.
    assert drained == ["hello"]


def test_run_print_stream_foreground_returns_after_convo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In foreground (print_stream=True, no autonomous), run() should
    await the convo task and return immediately."""

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        # Drain mission turn and finish.
        env = await inbox.get()
        if hasattr(env, "response") and not env.response.done():
            env.response.set_result("ok")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        return await runner.run(
            "ag-fg",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            print_stream=True,
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    hb = runner.read_heartbeat(tmp_path / "ag-fg")
    assert hb is not None and hb["state"] == runner.STATE_STOPPING


def test_run_autonomous_path_drives_until_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() with autonomous_enabled drives turns until drive_until
    matches; the autonomous loop sets stop, which unwinds the daemon."""

    async def _fake_conv(
        name,
        state_dir,
        *,
        pid,
        inbox,
        resume_session_id,
        stop,
        print_stream=False,
        max_restarts=0,
        restart_backoff_s=1.0,
    ) -> None:
        from scitex_agent_container._runners._session_inbox import (
            ShutdownEnvelope,
            TurnEnvelope,
        )

        replies = iter(["nope", "nope", "DONE here"])
        while True:
            env = await inbox.get()
            if isinstance(env, ShutdownEnvelope):
                return
            if isinstance(env, TurnEnvelope):
                if not env.response.done():
                    try:
                        env.response.set_result(next(replies))
                    except StopIteration:
                        env.response.set_result("DONE")

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        return await runner.run(
            "ag-auto",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
            autonomous_enabled=True,
            autonomous_drive_until="DONE",
            autonomous_max_turns=10,
            autonomous_kick_text="continue",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0


def test_run_a2a_port_spawns_http_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a2a_port is set, run() launches serve_inbound. We patch
    serve_inbound to a no-op so the test doesn't open sockets."""
    served: dict[str, Any] = {}

    async def _fake_serve(inbox, *, host, port, stop):
        served["host"] = host
        served["port"] = port
        # Idle until stop fires.
        await stop.wait()

    from scitex_agent_container._runners import _session_http

    monkeypatch.setattr(_session_http, "serve_inbound", _fake_serve)

    async def _fake_conv(name, state_dir, **kw):
        await kw["stop"].wait()

    monkeypatch.setattr(runner, "_run_conversation", _fake_conv)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-a2a",
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12345,  # stx-allow: STX-NL001
            a2a_host="0.0.0.0",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    assert served["port"] == 12345  # stx-allow: STX-NL001
    assert served["host"] == "0.0.0.0"


def test_run_cancels_hung_convo_task_on_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the convo task ignores the ShutdownEnvelope and hangs, run()
    falls back to convo_task.cancel() after the 5s wait_for window. To
    keep the test fast we patch asyncio.wait_for to immediately raise
    TimeoutError on the convo path."""

    async def _hanging_conv(name, state_dir, **kw):
        # Ignore inbox + stop forever.
        while True:
            await asyncio.sleep(60)

    monkeypatch.setattr(runner, "_run_conversation", _hanging_conv)

    real_wait_for = asyncio.wait_for
    convo_seen: list[int] = []

    async def _instant_timeout(awaitable, timeout):
        # Only short-circuit the 5s convo / http waits, not other waits.
        if 4.5 <= timeout <= 5.5:
            convo_seen.append(1)
            # Cancel the awaitable so it doesn't leak.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            else:
                awaitable.cancel()
            raise asyncio.TimeoutError
        return await real_wait_for(awaitable, timeout)

    monkeypatch.setattr(asyncio, "wait_for", _instant_timeout)

    async def _scenario() -> int:
        async def _stop_soon():
            await asyncio.sleep(0.05)
            import os

            os.kill(os.getpid(), signal.SIGTERM)

        asyncio.create_task(_stop_soon())
        return await runner.run(
            "ag-hang",
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="hi",
        )

    rc = asyncio.run(_scenario())
    assert rc == 0
    assert convo_seen, "convo task wait_for(5s) path was not exercised"
