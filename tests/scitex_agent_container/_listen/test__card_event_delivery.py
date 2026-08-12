"""Unit tests for the C10 ``scitex_todo.hooks`` consumer.

Mirrors ``src/scitex_agent_container/_listen/_card_event_delivery.py``
(PS-204 §2).

What this proves
================
:func:`deliver_card_event` is sac's consumer on scitex-todo's shared
card-event bus. It MUST:

* FILTER — deliver on a card-event kind; NO-OP on an unrecognized kind,
  most importantly sac's OWN liveness-tick anomaly events on the same
  bus (which carry ``reason`` / ``severity``, not a card-event kind). If
  the two flows cross-wired, sac would POST its own anomalies back into
  agents' inboxes.
* RESOLVE — target = card owner + collaborators + subscribers.
* DELIVER — one ``/v1/notify`` POST per target to the local daemon.
* DEGRADE — never raise; a per-target failure is logged + skipped.

No mocks (STX-NM002)
====================
The filter / resolve logic is asserted on REAL event dicts. The delivery
half is exercised against a REAL local HTTP server bound to a REAL
ephemeral loopback port that records the POSTs it receives — we point
``SAC_LISTEN_BASE_URL`` at it (via a real ``os.environ`` set/restore
fixture, NOT monkeypatch) and assert the consumer made the right real
HTTP calls. The graceful-degrade test uses the SAME real server, which
answers ``500`` for one specific agent name and ``200`` for the rest —
real per-target failure, nothing about production is rewritten.

TQ: AAA markers (TQ002); 3+-word names; the HTTP-server / env fixtures
are FUNCTION scoped (TQ004) and ``yield`` their resources (TQ005).
"""

from __future__ import annotations

import http.server
import json
import os
import threading
from dataclasses import dataclass, field

import pytest

from scitex_agent_container._listen._card_event_delivery import (
    _resolve_bearer,
    deliver_card_event,
    resolve_targets,
)

# Agent name the fake daemon is hard-wired to FAIL for (500). Used by the
# graceful-degrade test to produce a real per-target failure with no mock.
_FLAKY_AGENT = "flaky-agent"


# ---------------------------------------------------------------------------
# Filter + resolve: pure logic on real event dicts (no IO).
# ---------------------------------------------------------------------------


def test_anomaly_event_is_ignored_no_op() -> None:
    # Arrange — sac's OWN liveness-tick anomaly shape on the shared bus:
    # ``reason``/``severity``, NO card-event kind. (If this were delivered,
    # sac would push its own anomalies into an agent's inbox.)
    anomaly = {
        "agent": "worker-x",
        "card_id": "card-1",
        "reason": "owner-not-live",
        "severity": "critical",
        "ts": 1.0,
    }
    # Act
    delivered = deliver_card_event(anomaly)
    # Assert — recognised as not-a-card-event → no delivery.
    assert delivered == 0


def test_unknown_kind_is_ignored_no_op() -> None:
    # Arrange — a kind sac does not handle.
    event = {"kind": "archived", "owner": "worker-x"}
    # Act
    delivered = deliver_card_event(event)
    # Assert
    assert delivered == 0


def test_non_dict_event_is_ignored_no_op() -> None:
    # Arrange — a malformed (non-dict) payload on the bus.
    payload = "not-a-dict"
    # Act — swallowed, never raises into the producer.
    delivered = deliver_card_event(payload)
    # Assert
    assert delivered == 0


def test_resolve_targets_unions_owner_collaborators_subscribers() -> None:
    # Arrange — the three target sources, with a duplicate to dedup.
    event = {
        "kind": "commented",
        "owner": "alice",
        "collaborators": ["bob", "alice"],
        "subscribers": ["carol"],
    }
    # Act
    targets = resolve_targets(event)
    # Assert — order-preserving union, deduped.
    assert targets == ["alice", "bob", "carol"]


def test_resolve_targets_accepts_member_dicts() -> None:
    # Arrange — a producer that ships richer member objects.
    event = {
        "kind": "reassigned",
        "assignee": {"name": "dave"},
        "subscribers": [{"agent": "erin"}],
    }
    # Act
    targets = resolve_targets(event)
    # Assert
    assert targets == ["dave", "erin"]


# ---------------------------------------------------------------------------
# Delivery: against a REAL local HTTP server standing in for /v1/notify.
# ---------------------------------------------------------------------------


@dataclass
class _Daemon:
    base_url: str
    posts: list[dict] = field(default_factory=list)


@pytest.fixture
def fake_notify_daemon():
    """Run a REAL HTTP server that records ``/v1/notify`` POSTs.

    Stands in for the local ``sac listen`` daemon: a real socket on a real
    ephemeral loopback port. It answers ``500`` when the posted body's
    ``agent`` equals :data:`_FLAKY_AGENT` and ``200`` otherwise — a real
    per-target failure path for the graceful-degrade test, with nothing
    about production rewritten.

    Points ``SAC_LISTEN_BASE_URL`` / ``SAC_LISTEN_BEARER`` at it via a
    real ``os.environ`` set/restore (NOT monkeypatch). Function-scoped
    (TQ004); ``yield``s the running daemon handle (TQ005) and tears the
    server + env down afterwards.
    """
    recorder_posts: list[dict] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 (http.server API)
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8"))
            except (
                Exception
            ):  # stx-allow: fallback (reason: stub records whatever arrives)
                body = {"_raw": raw.decode("utf-8", "replace")}
            recorder_posts.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            fail = isinstance(body, dict) and body.get("agent") == _FLAKY_AGENT
            payload = b'{"ok": false}' if fail else b'{"ok": true}'
            self.send_response(500 if fail else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):  # silence default stderr spam
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    saved_base = os.environ.get("SAC_LISTEN_BASE_URL")
    saved_bearer = os.environ.get("SAC_LISTEN_BEARER")
    os.environ["SAC_LISTEN_BASE_URL"] = f"http://127.0.0.1:{port}"
    os.environ["SAC_LISTEN_BEARER"] = "test-c10-bearer"
    try:
        yield _Daemon(base_url=f"http://127.0.0.1:{port}", posts=recorder_posts)
    finally:
        for key, val in (
            ("SAC_LISTEN_BASE_URL", saved_base),
            ("SAC_LISTEN_BEARER", saved_bearer),
        ):
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        server.shutdown()
        server.server_close()


def test_commented_event_delivers_to_owner(fake_notify_daemon) -> None:
    # Arrange — a real card-event naming one owner agent.
    event = {"kind": "commented", "owner": "worker-x", "card_id": "card-7"}
    # Act
    delivered = deliver_card_event(event)
    # Assert — exactly one real POST landed at the daemon, addressed to the
    # owner agent.
    assert delivered == 1 and fake_notify_daemon.posts[0]["body"]["agent"] == (
        "worker-x"
    )


def test_delivery_carries_bearer_and_card_id(fake_notify_daemon) -> None:
    # Arrange
    event = {"kind": "completed", "owner": "worker-x", "card_id": "card-7"}
    # Act
    deliver_card_event(event)
    post = fake_notify_daemon.posts[0]
    # Assert — bearer header + card_id threaded onto the /v1/notify body.
    assert (
        post["authorization"] == "Bearer test-c10-bearer"
        and post["body"]["card_id"] == "card-7"
    )


def test_delivery_posts_to_each_unique_target(fake_notify_daemon) -> None:
    # Arrange — owner + collaborator + subscriber = three unique agents.
    event = {
        "kind": "status_changed",
        "owner": "alice",
        "collaborators": ["bob"],
        "subscribers": ["carol"],
        "card_id": "card-9",
    }
    # Act
    delivered = deliver_card_event(event)
    agents = sorted(p["body"]["agent"] for p in fake_notify_daemon.posts)
    # Assert
    assert delivered == 3 and agents == ["alice", "bob", "carol"]


def test_one_bad_target_does_not_abort_the_rest(fake_notify_daemon) -> None:
    # Arrange — the daemon returns a real 500 for ``_FLAKY_AGENT`` and 200
    # for the rest, so the first target genuinely fails over the wire.
    event = {
        "kind": "commented",
        "owner": _FLAKY_AGENT,
        "collaborators": ["bob"],
        "card_id": "card-9",
    }
    # Act — must NOT raise; the healthy target still gets delivered.
    delivered = deliver_card_event(event)
    # Assert — one success (bob); the 500 for the flaky owner is swallowed.
    assert delivered == 1


# ---------------------------------------------------------------------------
# Bearer resolution must match every other sac client
#
# This module read ``os.environ.get("SAC_LISTEN_BEARER")`` directly — the SHORT
# spelling only — while every other client goes through ``_env.getenv``, which
# accepts ``SAC_<NAME>`` AND ``SCITEX_AGENT_CONTAINER_<NAME>``. A deployment
# setting only the long form therefore authenticated everywhere else and 401'd
# here alone. Its own docstring claimed the opposite ("what every other sac
# client reads"), which is why the divergence looked deliberate.
# ---------------------------------------------------------------------------

_BEARER_KEYS = (
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
)


@pytest.fixture
def isolated_bearer_env(tmp_path):
    """Clear BOTH env spellings and redirect HOME to a clean tmp dir.

    Clearing both matters: a stray value in the operator's shell would make the
    must-read-the-long-form test pass for the wrong reason. HOME is redirected
    so the token-file fallback reads an isolated dir, never the real token.
    """
    saved = {k: os.environ.get(k) for k in _BEARER_KEYS}
    saved_home = os.environ.get("HOME")
    for k in _BEARER_KEYS:
        os.environ.pop(k, None)
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _write_host_token_file(home, token: str) -> None:
    from scitex_agent_container._listen.tokens import default_token_path

    path = default_token_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def test_the_long_form_bearer_env_prefix_is_honoured(isolated_bearer_env) -> None:
    """The regression: this spelling was invisible here, and worked elsewhere."""
    # Arrange
    os.environ["SCITEX_AGENT_CONTAINER_LISTEN_BEARER"] = "long-form-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "long-form-tok"


def test_the_short_form_bearer_env_prefix_still_works(isolated_bearer_env) -> None:
    # Arrange
    os.environ["SAC_LISTEN_BEARER"] = "short-form-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "short-form-tok"


def test_the_host_token_file_fallback_still_works(isolated_bearer_env) -> None:
    # Arrange — no env at all; a real token file on disk.
    _write_host_token_file(isolated_bearer_env, "file-tok-card-event")
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "file-tok-card-event"


def test_an_env_bearer_still_wins_over_the_token_file(isolated_bearer_env) -> None:
    # Arrange — both present; the env must win.
    _write_host_token_file(isolated_bearer_env, "file-tok")
    os.environ["SAC_LISTEN_BEARER"] = "env-tok"
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved == "env-tok"


def test_no_bearer_env_and_no_file_resolves_to_none(isolated_bearer_env) -> None:
    """Absent stays non-fatal — the POST goes out unauthenticated and 401s
    loudly in the per-target log line, which is the documented behaviour."""
    # Arrange — cleared env, no token file written.
    # Act
    resolved = _resolve_bearer()
    # Assert
    assert resolved is None
