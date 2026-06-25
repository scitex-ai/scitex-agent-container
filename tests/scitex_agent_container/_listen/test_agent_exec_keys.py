"""Tests for the ``type: key`` listen handler (``_listen._agent_exec_keys``).

``POST /agents/<name>/send`` with ``{"type":"key", ...}`` routes:
  * cancel keys (ESC / C-c / SIGINT) → SIGINT the runner pid;
  * any other named key / sequence → tmux send-keys to the session.

These tests exercise the send-keys branch through the ``mux_resolver``
injection seam (a recording fake mux — no live tmux, no monkeypatch)
plus the validation / discrimination branches. The cancel-key branch's
delegation is asserted by proving it does NOT consult the mux resolver.

STX-TQ002 AAA-markers + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

import json

from scitex_agent_container._listen._agent_exec_keys import _handle_key_send


class _RecordingMux:
    """Fake multiplexer recording every ``send_keys`` it receives.

    ``exists`` returns a configurable verdict so the no-session 404
    branch is reachable; ``send_keys`` records ``(session, keys)``.
    """

    def __init__(self, *, session_exists: bool = True) -> None:
        self._exists = session_exists
        self.sent: list[tuple[str, tuple[str, ...]]] = []

    def exists(self, session: str) -> bool:
        return self._exists

    def send_keys(self, session: str, *keys: str) -> None:
        self.sent.append((session, keys))


def _make_resolver(session: str, mux: _RecordingMux):
    """Return a ``mux_resolver`` yielding the given session + mux."""

    def _resolve(_name: str) -> tuple[str, object]:
        return session, mux

    return _resolve


def _body(response) -> dict:
    """Decode a JSONResponse body to a dict."""
    return json.loads(bytes(response.body).decode("utf-8"))


# ---------------------------------------------------------------------------
# send-keys branch — single named key
# ---------------------------------------------------------------------------


class TestNamedKeyGoesToSendKeys:
    """A non-cancel named key is delivered via tmux send-keys."""

    def test_enter_routed_to_send_keys(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        _handle_key_send("foo", {"key": "Enter"}, mux_resolver=resolver)
        # Assert
        assert mux.sent == [("cld-foo", ("Enter",))]

    def test_response_route_is_send_keys(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        resp = _handle_key_send("foo", {"key": "Enter"}, mux_resolver=resolver)
        # Assert
        assert _body(resp)["route"] == "send-keys"

    def test_digit_routed_to_send_keys(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        _handle_key_send("foo", {"key": "2"}, mux_resolver=resolver)
        # Assert
        assert mux.sent == [("cld-foo", ("2",))]


# ---------------------------------------------------------------------------
# send-keys branch — sequence
# ---------------------------------------------------------------------------


class TestKeySequenceGoesToSendKeys:
    """A ``keys`` sequence is split, validated and sent in order."""

    def test_sequence_sent_in_order(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        _handle_key_send(
            "foo", {"keys": "Up Up Enter"}, mux_resolver=resolver
        )
        # Assert
        assert mux.sent == [("cld-foo", ("Up", "Up", "Enter"))]

    def test_esc_alias_in_sequence_canonicalised(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        _handle_key_send("foo", {"keys": "ESC Enter"}, mux_resolver=resolver)
        # Assert
        assert mux.sent == [("cld-foo", ("Escape", "Enter"))]


# ---------------------------------------------------------------------------
# cancel-key branch — does NOT consult the mux resolver
# ---------------------------------------------------------------------------


class TestCancelKeyDoesNotSendKeys:
    """ESC takes the interrupt path; the mux resolver is untouched."""

    def test_esc_never_calls_send_keys(self) -> None:
        # Arrange — a resolver that explodes proves it is never called.
        def _boom(_name: str):
            raise AssertionError("resolver must not be consulted for ESC")

        # Act
        resp = _handle_key_send("foo", {"key": "ESC"}, mux_resolver=_boom)
        # Assert — interrupt path returns 404 (no live pid in test env),
        # NOT an AssertionError from the resolver.
        assert resp.status_code in (404, 500)


# ---------------------------------------------------------------------------
# validation / discrimination
# ---------------------------------------------------------------------------


class TestUnknownKeyRejected:
    """An unknown key name is rejected 400 before any send."""

    def test_unknown_key_returns_400(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        resp = _handle_key_send(
            "foo", {"key": "Retrun"}, mux_resolver=resolver
        )
        # Assert
        assert resp.status_code == 400

    def test_unknown_key_does_not_send(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        _handle_key_send("foo", {"key": "Retrun"}, mux_resolver=resolver)
        # Assert
        assert mux.sent == []


class TestMissingKeyRejected:
    """A body with neither key nor keys is rejected 400."""

    def test_empty_body_returns_400(self) -> None:
        # Arrange
        mux = _RecordingMux()
        resolver = _make_resolver("cld-foo", mux)
        # Act
        resp = _handle_key_send("foo", {}, mux_resolver=resolver)
        # Assert
        assert resp.status_code == 400


class TestNoLiveSession:
    """A non-existent tmux session yields a loud 404."""

    def test_missing_session_returns_404(self) -> None:
        # Arrange
        mux = _RecordingMux(session_exists=False)
        resolver = _make_resolver("cld-foo", mux)
        # Act
        resp = _handle_key_send("foo", {"key": "Enter"}, mux_resolver=resolver)
        # Assert
        assert resp.status_code == 404


# EOF
