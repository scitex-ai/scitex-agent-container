"""Honest-504-body tests for ``POST /v1/turn``.

A 504 from the inbound-turn endpoint means the BOUNDED HTTP wait
elapsed — NOT that the turn failed. The SDK call is never cancelled, so
the turn is usually still queued/draining. The earlier body hardcoded
an optimistic framing ("loud failure" in the docstring, a bare
``error`` string in the body). This module pins the contract that the
504 body now reports the turn's REAL state read from the agent's
``heartbeat.json``:

* ``is_failure`` is COMPUTED from heartbeat-beat staleness — a fresh
  beat means "still progressing", a stale beat (older than 2x the
  heartbeat tick) means "possibly wedged". It is never hardcoded.
* ``detail`` reflects the actual phase + beat age, not a canned label.
* a missing / unreadable ``heartbeat.json`` is reported honestly as
  "state unknown", never fabricated as progress.
* the ``status`` / ``timeout_s`` / ``session_id`` / ``heartbeat`` fields
  carry through, and the legacy ``error`` alias is preserved so old
  callers keep working.

No mocks, no monkeypatch (STX-NM): every test writes a REAL
``heartbeat.json`` into a ``tmp_path`` state dir and asserts the real
``_build_timeout_body`` / real ``serve_inbound`` output. AAA structure
(STX-TQ002) with one substantive assert per test (STX-TQ007).
"""

from __future__ import annotations

import asyncio
import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from scitex_agent_container._runners._session_http import (
    STALL_TICK_FACTOR,
    _build_timeout_body,
    serve_inbound,
)
from scitex_agent_container._runners._session_inbox import (
    ShutdownEnvelope,
    TurnEnvelope,
    make_inbox,
)
from scitex_agent_container._runners._session_state import write_heartbeat

# ---------------------------------------------------------------------------
# Real-collaborator helpers
# ---------------------------------------------------------------------------


def _write_heartbeat_with_age(state_dir: Path, *, state: str, age_s: float) -> None:
    """Write a real heartbeat.json whose ``ts`` is ``age_s`` in the past.

    Uses the real ``write_heartbeat`` then rewrites only ``ts`` so the
    on-disk shape matches production exactly while letting the test
    control beat age deterministically.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    write_heartbeat(state_dir, pid=4_321, state=state)
    hb_path = state_dir / "heartbeat.json"
    record = json.loads(hb_path.read_text(encoding="utf-8"))
    record["ts"] = time.time() - age_s
    hb_path.write_text(json.dumps(record), encoding="utf-8")


# ---------------------------------------------------------------------------
# _build_timeout_body — fresh beat means progressing, not failure
# ---------------------------------------------------------------------------


class TestFreshHeartbeatNotFailure:
    def test_fresh_heartbeat_is_not_flagged_as_failure(self, tmp_path) -> None:
        """A heartbeat written just now reads as still-progressing."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=0.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert body["is_failure"] is False

    def test_fresh_heartbeat_detail_mentions_alive(self, tmp_path) -> None:
        """The honest detail says the runner is alive, not 'failed'."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert "alive" in body["detail"]

    def test_fresh_heartbeat_embeds_real_phase(self, tmp_path) -> None:
        """The 504 body carries the real heartbeat phase from disk."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert body["heartbeat"]["state"] == "working"


# ---------------------------------------------------------------------------
# _build_timeout_body — stale beat means possible wedge, IS a failure
# ---------------------------------------------------------------------------


class TestStaleHeartbeatIsFailure:
    def test_stale_heartbeat_is_flagged_as_failure(self, tmp_path) -> None:
        """A beat older than 2x the tick reads as a possible wedge."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=100.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert body["is_failure"] is True

    def test_stale_heartbeat_detail_mentions_wedged(self, tmp_path) -> None:
        """The honest detail names the wedge / investigate signal."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=100.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert "wedged" in body["detail"]

    def test_beat_just_under_threshold_is_not_failure(self, tmp_path) -> None:
        """A beat younger than the stall threshold stays not-a-failure."""
        # Arrange
        tick = 10.0
        just_under = STALL_TICK_FACTOR * tick - 1.0
        _write_heartbeat_with_age(tmp_path, state="working", age_s=just_under)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=tick
        )
        # Assert
        assert body["is_failure"] is False


# ---------------------------------------------------------------------------
# _build_timeout_body — missing / unwired state is honest, never fabricated
# ---------------------------------------------------------------------------


class TestMissingStateIsHonest:
    def test_missing_heartbeat_is_not_failure(self, tmp_path) -> None:
        """No heartbeat.json on disk → unknown, not a hardcoded failure."""
        # Arrange
        empty_dir = tmp_path / "no-beat"
        empty_dir.mkdir()
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=empty_dir, tick_seconds=10.0
        )
        # Assert
        assert body["is_failure"] is False

    def test_missing_heartbeat_detail_says_unknown(self, tmp_path) -> None:
        """Missing heartbeat is reported honestly as state-unknown."""
        # Arrange
        empty_dir = tmp_path / "no-beat"
        empty_dir.mkdir()
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=empty_dir, tick_seconds=10.0
        )
        # Assert
        assert "unknown" in body["detail"]

    def test_missing_heartbeat_field_is_null(self, tmp_path) -> None:
        """The ``heartbeat`` body field is null when none exists on disk."""
        # Arrange
        empty_dir = tmp_path / "no-beat"
        empty_dir.mkdir()
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=empty_dir, tick_seconds=10.0
        )
        # Assert
        assert body["heartbeat"] is None

    def test_no_state_dir_detail_says_unavailable(self) -> None:
        """No state dir wired → body admits live state is unavailable."""
        # Arrange
        timeout_s = 120.0
        # Act
        body = _build_timeout_body(
            timeout_s=timeout_s, session_id=None, state_dir=None, tick_seconds=10.0
        )
        # Assert
        assert "unavailable" in body["detail"]


# ---------------------------------------------------------------------------
# _build_timeout_body — never hardcoded "still_working"
# ---------------------------------------------------------------------------


class TestNoHardcodedOptimisticLabel:
    def test_body_does_not_contain_still_working_literal(self, tmp_path) -> None:
        """The body must not stamp the hardcoded 'still_working' label."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert "still_working" not in json.dumps(body)


# ---------------------------------------------------------------------------
# _build_timeout_body — session_id falls back to the persisted real id
# ---------------------------------------------------------------------------


class TestSessionIdFallsBackToPersisted:
    def test_persisted_session_id_used_when_env_lacks_it(self, tmp_path) -> None:
        """When the envelope has no session_id, the persisted real id wins."""
        # Arrange
        from scitex_agent_container._runners._session_state import write_session_id

        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        write_session_id(tmp_path, "sid-from-disk")
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert body["session_id"] == "sid-from-disk"


# ---------------------------------------------------------------------------
# Back-compat — legacy ``error`` + ``timeout_s`` fields preserved
# ---------------------------------------------------------------------------


class TestLegacyFieldsPreserved:
    def test_legacy_error_field_carries_timeout_string(self, tmp_path) -> None:
        """Old consumers keying on ``error`` still see the timeout string."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        # Act
        body = _build_timeout_body(
            timeout_s=120.0, session_id=None, state_dir=tmp_path, tick_seconds=10.0
        )
        # Assert
        assert "120s timeout" in body["error"]


# ---------------------------------------------------------------------------
# End-to-end — the honest body flows through the real HTTP 504 handler
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Ask the kernel for an unused TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _wait_bound(port: int) -> None:
    """Poll until the TCP port accepts connections."""
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            await asyncio.sleep(0.05)
    pytest.fail(f"server never bound on port {port}")


def _http_post(url: str, body: bytes) -> tuple[int, dict | None]:
    """POST JSON to ``url`` — returns ``(status, parsed_body_or_None)``."""
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode())
        except (
            ValueError,
            OSError,
        ):  # stx-allow: fallback (reason: error body may be empty in test)
            payload = None
        return exc.code, payload


async def _consumer_never_resolves(inbox: "asyncio.Queue") -> None:
    """Pop envelopes and never resolve — mimics a turn still draining."""
    while True:
        env = await inbox.get()
        if isinstance(env, ShutdownEnvelope):
            return
        # Intentionally NOT resolving env.response — forces the 504 path.
        _ = isinstance(env, TurnEnvelope)


def _run_504_against_state_dir(
    *, port: int, state_dir: Path, cap: float
) -> tuple[int, dict | None]:
    """Drive one POST /v1/turn through the real sidecar with a state dir."""

    async def _client(p: int):
        return await asyncio.to_thread(
            _http_post,
            f"http://127.0.0.1:{p}/v1/turn",
            b'{"text": "wedge me"}',
        )

    async def _driver() -> tuple[int, dict | None]:
        inbox = make_inbox()
        stop = asyncio.Event()
        consumer = asyncio.create_task(_consumer_never_resolves(inbox))
        server = asyncio.create_task(
            serve_inbound(
                inbox,
                host="127.0.0.1",
                port=port,
                stop=stop,
                turn_timeout_s=cap,
                state_dir=state_dir,
                tick_seconds=10.0,
            )
        )
        try:
            await _wait_bound(port)
            result = await _client(port)
        finally:
            stop.set()
            await inbox.put(ShutdownEnvelope())
            try:
                await asyncio.wait_for(consumer, timeout=5.0)
            except asyncio.TimeoutError:
                consumer.cancel()
                try:
                    await consumer
                except (
                    asyncio.CancelledError,
                    Exception,
                ):  # stx-allow: fallback (reason: defensive teardown)
                    pass
            await asyncio.wait_for(server, timeout=5.0)
        return result

    return asyncio.run(_driver())


class TestEndToEndHonest504:
    def test_504_body_carries_real_heartbeat_state(self, tmp_path) -> None:
        """A live 504 over HTTP embeds the real on-disk heartbeat phase."""
        # Arrange
        _write_heartbeat_with_age(tmp_path, state="working", age_s=1.0)
        port = _free_port()
        # Act
        status, body = _run_504_against_state_dir(
            port=port, state_dir=tmp_path, cap=0.3
        )
        # Assert
        assert (status, body["heartbeat"]["state"]) == (504, "working")
