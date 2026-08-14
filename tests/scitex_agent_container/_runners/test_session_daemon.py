"""Contract tests for :mod:`scitex_agent_container._runners.session_daemon`.

The residency daemon was extracted verbatim from ``claude_session`` (v4
migration step 3 — "sac owns the process; a harness owns only the
turn"). Its behavior is already covered end-to-end by
``test_claude_session.py`` through the ``run()`` wrapper; the tests
here pin the DAEMON'S OWN surface — the parts a future harness runner
(v4 step 7) will program against directly:

- ``turn_driver`` is a required keyword (the daemon has no vendor
  default; the harness module supplies it),
- the daemon calls the driver with the documented kwarg shape,
- the lifecycle artifacts (pid file, STOPPING heartbeat) come from the
  daemon itself, with no harness code involved.

No mocks — hand-rolled coroutines only, same as the sibling suite.
"""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._runners import session_daemon
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
)


async def _drain_driver(
    name: str,
    state_dir: Path,
    **kwargs: Any,
) -> None:
    """Minimal well-behaved turn driver: ack every turn until shutdown."""
    inbox = kwargs["inbox"]
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_result("ack")


def _sigterm_soon(delay_s: float = 0.05) -> None:
    async def _kick() -> None:
        await asyncio.sleep(delay_s)
        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(_kick())


# ---------------------------------------------------------------------------
# turn_driver is the required harness seam
# ---------------------------------------------------------------------------


def test_run_session_daemon_requires_turn_driver_keyword(tmp_path: Path) -> None:
    # Arrange: no driver supplied — there is no vendor default here.
    kwargs = {"state_root": tmp_path}
    # Act
    call = lambda: session_daemon.run_session_daemon("ag-seam", **kwargs)  # noqa: E731
    # Assert
    with pytest.raises(TypeError):
        call()


def test_run_session_daemon_calls_driver_with_documented_kwargs(
    tmp_path: Path,
) -> None:
    # Arrange: capture the exact call shape the daemon hands the driver.
    seen: dict[str, Any] = {}

    async def _capturing_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
        seen["name"] = name
        seen["state_dir"] = state_dir
        seen["kwargs"] = set(kwargs)
        await _drain_driver(name, state_dir, **kwargs)

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-shape",
            turn_driver=_capturing_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
        )

    # Act
    asyncio.run(_scenario())
    # Assert: this is the v4 turn-driver contract; step 7's harness
    # driver must accept exactly these keywords.
    assert seen["kwargs"] == {
        "pid",
        "inbox",
        "resume_session_id",
        "stop",
        "print_stream",
        "max_restarts",
        "restart_backoff_s",
        "host",
        "channels",
        "a2a_port",
    }


def test_run_session_daemon_driver_gets_name_and_state_dir(tmp_path: Path) -> None:
    # Arrange
    seen: dict[str, Any] = {}

    async def _capturing_driver(name: str, state_dir: Path, **kwargs: Any) -> None:
        seen["name"] = name
        seen["state_dir"] = state_dir
        await _drain_driver(name, state_dir, **kwargs)

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-dir",
            turn_driver=_capturing_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            mission="boot",
        )

    # Act
    asyncio.run(_scenario())
    # Assert
    assert seen["name"] == "ag-dir" and seen["state_dir"] == tmp_path / "ag-dir"


def test_run_session_daemon_without_producer_never_calls_driver(
    tmp_path: Path,
) -> None:
    # Arrange: no mission and no a2a_port — the inbox has no producer,
    # so the daemon must not spawn a conversation task at all.
    called: list[str] = []

    async def _driver(name: str, state_dir: Path, **kwargs: Any) -> None:
        called.append(name)

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-idle",
            turn_driver=_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and called == []


# ---------------------------------------------------------------------------
# lifecycle artifacts are the daemon's own
# ---------------------------------------------------------------------------


def test_run_session_daemon_writes_pid_file(tmp_path: Path) -> None:
    # Arrange
    from scitex_agent_container._runners._session_state import read_pid

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-pid",
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and read_pid(tmp_path / "ag-pid") == os.getpid()


def test_run_session_daemon_final_heartbeat_is_stopping(tmp_path: Path) -> None:
    # Arrange
    from scitex_agent_container._runners._session_state import (
        STATE_STOPPING,
        read_heartbeat,
    )

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-stop",
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
        )

    # Act
    asyncio.run(_scenario())
    hb = read_heartbeat(tmp_path / "ag-stop")
    # Assert
    assert hb is not None and hb["state"] == STATE_STOPPING


# ---------------------------------------------------------------------------
# a2a sidecar stays a daemon concern (the harness never sees it)
# ---------------------------------------------------------------------------


def test_run_session_daemon_spawns_sidecar_with_supplied_port(tmp_path: Path) -> None:
    # Arrange
    served: dict[str, Any] = {}

    async def _fake_serve(inbox: Any, *, host: str, port: int, stop: Any, **kw: Any) -> None:
        served["host"] = host
        served["port"] = port
        await stop.wait()

    async def _scenario() -> int:
        _sigterm_soon()
        return await session_daemon.run_session_daemon(
            "ag-side",
            turn_driver=_drain_driver,
            state_root=tmp_path,
            tick_seconds=0.01,
            a2a_port=12_347,
            a2a_host="127.0.0.1",
            serve_inbound_fn=_fake_serve,
        )

    # Act
    rc = asyncio.run(_scenario())
    # Assert
    assert rc == 0 and served == {"host": "127.0.0.1", "port": 12_347}
