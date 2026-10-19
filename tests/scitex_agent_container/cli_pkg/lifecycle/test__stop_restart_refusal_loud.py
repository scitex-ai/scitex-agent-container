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
PA-307 §3: one assertion per test function; Arrange/Act/Assert each
on its own line.
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
import scitex_agent_container.cli_pkg.lifecycle._restart_remote as remote_mod
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
        _swap(restart_local_mod, "spec_host_fallback_peer", lambda *a, **k: None),
        _swap(restart_mod, "must_broker_to_host", lambda: False),
    ):
        return CliRunner().invoke(restart, argv)


class _Proc:
    def __init__(self, returncode: int, stdout: str, stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _drive_remote_stop_refusal() -> Exception | None:
    """Run the ssh stop dispatch against a peer that refuses with JSON."""
    peer_stdout = json.dumps(
        {
            "name": AGENT,
            "stopped": False,
            "reason": "stop-refused-active-turn",
            "error": REFUSAL_TEXT,
        }
    )
    stub_subprocess = SimpleNamespace(run=lambda *a, **k: _Proc(1, peer_stdout, ""))
    with (
        _swap(stop_mod, "subprocess", stub_subprocess),
        _swap(
            stop_mod,
            "build_ssh_argv",
            lambda peer, argv, peers: ["ssh", peer, "--", *argv],
        ),
    ):
        try:
            stop_mod._dispatch_remote_stop("peer-1", {"id": "x"}, {}, AGENT)
            return None
        except Exception as exc:  # noqa: BLE001 — the raised shape is the assertion subject
            return exc


def _drive_remote_restart_refusal() -> Exception | None:
    """Run the ssh restart dispatch against a peer that refuses with JSON."""
    peer_stdout = json.dumps(
        {
            "name": AGENT,
            "restarted": False,
            "reason": "stop-refused-active-turn",
            "hint": "retry with --drain-timeout SECONDS",
            "error": REFUSAL_TEXT,
        }
    )
    stub_subprocess = SimpleNamespace(run=lambda *a, **k: _Proc(1, peer_stdout, ""))
    with (
        _swap(remote_mod, "subprocess", stub_subprocess),
        _swap(
            remote_mod,
            "build_ssh_argv",
            lambda peer, argv, peers, login=False: ["ssh", peer, "--", *argv],
        ),
    ):
        try:
            remote_mod._dispatch_remote_restart("peer-1", {"id": "x"}, {}, AGENT)
            return None
        except Exception as exc:  # noqa: BLE001 — the raised shape is the assertion subject
            return exc


# ---------------------------------------------------------------------------
# sac agents stop — refusal is loud
# ---------------------------------------------------------------------------


@pytest.fixture
def stop_refusal_result():
    # Arrange
    argv = [AGENT]
    # Act
    result = _drive_stop_local(argv)
    # Assert
    return result


def test_stop_refusal_exits_nonzero(stop_refusal_result):
    # Arrange
    result = stop_refusal_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_stop_refusal_names_agent_not_stopped(stop_refusal_result):
    # Arrange
    result = stop_refusal_result
    # Act
    out = result.output
    # Assert
    assert f"NOT stopped: agent '{AGENT}' is actively working" in out


def test_stop_refusal_names_intactness(stop_refusal_result):
    # Arrange
    result = stop_refusal_result
    # Act
    out = result.output
    # Assert
    assert "Nothing was torn down" in out


def test_stop_refusal_names_remedy(stop_refusal_result):
    # Arrange
    result = stop_refusal_result
    # Act
    out = result.output
    # Assert
    assert "--drain-timeout" in out


@pytest.fixture
def stop_refusal_warning_result():
    # Arrange
    argv = [AGENT]
    # Act
    with _warning_threshold():
        result = _drive_stop_local(argv)
    # Assert
    return result


def test_stop_refusal_warning_exits_nonzero(stop_refusal_warning_result):
    # Arrange
    result = stop_refusal_warning_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_stop_refusal_warning_stays_loud(stop_refusal_warning_result):
    # Arrange
    result = stop_refusal_warning_result
    # Act
    out = result.output
    # Assert
    assert f"NOT stopped: agent '{AGENT}' is actively working" in out


@pytest.fixture
def stop_refusal_json_result():
    # Arrange
    argv = [AGENT, "--json"]
    # Act
    result = _drive_stop_local(argv)
    # Assert
    return result


def test_stop_refusal_json_exits_nonzero(stop_refusal_json_result):
    # Arrange
    result = stop_refusal_json_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_stop_refusal_json_names_agent(stop_refusal_json_result):
    # Arrange
    result = stop_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["name"] == AGENT


def test_stop_refusal_json_marks_not_stopped(stop_refusal_json_result):
    # Arrange
    result = stop_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["stopped"] is False


def test_stop_refusal_json_names_reason(stop_refusal_json_result):
    # Arrange
    result = stop_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["reason"] == "stop-refused-active-turn"


def test_stop_refusal_json_carries_error(stop_refusal_json_result):
    # Arrange
    result = stop_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert "working" in envelope["error"]


# ---------------------------------------------------------------------------
# sac agents restart — refusal aborts loud with reason, never silent
# ---------------------------------------------------------------------------


@pytest.fixture
def restart_refusal_result():
    # Arrange
    argv = [AGENT, "-y"]
    # Act
    result = _drive_restart_local(argv)
    # Assert
    return result


def test_restart_refusal_exits_nonzero(restart_refusal_result):
    # Arrange
    result = restart_refusal_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_restart_refusal_names_not_restarted(restart_refusal_result):
    # Arrange
    result = restart_refusal_result
    # Act
    out = result.output
    # Assert
    assert f"Agent '{AGENT}' NOT restarted" in out


def test_restart_refusal_names_old_process(restart_refusal_result):
    # Arrange
    result = restart_refusal_result
    # Act
    out = result.output
    # Assert
    assert "still the OLD process" in out


def test_restart_refusal_names_remedy(restart_refusal_result):
    # Arrange
    result = restart_refusal_result
    # Act
    out = result.output
    # Assert
    assert "--drain-timeout" in out


@pytest.fixture
def restart_refusal_warning_result():
    # Arrange
    argv = [AGENT, "-y"]
    # Act
    with _warning_threshold():
        result = _drive_restart_local(argv)
    # Assert
    return result


def test_restart_refusal_warning_exits_nonzero(restart_refusal_warning_result):
    # Arrange
    result = restart_refusal_warning_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_restart_refusal_warning_stays_loud(restart_refusal_warning_result):
    # Arrange
    result = restart_refusal_warning_result
    # Act
    out = result.output
    # Assert
    assert f"Agent '{AGENT}' NOT restarted" in out


@pytest.fixture
def restart_refusal_json_result():
    # Arrange
    argv = [AGENT, "-y", "--json"]
    # Act
    result = _drive_restart_local(argv)
    # Assert
    return result


def test_restart_refusal_json_exits_nonzero(restart_refusal_json_result):
    # Arrange
    result = restart_refusal_json_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_restart_refusal_json_marks_not_restarted(restart_refusal_json_result):
    # Arrange
    result = restart_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["restarted"] is False


def test_restart_refusal_json_names_reason(restart_refusal_json_result):
    # Arrange
    result = restart_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["reason"] == "stop-refused-active-turn"


def test_restart_refusal_json_names_remedy(restart_refusal_json_result):
    # Arrange
    result = restart_refusal_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert "--drain-timeout" in envelope["hint"]


@pytest.fixture
def restart_generic_failure_json_result():
    # Arrange
    def _boom(*args, **kwargs):
        raise RuntimeError("start leg exploded")

    # Act
    with (
        _swap(restart_local_mod, "agent_restart", _boom),
        _swap(restart_local_mod, "try_dispatch_remote", lambda *a, **k: False),
        _swap(restart_local_mod, "spec_host_fallback_peer", lambda *a, **k: None),
        _swap(restart_mod, "must_broker_to_host", lambda: False),
    ):
        result = CliRunner().invoke(restart, [AGENT, "-y", "--json"])
    # Assert
    return result


def test_restart_generic_failure_marks_not_restarted(
    restart_generic_failure_json_result,
):
    # Arrange
    result = restart_generic_failure_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert envelope["restarted"] is False


def test_restart_generic_failure_carries_error(
    restart_generic_failure_json_result,
):
    # Arrange
    result = restart_generic_failure_json_result
    # Act
    envelope = json.loads(result.stdout)
    # Assert
    assert "start leg exploded" in envelope["error"]


# ---------------------------------------------------------------------------
# Cross-host: a peer-side refusal reads as a refusal, not a dead peer
# ---------------------------------------------------------------------------


@pytest.fixture
def remote_stop_refusal_text():
    # Arrange
    raised = _drive_remote_stop_refusal()
    # Act
    text = str(raised)
    # Assert
    return text


def test_remote_stop_refusal_framed_as_refusal(remote_stop_refusal_text):
    # Arrange
    text = remote_stop_refusal_text
    # Act
    framed = "Peer refused the stop (NOT a transport failure)" in text
    # Assert
    assert framed


def test_remote_stop_refusal_quotes_peer_reason(remote_stop_refusal_text):
    # Arrange
    text = remote_stop_refusal_text
    # Act
    quoted = "stop-refused-active-turn" in text
    # Assert
    assert quoted


def test_remote_stop_refusal_quotes_peer_words(remote_stop_refusal_text):
    # Arrange
    text = remote_stop_refusal_text
    # Act
    quoted = "working" in text
    # Assert
    assert quoted


@pytest.fixture
def remote_restart_refusal_text():
    # Arrange
    raised = _drive_remote_restart_refusal()
    # Act
    text = str(raised)
    # Assert
    return text


def test_remote_restart_refusal_framed_as_refusal(remote_restart_refusal_text):
    # Arrange
    text = remote_restart_refusal_text
    # Act
    framed = "Peer refused the restart (NOT a transport failure)" in text
    # Assert
    assert framed


def test_remote_restart_refusal_quotes_peer_reason(remote_restart_refusal_text):
    # Arrange
    text = remote_restart_refusal_text
    # Act
    quoted = "stop-refused-active-turn" in text
    # Assert
    assert quoted


def test_remote_restart_refusal_quotes_peer_hint(remote_restart_refusal_text):
    # Arrange
    text = remote_restart_refusal_text
    # Act
    quoted = "--drain-timeout" in text
    # Assert
    assert quoted
