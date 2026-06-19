"""Tests for ``scitex_agent_container.cli_pkg._send.send_to_agent``.

The library-facing helper that backs both the CLI's cross-host branch
and the MCP ``agent_send`` tool. Verifies:

* status="ok" payload shape on a successful sidecar reply
* status="error" when no active state.db row exists
* status="error" when the row has no a2a_port
* status="timeout" when the sidecar exceeds ``timeout_seconds``
* prompt / key mutual exclusion raises ValueError
* cross-host row routes through the ssh:// branch

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch``. All
collaborators are swapped at the module namespace via real save/restore
context managers, and env mutations follow the same pattern.

STX-TQ007: each test asserts exactly one fact. STX-TQ002: every test
carries Arrange / Act / Assert markers.
"""

from __future__ import annotations

import importlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import scitex_agent_container.cli_pkg._send as _send_mod
from scitex_agent_container.cli_pkg._send import send_to_agent

# ---------------------------------------------------------------------------
# Collaborator swaps (test seams — no mocks, no monkeypatch)
# ---------------------------------------------------------------------------


@contextmanager
def _swap_post_turn(fn) -> Iterator[None]:
    """Replace ``_send._post_turn`` with ``fn`` for the duration of the test."""
    saved = _send_mod._post_turn
    _send_mod._post_turn = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        _send_mod._post_turn = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Fresh OAuth credentials fixture
#
# PR #114 wired ``preflight_send_creds`` into ``send_to_agent`` so a stale
# ``~/.claude/.credentials.json`` short-circuits dispatch with
# ``status="creds-expired"`` BEFORE the test's mocked ``_post_turn`` ever
# runs. CI runners don't have the operator's credentials file at all,
# which fires the same short-circuit (``FileNotFoundError`` → mapped to
# ``creds-expired``).
#
# The production helper accepts ``lead_creds_path=`` as an explicit
# injection seam (see ``_send.send_to_agent`` docstring) — tests that
# need to exercise the post-preflight code path pass an explicit tmp
# creds file so the preflight passes deterministically on any host.
# ---------------------------------------------------------------------------


@pytest.fixture
def fresh_lead_creds_path(tmp_path) -> Path:
    """Return a path to a fresh, non-expired OAuth credentials JSON.

    Matches the shape ``_state._preflight_creds.check_oauth_token_expiry``
    reads: ``{"claudeAiOauth": {"expiresAt": <unix-millis>, ...}}`` with
    expiry one hour in the future (well beyond the 5-minute skew window).
    """
    creds = tmp_path / ".credentials.json"
    expires_at_ms = int((time.time() + 3600) * 1000)
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": expires_at_ms,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return creds


# ---------------------------------------------------------------------------
# state.db isolation fixture: redirect env so the helper sees an empty
# db unless the test seeds rows itself.
# ---------------------------------------------------------------------------


@pytest.fixture
def state_db_env(tmp_path):
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


def _seed_local(name: str, a2a_port: int) -> None:
    """Record an active instance row on the current host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host="lead-host", a2a_port=a2a_port)


def _seed_remote(name: str, peer: str, a2a_port: int) -> None:
    """Record an active instance row on a peer host."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host=peer, a2a_port=a2a_port)


# ---------------------------------------------------------------------------
# Spec test 1-3: status="ok" on successful reply + response_text present
# ---------------------------------------------------------------------------


def test_agent_send_returns_dict_with_status_field(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("hello", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha", "hi", wait=True, lead_creds_path=fresh_lead_creds_path
        )
    # Assert
    assert "status" in result


def test_agent_send_status_ok_on_successful_response(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("hello back", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha", "hi", wait=True, lead_creds_path=fresh_lead_creds_path
        )
    # Assert
    assert result["status"] == "ok"


def test_agent_send_returns_response_text_field_on_success(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("the reply", {})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha", "hi", wait=True, lead_creds_path=fresh_lead_creds_path
        )
    # Assert
    assert result["response_text"] == "the reply"


# ---------------------------------------------------------------------------
# Spec test 4: agent not running → status="error"
# ---------------------------------------------------------------------------


def test_agent_send_status_error_when_agent_not_running(state_db_env):
    # Arrange — no rows seeded
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert result["status"] == "error"


def test_agent_send_error_message_when_agent_not_running(state_db_env):
    # Arrange
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert "not running" in result["error"]


# ---------------------------------------------------------------------------
# Spec test 5: slow sidecar → status="timeout"
# ---------------------------------------------------------------------------


def test_agent_send_status_timeout_on_slow_sidecar(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError(f"peer timeout at {url} after {timeout_s:.0f}s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["status"] == "timeout"


def test_agent_send_timeout_error_message_quotes_timeout_value(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert "2s" in result["error"]


# ---------------------------------------------------------------------------
# Spec test 6: prompt + key mutually exclusive → ValueError
# ---------------------------------------------------------------------------


def test_agent_send_prompt_and_key_mutually_exclusive(state_db_env):
    # Arrange
    # Act
    raised: Exception | None = None
    try:
        send_to_agent("alpha", prompt="hi", key="ESC")
    except ValueError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


def test_agent_send_neither_prompt_nor_key_raises_value_error(state_db_env):
    # Arrange
    name = "alpha"
    # Act
    raised: Exception | None = None
    try:
        send_to_agent(name)
    except ValueError as exc:
        raised = exc
    # Assert
    assert isinstance(raised, ValueError)


# ---------------------------------------------------------------------------
# Spec test 7: cross-host row → ssh:// URL used
# ---------------------------------------------------------------------------


def test_agent_send_cross_host_routes_through_ssh(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_remote("beta", peer="peer-x", a2a_port=18888)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Cross-host preflight also ssh-probes the peer; inject a stub
    # runner that returns rc=0 so the preflight passes without invoking
    # real ssh. (Lead-local probe is satisfied by ``fresh_lead_creds_path``.)
    import subprocess

    def fake_ssh_runner(peer_host, remote_creds_path):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent(
            "beta",
            "hi",
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
            ssh_runner=fake_ssh_runner,
        )
    # Assert
    assert captured["url"] == "ssh://peer-x:18888/v1/turn"


def test_agent_send_local_host_uses_loopback_url(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent("alpha", "hi", wait=True, lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert captured["url"] == "http://127.0.0.1:12345/v1/turn"


# ---------------------------------------------------------------------------
# Bonus: response_metadata + a2a_port missing branch + non-200 branch
# ---------------------------------------------------------------------------


def test_agent_send_includes_response_metadata_on_success(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)

    def fake_post(url, text, *, timeout_s):
        return ("reply", {"exit_after": True})

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha", "hi", wait=True, lead_creds_path=fresh_lead_creds_path
        )
    # Assert
    assert result["response_metadata"]["name"] == "alpha"


def test_agent_send_error_when_row_has_no_a2a_port(state_db_env):
    # Arrange
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name="alpha", host="lead-host", a2a_port=None)
    # Act
    result = send_to_agent("alpha", "hi")
    # Assert
    assert result["status"] == "error"


def test_agent_send_error_when_sidecar_returns_non_200(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer returned HTTP 500: internal error")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# State-aware failure diagnosis (PS-NN: send_to_agent attaches a
# ``diagnosis`` to the timeout / error returns so the caller can tell
# "still booting" / "alive & busy" / "dead" / "port unreachable" apart).
#
# Real state: the ``heartbeats`` diary table + ``instances`` row + a real
# local TCP listener are used — no mocks. ``record_heartbeat`` writes a
# real row; a throwaway socket binds a real port.
# ---------------------------------------------------------------------------


def _record_heartbeat(name: str, state: str, *, pid=None, ts=None) -> None:
    """Append one real ``heartbeats`` row for ``name`` (no mocks)."""
    from scitex_agent_container._state.state_db import record_heartbeat

    record_heartbeat(name=name, host="lead-host", pid=pid, state=state, ts=ts)


def _seed_local_with_pid(name: str, a2a_port: int, pid: int) -> None:
    """Record an active local instance row carrying a specific pid."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host="lead-host", pid=pid, a2a_port=a2a_port)


@contextmanager
def _real_listener() -> Iterator[int]:
    """Bind a real TCP listener on a free loopback port; yield the port.

    Used so the diagnosis sees ``port_reachable=True`` and the heartbeat
    dimension (not the port) is the deciding factor for ``likely_causes``.
    """
    import socket as _socket

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    try:
        yield srv.getsockname()[1]
    finally:
        srv.close()


def test_agent_send_timeout_includes_diagnosis(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError(f"peer timeout at {url} after {timeout_s:.0f}s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert "diagnosis" in result


def test_agent_send_error_includes_diagnosis(state_db_env, fresh_lead_creds_path):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer returned HTTP 500: internal error")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert "diagnosis" in result


def test_agent_send_diagnosis_reports_registry_running(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["diagnosis"]["registry_status"] == "running"


def test_agent_send_not_running_diagnosis_reports_stopped(state_db_env):
    # Arrange — no rows seeded
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert result["diagnosis"]["registry_status"] == "stopped"


def test_agent_send_diagnosis_reports_busy_heartbeat_state(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    _seed_local("alpha", a2a_port=12345)
    _record_heartbeat("alpha", "working", ts=time.time())
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["diagnosis"]["heartbeat_state"] == "working"


def test_agent_send_diagnosis_busy_likely_cause_says_in_progress(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — real listener so the port is reachable and the heartbeat
    # state (working) is what drives likely_causes.
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        _record_heartbeat("alpha", "working", ts=time.time())
        # Act
        with _swap_post_turn(fake_post):
            result = send_to_agent(
                "alpha",
                "hi",
                timeout_seconds=2,
                wait=True,
                lead_creds_path=fresh_lead_creds_path,
            )
    # Assert
    assert "still" in result["diagnosis"]["likely_causes"].lower()


def test_agent_send_diagnosis_stale_heartbeat_likely_cause_says_dead(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — real listener (port reachable) but heartbeat far older
    # than the staleness window, so "stale/dead" is the deciding factor.
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        _record_heartbeat("alpha", "idle", ts=time.time() - 600)
        # Act
        with _swap_post_turn(fake_post):
            result = send_to_agent(
                "alpha",
                "hi",
                timeout_seconds=2,
                wait=True,
                lead_creds_path=fresh_lead_creds_path,
            )
    # Assert
    assert "dead or hung" in result["diagnosis"]["likely_causes"]


def test_agent_send_diagnosis_dead_pid_reports_pid_not_alive(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — a pid that is essentially guaranteed not to exist.
    dead_pid = 2_147_483_646
    _seed_local_with_pid("alpha", a2a_port=12345, pid=dead_pid)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["diagnosis"]["pid_alive"] is False


def test_agent_send_diagnosis_port_unreachable_when_nothing_listening(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — a2a_port that no process is listening on. We grab a free
    # port from the OS and immediately release it so the connect refuses.
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free_port = s.getsockname()[1]
    s.close()
    _seed_local("alpha", a2a_port=free_port)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    with _swap_post_turn(fake_post):
        result = send_to_agent(
            "alpha",
            "hi",
            timeout_seconds=2,
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
        )
    # Assert
    assert result["diagnosis"]["port_reachable"] is False


def test_agent_send_diagnosis_port_reachable_when_listener_bound(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — bind a real listener so the diagnosis sees a live port.
    import socket as _socket

    srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    bound_port = srv.getsockname()[1]
    _seed_local("alpha", a2a_port=bound_port)
    from scitex_agent_container._network.peer import PeerError

    def fake_post(url, text, *, timeout_s):
        raise PeerError("peer timeout at <url> after 2s")

    # Act
    try:
        with _swap_post_turn(fake_post):
            result = send_to_agent(
                "alpha",
                "hi",
                timeout_seconds=2,
                wait=True,
                lead_creds_path=fresh_lead_creds_path,
            )
    finally:
        srv.close()
    # Assert
    assert result["diagnosis"]["port_reachable"] is True


# ---------------------------------------------------------------------------
# Non-blocking dispatch (default wait=False)
#
# The default path validates reachability and returns PROMPTLY with
# status="dispatched" + a backgroundable track_command, WITHOUT POSTing
# the blocking turn. A swapped ``_post_turn`` that records every call is
# used to PROVE the blocking POST never fires in non-blocking mode (it
# would hang the caller). Reachability uses a REAL bound listener so the
# diagnosis sees port_reachable=True — no mocks.
# ---------------------------------------------------------------------------


@contextmanager
def _exploding_post_turn() -> Iterator[list]:
    """Swap ``_post_turn`` with a recorder that FAILS if ever called.

    Yields the call-log list. The non-blocking path must never invoke
    the blocking POST; calling this stand-in raises so a regression that
    re-introduces the blocking call surfaces loudly in the test.
    """
    calls: list = []

    def recorder(url, text, *, timeout_s):
        calls.append(url)
        raise AssertionError("blocking _post_turn must NOT run in non-blocking mode")

    with _swap_post_turn(recorder):
        yield calls


def test_agent_send_nonblocking_returns_dispatched_status(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — real bound listener so the sidecar port is reachable.
    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        # Act
        with _exploding_post_turn():
            result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["status"] == "dispatched"


def test_agent_send_nonblocking_does_not_fire_blocking_post(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        # Act
        with _exploding_post_turn() as calls:
            send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert — the blocking POST recorder was never reached.
    assert calls == []


def test_agent_send_nonblocking_payload_carries_track_command(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        # Act
        with _exploding_post_turn():
            result = send_to_agent(
                "alpha", "now bump the threshold", lead_creds_path=fresh_lead_creds_path
            )
    # Assert
    assert result["track_command"] == "sac agents send alpha 'now bump the threshold'"


def test_agent_send_nonblocking_track_command_argv_is_a_list(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        # Act
        with _exploding_post_turn():
            result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["track_command_argv"] == ["sac", "agents", "send", "alpha", "hi"]


def test_agent_send_nonblocking_reports_delivered_subscriber_count(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    with _real_listener() as port:
        _seed_local("alpha", a2a_port=port)
        # Act
        with _exploding_post_turn():
            result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["delivered_subscriber_count"] == 1


def test_agent_send_nonblocking_fails_loud_when_port_unreachable(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — grab then release a port so nothing is listening on it.
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    _seed_local("alpha", a2a_port=dead_port)
    # Act
    with _exploding_post_turn():
        result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert — demonstrable unreachability is a loud error, not "dispatched".
    assert result["status"] == "error"


def test_agent_send_nonblocking_port_unreachable_error_carries_diagnosis(
    state_db_env, fresh_lead_creds_path
):
    # Arrange
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    _seed_local("alpha", a2a_port=dead_port)
    # Act
    with _exploding_post_turn():
        result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["diagnosis"]["port_reachable"] is False


def test_agent_send_nonblocking_fails_loud_when_dead_pid(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — a recorded pid that is essentially guaranteed not to exist.
    dead_pid = 2_147_483_646
    _seed_local_with_pid("alpha", a2a_port=12345, pid=dead_pid)
    # Act
    with _exploding_post_turn():
        result = send_to_agent("alpha", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["status"] == "error"


def test_agent_send_nonblocking_not_running_still_errors(state_db_env):
    # Arrange — no rows seeded (mode-independent failure, before dispatch).
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Registry split-brain regression (2026-06-19, figrecipe→beta).
#
# A locally-running agent whose ``instances`` row went STALE (the
# health-monitor restart calls ``runtime.start`` directly, never
# re-running ``agent_start``/``record_local_instance``; the
# stale-lease sweep also ends rows) used to make ``agent_send`` report
# ``status=error`` / ``a2a_port=null`` — even though the agent was live
# and ``a2a_send`` reached it on the bus. The fix resolves the endpoint
# from the DURABLE ``port_allocator`` claim (released only at stop /
# --force) when no active instances row carries a port, matching how
# the listen forwarder + peer resolver already resolve.
#
# These tests seed ONLY a real ``a2a_ports`` claim (no instances row)
# to reproduce the split-brain, and a REAL bound listener so the
# reachability gate sees a live sidecar — no mocks.
# ---------------------------------------------------------------------------


def _seed_port_claim(name: str, port: int) -> None:
    """Insert a real durable allocator claim (no instances row)."""
    from scitex_agent_container._state.port_allocator import claim_port

    claim_port(name, explicit=port)


def test_agent_send_reaches_agent_with_only_allocator_claim_nonblocking(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — NO instances row; only a durable allocator claim on a
    # REAL bound port (the post-restart split-brain state).
    with _real_listener() as port:
        _seed_port_claim("beta", port)
        # Act
        with _exploding_post_turn():
            result = send_to_agent("beta", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert — dispatched, not the old misleading "agent not running".
    assert result["status"] == "dispatched"


def test_agent_send_allocator_claim_url_uses_claimed_port_blocking(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — NO instances row; only an allocator claim. The blocking
    # POST must target the CLAIMED port's loopback /v1/turn.
    _seed_port_claim("beta", 19007)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent("beta", "hi", wait=True, lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert captured["url"] == "http://127.0.0.1:19007/v1/turn"


def test_agent_send_allocator_claim_diagnosis_reports_running(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — only an allocator claim (no row) on a port nothing is
    # listening on, so the dispatch fails its reachability gate and we
    # can inspect the attached diagnosis. The agent is RUNNING (the claim
    # proves it), so the diagnosis must NOT repeat the split-brain lie of
    # "stopped".
    import socket as _socket

    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    dead_port = s.getsockname()[1]
    s.close()
    _seed_port_claim("beta", dead_port)
    # Act
    with _exploding_post_turn():
        result = send_to_agent("beta", "hi", lead_creds_path=fresh_lead_creds_path)
    # Assert
    assert result["diagnosis"]["registry_status"] == "running"


def test_agent_send_genuinely_absent_still_reports_not_running(state_db_env):
    # Arrange — neither a row NOR a claim: the agent is genuinely gone.
    # The split-brain fix must not paper over a real "not running".
    # Act
    result = send_to_agent("ghost", "hi")
    # Assert
    assert "not running" in result["error"]
