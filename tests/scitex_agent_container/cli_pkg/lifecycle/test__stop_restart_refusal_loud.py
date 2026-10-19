#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stop/restart refusals are loud: clear message + non-zero exit, always.

Operator incident 2026-10-19: ``sac stop`` / ``sac restart`` went SILENT
when the stop leg was refused (agent actively working) — no clear
message, and the restart halted mid-way with nothing on screen.

Root cause: refusal lines were rendered with ``render_rich`` at its
default ``"info"`` level (colour is not severity), so any
``SCITEX_LOGGING_LEVEL`` above INFO filtered them and the command
exited 1 with EMPTY stdout/stderr. The restart's error envelope also
carried no ``"restarted"`` key, so a machine consumer could not tell
"failed" from "unknown".

These tests pin the loud contract at BOTH the default INFO threshold
and an elevated WARNING threshold:

* ``sac agents stop`` refusal → exit 1, ``NOT stopped`` + remedy on
  console, ``{"stopped": false, "reason": ...}`` under ``--json``.
* ``sac agents restart`` refusal → exit 1, ``NOT restarted`` + remedy
  on console, ``{"restarted": false, "reason": ..., "hint": ...}``
  under ``--json``.
* A peer-side refusal over ssh is framed as a refusal quoting the
  peer's own words, not as an unreachable peer.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Collaborators are
swapped at the module namespace with an explicit save/restore
``_swap``. Fixture agent names use the ``pytest-fixture-`` prefix so
no real agent can ever be prefix-matched (see ``test__stop.py``).
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator

import pytest
import scitex_logging
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._restart as restart_mod
import scitex_agent_container.cli_pkg.lifecycle._restart_local as restart_local_mod
import scitex_agent_container.cli_pkg.lifecycle._stop as stop_mod
from scitex_agent_container._lifecycle._managed_turn_drain import (
    ManagedTurnDrainRefusal,
)
from scitex_agent_container.cli_pkg.lifecycle._restart import restart
from scitex_agent_container.cli_pkg.lifecycle._stop import stop

AGENT = "pytest-fixture-refusal"

REFUSAL_TEXT = (
    "Refusing to stop Hermes agent 'pytest-fixture-refusal': session "
    "'live-1' is 'working' after waiting 0.0s. Let the turn finish, or "
    "retry with `--drain-timeout SECONDS`. Tmux detach is always safe."
)


def _refusal() -> ManagedTurnDrainRefusal:
    return ManagedTurnDrainRefusal(REFUSAL_TEXT)


def _raise_refusal(*args, **kwargs):
    raise _refusal()


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path):
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@contextmanager
def _swap(mod, name: str, fn: object) -> Iterator[None]:
    saved = getattr(mod, name)
    setattr(mod, name, fn)
    try:
        yield
    finally:
        setattr(mod, name, saved)


@contextmanager
def _warning_threshold() -> Iterator[None]:
    """Raise the console threshold the way a noise-cutting fleet host does."""
    saved = scitex_logging.get_level()
    scitex_logging.set_level(scitex_logging.WARNING)
    try:
        yield
    finally:
        scitex_logging.set_level(saved)


def _drive_stop_local(argv: list[str]):
    """Run the stop CLI with every collaborator stubbed to the local path."""
    with (
        _swap(stop_mod, "resolve_with_prefix", lambda raw: f"/fake/{raw}.yaml"),
        _swap(stop_mod, "load_config", lambda path: SimpleNamespace(name=AGENT)),
        _swap(stop_mod, "try_dispatch_remote", lambda *a, **k: False),
        _swap(stop_mod, "spec_host_fallback_peer", lambda *a, **k: None),
        _swap(
            stop_mod,
            "_load_host_config",
            lambda: SimpleNamespace(peers={}),
        ),
        _swap(stop_mod, "agent_stop", _raise_refusal),
    ):
        return CliRunner().invoke(stop, argv)


def _drive_restart_local(argv: list[str]):
    """Run the restart CLI with the local leg raising a turn refusal."""
    with (
        _swap(restart_local_mod, "agent_restart", _raise_refusal),
        _swap(restart_local_mod, "try_dispatch_remote", lambda *a, **k: False),
        _swap(
            restart_local_mod, "spec_host_fallback_peer", lambda *a, **k: None
        ),
        _swap(
            restart_mod, "must_broker_to_host", lambda: False
        ),
    ):
        return CliRunner().invoke(restart, argv)


# ---------------------------------------------------------------------------
# sac agents stop — refusal is loud
# ---------------------------------------------------------------------------


def test_stop_refusal_exits_nonzero_with_clear_message():
    # Arrange / Act
    result = _drive_stop_local([AGENT])
    # Assert
    assert result.exit_code == 1
    assert f"NOT stopped: agent '{AGENT}' is actively working" in result.output
    assert "Nothing was torn down" in result.output
    assert "--drain-timeout" in result.output


def test_stop_refusal_stays_loud_above_info_threshold():
    # Arrange — a fleet host cutting noise with SCITEX_LOGGING_LEVEL=WARNING
    # Act
    with _warning_threshold():
        result = _drive_stop_local([AGENT])
    # Assert — previously: exit 1 with EMPTY stdout/stderr (the incident).
    assert result.exit_code == 1
    assert f"NOT stopped: agent '{AGENT}' is actively working" in result.output
    assert result.output.strip() != ""


def test_stop_refusal_json_envelope_names_failure():
    # Arrange / Act
    result = _drive_stop_local([AGENT, "--json"])
    # Assert
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["name"] == AGENT
    assert envelope["stopped"] is False
    assert envelope["reason"] == "stop-refused-active-turn"
    assert "working" in envelope["error"]


# ---------------------------------------------------------------------------
# sac agents restart — refusal aborts loud with reason, never silent
# ---------------------------------------------------------------------------


def test_restart_refusal_exits_nonzero_with_clear_message():
    # Arrange / Act
    result = _drive_restart_local([AGENT, "-y"])
    # Assert
    assert result.exit_code == 1
    assert f"Agent '{AGENT}' NOT restarted" in result.output
    assert "actively working" in result.output
    assert "still the OLD process" in result.output
    assert "--drain-timeout" in result.output


def test_restart_refusal_stays_loud_above_info_threshold():
    # Arrange / Act
    with _warning_threshold():
        result = _drive_restart_local([AGENT, "-y"])
    # Assert — previously: exit 1 with EMPTY stdout/stderr (the incident).
    assert result.exit_code == 1
    assert f"Agent '{AGENT}' NOT restarted" in result.output
    assert result.output.strip() != ""


def test_restart_refusal_json_envelope_carries_restarted_false():
    # Arrange / Act
    result = _drive_restart_local([AGENT, "-y", "--json"])
    # Assert
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["name"] == AGENT
    assert envelope["restarted"] is False
    assert envelope["reason"] == "stop-refused-active-turn"
    assert "--drain-timeout" in envelope["hint"]


def test_restart_generic_failure_envelope_carries_restarted_false():
    # Arrange — a non-refusal fault must not lose the restarted key either.
    def _boom(*args, **kwargs):
        raise RuntimeError("start leg exploded")

    with (
        _swap(restart_local_mod, "agent_restart", _boom),
        _swap(restart_local_mod, "try_dispatch_remote", lambda *a, **k: False),
        _swap(
            restart_local_mod, "spec_host_fallback_peer", lambda *a, **k: None
        ),
        _swap(restart_mod, "must_broker_to_host", lambda: False),
    ):
        result = CliRunner().invoke(restart, [AGENT, "-y", "--json"])
    # Assert
    assert result.exit_code == 1
    envelope = json.loads(result.stdout)
    assert envelope["restarted"] is False
    assert "start leg exploded" in envelope["error"]


# ---------------------------------------------------------------------------
# Cross-host: a peer-side refusal reads as a refusal, not a dead peer
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_remote_stop_refusal_framed_as_refusal_not_unreachable():
    # Arrange — the peer refused (rc=1) with its own JSON error envelope.
    peer_stdout = json.dumps(
        {
            "name": AGENT,
            "stopped": False,
            "reason": "stop-refused-active-turn",
            "error": REFUSAL_TEXT,
        }
    )
    stub_subprocess = SimpleNamespace(
        run=lambda *a, **k: _Proc(1, peer_stdout, "")
    )
    with (
        _swap(stop_mod, "subprocess", stub_subprocess),
        _swap(
            stop_mod,
            "build_ssh_argv",
            lambda peer, argv, peers: ["ssh", peer, "--", *argv],
        ),
    ):
        # Act
        try:
            stop_mod._dispatch_remote_stop("peer-1", {"id": "x"}, {}, AGENT)
            raised = None
        except Exception as exc:  # noqa: BLE001 — asserting on the raised shape
            raised = exc
    # Assert
    assert raised is not None
    assert "Peer refused the stop (NOT a transport failure)" in str(raised)
    assert "working" in str(raised)
    assert "stop-refused-active-turn" in str(raised)


def test_remote_restart_refusal_framed_as_refusal_not_transport():
    # Arrange — the peer refused (rc=1) with its own JSON error envelope.
    peer_stdout = json.dumps(
        {
            "name": AGENT,
            "restarted": False,
            "reason": "stop-refused-active-turn",
            "hint": "retry with --drain-timeout SECONDS",
            "error": REFUSAL_TEXT,
        }
    )
    import scitex_agent_container.cli_pkg.lifecycle._restart_remote as remote_mod

    stub_subprocess = SimpleNamespace(
        run=lambda *a, **k: _Proc(1, peer_stdout, "")
    )
    with (
        _swap(remote_mod, "subprocess", stub_subprocess),
        _swap(
            remote_mod,
            "build_ssh_argv",
            lambda peer, argv, peers, login=False: ["ssh", peer, "--", *argv],
        ),
    ):
        # Act
        try:
            remote_mod._dispatch_remote_restart(
                "peer-1", {"id": "x"}, {}, AGENT
            )
            raised = None
        except Exception as exc:  # noqa: BLE001 — asserting on the raised shape
            raised = exc
    # Assert
    assert raised is not None
    assert "Peer refused the restart (NOT a transport failure)" in str(raised)
    assert "stop-refused-active-turn" in str(raised)
    assert "--drain-timeout" in str(raised)
