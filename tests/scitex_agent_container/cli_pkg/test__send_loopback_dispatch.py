"""Blast-radius guard: a LOOPBACK peer host must dispatch locally, not over ssh.

Why this file exists
====================
``derive_turn_url`` now names ``127.0.0.1`` for a LOCAL agent instead of
the machine's canonical hostname (which on Debian / Ubuntu / WSL resolves
to ``127.0.1.1`` and is refused by the sidecar bound on ``127.0.0.1``).

``_send`` consumes that URL: the BROKERED lookup an in-container agent
uses parses the registry's ``turn_url`` back into a host
(``_send_broker._host_from_turn_url``) and hands it to ``send_to_agent``
as ``endpoint.host``. The dispatch branch then read::

    peer_host = endpoint.host if endpoint.host != current_host else ""
    ...
    if peer_host and peer_host != current_host:
        url = f"ssh://{peer_host}:{a2a_port}/v1/turn"

A bare string-compare against ``current_host`` would classify the new
``127.0.0.1`` literal as a REMOTE peer and try to **ssh to
``ssh://127.0.0.1:<port>``** — turning a URL fix into a comms outage.
The classification is therefore loopback-aware (``is_local_host``), and
these tests pin that, in both directions: a loopback host stays LOCAL, a
genuinely remote host still routes over ssh.

PA-306 / STX-NM002: no ``unittest.mock``, no ``monkeypatch`` — the
collaborator is swapped at the module namespace via a real save/restore
context manager.

AAA + >=3-word names + one assert per test (STX-TQ002 / PA-307).
"""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

import scitex_agent_container.cli_pkg._send as _send_mod
from scitex_agent_container.cli_pkg._send import send_to_agent


@contextmanager
def _swap_post_turn(fn) -> Iterator[None]:
    """Replace ``_send._post_turn`` with ``fn`` for the duration of the test."""
    saved = _send_mod._post_turn
    _send_mod._post_turn = fn  # type: ignore[assignment]
    try:
        yield
    finally:
        _send_mod._post_turn = saved  # type: ignore[assignment]


@pytest.fixture
def fresh_lead_creds_path(tmp_path) -> Path:
    """A fresh, non-expired OAuth credentials JSON so the preflight passes."""
    creds = tmp_path / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-oat-fake",
                    "refreshToken": "sk-ant-ort-fake",
                    "expiresAt": int((time.time() + 3600) * 1000),
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                }
            }
        ),
        encoding="utf-8",
    )
    return creds


@pytest.fixture
def state_db_env(tmp_path):
    """Redirect state.db + SAC_HOST so the test NEVER touches the live registry."""
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


def _seed(name: str, host: str, a2a_port: int) -> None:
    """Record an active instance row for ``name`` on ``host``."""
    from scitex_agent_container._state.state_db import record_instance_start

    record_instance_start(name=name, host=host, a2a_port=a2a_port)


def _ok_ssh_runner(peer_host, remote_creds_path):
    """Stub ssh probe — rc=0 so the cross-host preflight cannot hang on real ssh."""
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


# ---------------------------------------------------------------------------
# A loopback peer host is LOCAL — never ssh
# ---------------------------------------------------------------------------


def test_loopback_host_dispatches_over_loopback(state_db_env, fresh_lead_creds_path):
    # Arrange — the endpoint host is the loopback literal the fixed
    # derive_turn_url now yields for a local agent.
    _seed("gamma", host="127.0.0.1", a2a_port=19017)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent(
            "gamma",
            "hi",
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
            ssh_runner=_ok_ssh_runner,
        )
    # Assert
    assert captured["url"] == "http://127.0.0.1:19017/v1/turn"


def test_loopback_host_never_routes_through_ssh(state_db_env, fresh_lead_creds_path):
    # Arrange — the failure this guards: ssh://127.0.0.1:19017/v1/turn.
    _seed("gamma", host="127.0.0.1", a2a_port=19017)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent(
            "gamma",
            "hi",
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
            ssh_runner=_ok_ssh_runner,
        )
    # Assert
    assert not captured["url"].startswith("ssh://")


def test_debian_self_host_ip_dispatches_over_loopback(
    state_db_env, fresh_lead_creds_path
):
    # Arrange — 127.0.1.1 is loopback too. Even if a stale row still carries
    # it, it must never be treated as a remote peer to ssh into.
    _seed("delta", host="127.0.1.1", a2a_port=19018)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent(
            "delta",
            "hi",
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
            ssh_runner=_ok_ssh_runner,
        )
    # Assert
    assert captured["url"] == "http://127.0.0.1:19018/v1/turn"


# ---------------------------------------------------------------------------
# A genuinely remote peer STILL routes over ssh (no over-correction)
# ---------------------------------------------------------------------------


def test_remote_host_still_routes_through_ssh(state_db_env, fresh_lead_creds_path):
    # Arrange — the loopback-awareness must not swallow real cross-host
    # dispatch: rewriting a remote peer to loopback would point every
    # cross-host send back at ourselves.
    _seed("beta", host="peer-x", a2a_port=18888)
    captured: dict = {}

    def fake_post(url, text, *, timeout_s):
        captured["url"] = url
        return ("ok", {})

    # Act
    with _swap_post_turn(fake_post):
        send_to_agent(
            "beta",
            "hi",
            wait=True,
            lead_creds_path=fresh_lead_creds_path,
            ssh_runner=_ok_ssh_runner,
        )
    # Assert
    assert captured["url"] == "ssh://peer-x:18888/v1/turn"
