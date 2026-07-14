"""End-to-end tests for ACL-deny synthetic-notification (sac-comms item D).

Lead a2a ``c42b3e3c`` (merged with
``lead-sac-acl-blocked-attempt-notification``). When an outbound
``a2a_send(sender, target)`` is ACL-denied, the receiver gets a
synthetic system-level notification (ACL-bypassing) embedding the
exact ``sac a2a grant <sender> <target>`` command so the operator
can grant proactively. Rate-limited per (sender, target) pair
(default cool-down 30 min, env-overridable via
``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S``).

REPLACES the prior parent/child auto-grant policy.

No-mocks (PA-306): real on-disk state.db, real Starlette TestClient,
real deny scenario (a per-spec ``inbound.siblings=deny`` sender →
target — the surviving deny path now that cross-group messaging is
default-allow). AAA markers (TQ002), one assert per test (TQ007),
3+-word test names.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from starlette.testclient import TestClient

from scitex_agent_container._listen.server import create_app
from scitex_agent_container._runners import _session_state as _ss
from scitex_agent_container._state import registry as _reg
from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_acl_deny_notify import (
    last_notified_at,
)
from scitex_agent_container._state.state_db_channel import list_undelivered
from scitex_agent_container._state.state_db_nodes import (
    record_comms_policy,
    record_lineage,
)

_TOKEN = "test-token-acl-deny-synthetic-notify"


@pytest.fixture
def isolated_state(tmp_path: Path) -> Iterator[Path]:
    db = tmp_path / "state.db"
    saved_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_default = state_db.DEFAULT_DB_PATH
    saved_home = os.environ.get("HOME")
    saved_reg_const = _reg.REGISTRY_DIR
    saved_state_const = _ss.DEFAULT_STATE_ROOT
    saved_cooldown_env = os.environ.get("SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    state_db.DEFAULT_DB_PATH = db
    state_db.init_schema(db)
    os.environ["HOME"] = str(tmp_path)
    _reg.REGISTRY_DIR = tmp_path / "registry"
    _ss.DEFAULT_STATE_ROOT = tmp_path / "runtime"
    # Set the cool-down to ZERO so the second post in a single test
    # always elapses the window (the fast-test path — the per-pair
    # rate-limit primitive itself has a dedicated unit-test file
    # that drives ``now=`` to exercise the throttle).
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "0"
    try:
        # Seed the canonical deny scenario item D fixes. Since messaging
        # is now DEFAULT-ALLOW cross-group (operator 2026-07-03), the
        # synthetic ACL-deny notification is exercised via the surviving
        # deny path: ``worker-a`` and ``lead`` are siblings under a shared
        # root and ``lead`` carries a per-spec ``inbound.siblings=deny``,
        # so ``worker-a → lead`` is denied.
        record_lineage(child="worker-a", parent="root", db_path=db)
        record_lineage(child="lead", parent="root", db_path=db)
        record_comms_policy(name="lead", inbound_siblings="deny", db_path=db)
        yield db
    finally:
        state_db.DEFAULT_DB_PATH = saved_default
        _reg.REGISTRY_DIR = saved_reg_const
        _ss.DEFAULT_STATE_ROOT = saved_state_const
        if saved_env is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_STATE_DB", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = saved_env
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home
        if saved_cooldown_env is None:
            os.environ.pop("SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S", None)
        else:
            os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = saved_cooldown_env


def _send_payload(sender: str, content: str = "hi") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": "1",
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": "m1",
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": {"from_agent": sender},
        },
    }


def _synthetic_rows(target: str, db_path: Path) -> list[dict]:
    return [
        r
        for r in list_undelivered(target=target, db_path=db_path)
        if r["event"].get("kind") == "acl_deny_notify"
    ]


# ---------------------------------------------------------------------------
# Cross-group deny publishes the synthetic notification at the target.
# ---------------------------------------------------------------------------


def test_cross_group_deny_publishes_synthetic_notification(
    isolated_state: Path,
) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    # Assert — the receiver MUST see a synthetic ACL-deny frame.
    assert len(rows) == 1


def test_synthetic_notification_sender_is_daemon(
    isolated_state: Path,
) -> None:
    # Arrange — the frame must not appear to come from the sender
    # (that would impersonate a granted peer at the receiver). As a sac
    # daemon-originated frame it carries sender ``from_agent="daemon"``
    # so the receiving agent's channel tag renders ``<- sac [daemon]``
    # (operator directive 2026-07-05, bracket form).
    from scitex_agent_container.a2a._inbox_bus import DAEMON_SENDER

    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    # Assert
    assert rows[0]["event"]["from_agent"] == DAEMON_SENDER


def test_synthetic_notification_sender_literal_is_daemon(
    isolated_state: Path,
) -> None:
    # Arrange — pin the literal wire value so a rename of the constant
    # can't silently drift the on-the-wire sender the operator's bracket
    # convention depends on. No package-suffixed source token.
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    # Assert
    assert rows[0]["event"]["from_agent"] == "daemon"


def test_synthetic_notification_embeds_grant_command(
    isolated_state: Path,
) -> None:
    # Arrange — operator-actionable: the content MUST embed the exact
    # CLI to grant if the attempt was intended.
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    content = rows[0]["event"].get("content") or ""
    # Assert
    assert "sac a2a grant worker-a lead" in content


def test_synthetic_notification_does_not_leak_message_body(
    isolated_state: Path,
) -> None:
    # Arrange — receiver decides on identity; the synthetic frame
    # MUST NOT reveal the denied body pre-decision.
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a", content="SECRET-PAYLOAD"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    content = rows[0]["event"].get("content") or ""
    # Assert
    assert "SECRET-PAYLOAD" not in content


def test_synthetic_notification_extra_carries_grant_command(
    isolated_state: Path,
) -> None:
    # Arrange — structured field for richer clients.
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    extra = rows[0]["event"].get("extra") or {}
    # Assert
    assert extra.get("grant_command") == "sac a2a grant worker-a lead"


# ---------------------------------------------------------------------------
# Rate-limit log is touched on every deny attempt admitted.
# ---------------------------------------------------------------------------


def test_deny_records_rate_limit_log_timestamp(
    isolated_state: Path,
) -> None:
    # Arrange
    app = create_app(token=_TOKEN)
    # Act
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    stamp = last_notified_at(sender="worker-a", target="lead", db_path=isolated_state)
    # Assert — admit MUST have written a row to the rate-limit log.
    assert stamp is not None


# ---------------------------------------------------------------------------
# Rate-limit throttle: a non-zero cool-down silences the second deny
# inside the window. Exercised via the per-pair primitive directly so
# the test does not need to wait wall-clock seconds.
# ---------------------------------------------------------------------------


def test_second_deny_inside_cooldown_publishes_no_extra_synthetic(
    isolated_state: Path,
) -> None:
    # Arrange — set a long cool-down so the second post is throttled.
    os.environ["SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S"] = "3600"
    app = create_app(token=_TOKEN)
    # Act — two denies back-to-back; only the first should publish
    # a synthetic frame.
    with TestClient(app) as client:
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
        client.post(
            "/agents/lead/message:send",
            json=_send_payload("worker-a"),
            headers={"authorization": f"Bearer {_TOKEN}"},
        )
    rows = _synthetic_rows("lead", isolated_state)
    # Assert — exactly ONE synthetic frame within the window.
    assert len(rows) == 1
