"""Tests for the key-passthrough path of ``_send.send_to_agent``.

The MCP ``agent_send`` tool mirrors the CLI ``sac agents send
--key/--keys`` by passing ``key`` / ``keys`` to
:func:`scitex_agent_container.cli_pkg._send.send_to_agent`. Routing:

  * cancel keys (ESC / C-c / SIGINT) → SIGINT the local runner pid;
  * any other named key / sequence → tmux send-keys to the LOCAL
    session;
  * a cross-host / not-running-locally agent → loud error (key
    passthrough is local-tmux only);
  * an unknown key name → loud error.

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. Real
collaborators are swapped at the module namespace via save/restore
context managers; env mutations follow the same pattern.

STX-TQ002 AAA-markers + STX-TQ007 one-assert.
"""

from __future__ import annotations

import importlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import scitex_agent_container._runners._tmux.multiplexer as _mux_mod
from scitex_agent_container.cli_pkg._send import send_to_agent


@pytest.fixture
def state_db_env(tmp_path: Path) -> Iterator[Path]:
    """Isolate ``state.db`` + pin host to ``lead-host`` for the test."""
    saved_db = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host = os.environ.get("SAC_HOST")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(tmp_path / "state.db")
    os.environ["SAC_HOST"] = "lead-host"
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        if saved_db is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_db
        if saved_host is None:
            os.environ.pop("SAC_HOST", None)
        else:
            os.environ["SAC_HOST"] = saved_host
        importlib.reload(_state_db_mod)


def _seed_local(name: str, *, a2a_port: int = 12345, pid: int = 4242) -> None:
    """Record an active instance row on the current (lead) host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(
        name=name, host="lead-host", pid=pid, a2a_port=a2a_port
    )


def _seed_remote(name: str, *, peer: str = "peer-host", a2a_port: int = 1) -> None:
    """Record an active instance row on a peer host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host=peer, a2a_port=a2a_port)


class _RecordingMux:
    """Fake multiplexer recording ``send_keys``; session present."""

    sent: list[tuple[str, tuple[str, ...]]] = []

    @staticmethod
    def exists(session: str) -> bool:
        return True

    @classmethod
    def send_keys(cls, session: str, *keys: str) -> None:
        cls.sent.append((session, keys))


@contextmanager
def _swap_mux_and_config(session: str):
    """Swap ``get_multiplexer`` + config resolvers so the send-keys path
    runs against a fresh recording fake with a known ``screen_name``.

    Returns the fresh recording mux class so the test can probe ``sent``.
    """
    import scitex_agent_container.cli_pkg._send as _send_mod  # noqa: F401

    class _Mux(_RecordingMux):
        sent: list[tuple[str, tuple[str, ...]]] = []

    class _Cfg:
        screen_name = session

    saved_get = _mux_mod.get_multiplexer
    import scitex_agent_container.config as _config_mod
    import scitex_agent_container.config._resolve as _resolve_mod

    saved_resolve = _resolve_mod.resolve_config
    saved_load = _config_mod.load_config
    _mux_mod.get_multiplexer = lambda cfg: _Mux  # type: ignore[assignment]
    _resolve_mod.resolve_config = lambda name: "spec.yaml"  # type: ignore[assignment]
    _config_mod.load_config = lambda path: _Cfg()  # type: ignore[assignment]
    try:
        yield _Mux
    finally:
        _mux_mod.get_multiplexer = saved_get  # type: ignore[assignment]
        _resolve_mod.resolve_config = saved_resolve  # type: ignore[assignment]
        _config_mod.load_config = saved_load  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# send-keys delivery (local agent)
# ---------------------------------------------------------------------------


class TestNamedKeyDelivered:
    """A non-cancel named key reaches the local tmux session."""

    def test_enter_sent_to_session(self, state_db_env) -> None:
        # Arrange
        _seed_local("alpha")
        # Act
        with _swap_mux_and_config("cld-alpha") as mux:
            send_to_agent("alpha", key="Enter")
        # Assert
        assert mux.sent == [("cld-alpha", ("Enter",))]

    def test_status_ok_route_send_keys(self, state_db_env) -> None:
        # Arrange
        _seed_local("alpha")
        # Act
        with _swap_mux_and_config("cld-alpha"):
            result = send_to_agent("alpha", key="2")
        # Assert
        assert result["route"] == "send-keys"


class TestKeySequenceDelivered:
    """A ``keys`` sequence is split, validated and sent in order."""

    def test_sequence_sent_in_order(self, state_db_env) -> None:
        # Arrange
        _seed_local("alpha")
        # Act
        with _swap_mux_and_config("cld-alpha") as mux:
            send_to_agent("alpha", keys="Up Up Enter")
        # Assert
        assert mux.sent == [("cld-alpha", ("Up", "Up", "Enter"))]


# ---------------------------------------------------------------------------
# cancel key → SIGINT
# ---------------------------------------------------------------------------


class TestCancelKeyInterrupts:
    """ESC routes to the SIGINT interrupt path, returning route=interrupt."""

    def test_esc_returns_interrupt_route(self, state_db_env) -> None:
        # Arrange — a real short-lived child handles SIGINT (NOT the test
        # process), and its pid is the one the interrupt path targets.
        import subprocess
        import sys

        child = subprocess.Popen(
            [sys.executable, "-c", "import signal,time\n"
             "signal.signal(signal.SIGINT, lambda *a: None)\n"
             "time.sleep(30)"]
        )
        _seed_local("alpha", pid=child.pid)
        sd = state_db_env / "runtime" / "alpha"
        sd.mkdir(parents=True)
        (sd / "pid").write_text(str(child.pid))
        saved = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
        os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(
            state_db_env / "runtime"
        )
        import scitex_agent_container._runners._session_state as _ss

        importlib.reload(_ss)
        # Act
        try:
            result = send_to_agent("alpha", key="ESC")
        finally:
            child.kill()
            child.wait()
            if saved is None:
                os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
            else:
                os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = saved
            importlib.reload(_ss)
        # Assert
        assert result["route"] == "interrupt"


# ---------------------------------------------------------------------------
# error surfaces
# ---------------------------------------------------------------------------


class TestUnknownKeyError:
    """An unknown key name returns a structured error (no send)."""

    def test_unknown_key_status_error(self, state_db_env) -> None:
        # Arrange
        _seed_local("alpha")
        # Act
        with _swap_mux_and_config("cld-alpha"):
            result = send_to_agent("alpha", key="Bogus")
        # Assert
        assert result["status"] == "error"


class TestCrossHostRejected:
    """A key to a remote-only agent is refused loud (local-tmux only)."""

    def test_remote_agent_status_error(self, state_db_env) -> None:
        # Arrange — only a peer-host row exists.
        _seed_remote("beta")
        # Act
        result = send_to_agent("beta", key="Enter")
        # Assert
        assert result["status"] == "error"

    def test_remote_agent_error_mentions_local(self, state_db_env) -> None:
        # Arrange
        _seed_remote("beta")
        # Act
        result = send_to_agent("beta", key="Enter")
        # Assert
        assert "local" in result["error"]


# ---------------------------------------------------------------------------
# mutual exclusion
# ---------------------------------------------------------------------------


class TestMutualExclusion:
    """prompt / key / keys are mutually exclusive."""

    def test_key_and_keys_raises(self, state_db_env) -> None:
        # Arrange
        raised: Exception | None = None
        # Act
        try:
            send_to_agent("alpha", key="Enter", keys="Up Down")
        except ValueError as exc:
            raised = exc
        # Assert
        assert isinstance(raised, ValueError)


# EOF
