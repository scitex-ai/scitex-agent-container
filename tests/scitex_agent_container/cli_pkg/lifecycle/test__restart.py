"""Tests for ``cli_pkg.lifecycle._restart.restart`` (Click command).

The command has four behavioural branches: ``--dry-run`` prints a
"would restart" line without invoking the restart collaborator; absence
of ``-y``/``--yes`` refuses with exit code ``2``; the happy path
delegates to ``agent_restart(name)`` and reports success; a YAML path
argument is resolved through ``resolve_with_prefix`` / ``load_config``
so the resolved ``config.name`` is forwarded to ``agent_restart`` rather
than the raw path; any exception from ``agent_restart`` surfaces as
exit code ``1`` with the message preserved on stderr/stdout.

PA-306: no ``unittest.mock`` / ``monkeypatch``. Production collaborators
(``agent_restart``, ``resolve_with_prefix``, ``load_config``) are
swapped at the module's namespace via a small ``_swap`` context
manager with explicit save/restore.

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape invariants over a single arrange/act collapse into
``pytest.parametrize``.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._restart as restart_mod
from scitex_agent_container.cli_pkg.lifecycle._restart import restart


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
def _swap(name: str, fn: Callable) -> Iterator[None]:
    saved = getattr(restart_mod, name)
    setattr(restart_mod, name, fn)
    try:
        yield
    finally:
        setattr(restart_mod, name, saved)


class _FakeCfg:
    def __init__(self, name: str) -> None:
        self.name = name


# ---------------------------------------------------------------------------
# --dry-run branch: no collaborator invocation, prints "would restart"
# ---------------------------------------------------------------------------


def test_dry_run_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_dry_run_announces_target_agent():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert "would restart agent 'alpha'" in result.output


def test_dry_run_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "--dry-run"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Confirmation guard: missing --yes refuses with exit code 2
# ---------------------------------------------------------------------------


def test_refuse_without_yes_exits_two():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert result.exit_code == 2


def test_refuse_without_yes_emits_refusal_message():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha"])
    # Assert
    assert "Refusing to restart" in result.output


def test_refuse_without_yes_does_not_invoke_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Happy path: bare name is forwarded to agent_restart verbatim
# ---------------------------------------------------------------------------


def test_happy_path_exits_zero():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_happy_path_forwards_name_to_agent_restart():
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert called == ["alpha"]


def test_happy_path_reports_success():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "restarted" in result.output


# ---------------------------------------------------------------------------
# YAML path argument: resolved through resolve_with_prefix/load_config,
# resolved config.name (not the raw path) is forwarded to agent_restart.
# ---------------------------------------------------------------------------


@pytest.fixture
def _yaml_path(tmp_path):
    path = tmp_path / "foo.yaml"
    path.write_text("name: foo\n")
    return path


def test_yaml_path_exits_zero(_yaml_path):
    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda _name: None),
    ):
        result = runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_yaml_path_forwards_resolved_name(_yaml_path):
    # Arrange
    called: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(_yaml_path)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved")),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner.invoke(restart, [str(_yaml_path), "-y"])
    # Assert
    assert called == ["resolved"]


# ---------------------------------------------------------------------------
# Failure path: any exception from agent_restart surfaces as exit code 1
# and the message is reported back to the user.
# ---------------------------------------------------------------------------


def _boom(_name: Any) -> None:
    raise RuntimeError("boom")


def test_failure_exits_one():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert result.exit_code == 1


def test_failure_reports_exception_message():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["alpha", "-y"])
    # Assert
    assert "boom" in result.output


# ---------------------------------------------------------------------------
# Cross-host dispatch: an active state.db row on a PEER node makes restart
# ssh into that peer and run `sac agents restart --yes --json` there — the
# node-aware automation of the manual recipe. Real on-disk state.db + a
# PATH-prepended fake ssh shim (no mocks; mirrors test__stop.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_state_db(tmp_path):
    """Per-test state.db at tmp_path; SCITEX_AGENT_CONTAINER_STATE_DB +
    module reload so the env override actually takes effect. The current
    host resolves to ``lead-host`` and one peer ``peer-x`` is declared.
    """
    import importlib

    db = tmp_path / "state.db"
    saved_db_env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    saved_host_env = os.environ.get("SAC_HOST")
    saved_cfg_env = os.environ.get("SCITEX_AGENT_CONTAINER_CONFIG")
    os.environ["SCITEX_AGENT_CONTAINER_STATE_DB"] = str(db)
    os.environ["SAC_HOST"] = "lead-host"
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "host:\n  fallback: hostname-short\npeers:\n  peer-x:\n    ssh: peer-x\n"
    )
    os.environ["SCITEX_AGENT_CONTAINER_CONFIG"] = str(cfg)
    import scitex_agent_container._state.state_db as _state_db_mod

    importlib.reload(_state_db_mod)
    try:
        yield tmp_path
    finally:
        for k, v in (
            ("SCITEX_AGENT_CONTAINER_STATE_DB", saved_db_env),
            ("SAC_HOST", saved_host_env),
            ("SCITEX_AGENT_CONTAINER_CONFIG", saved_cfg_env),
        ):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(_state_db_mod)


@pytest.fixture
def remote_row_for_zeta(cross_host_state_db):
    """Seed an active row for agent ``zeta`` on peer ``peer-x``."""
    from scitex_agent_container._state.state_db import record_instance_start

    # Port reads as a whole (carve-out, see _skills .../14_numeric-literals.md).
    port = 18_888
    iid = record_instance_start(name="zeta", host="peer-x", a2a_port=port)
    return iid


@pytest.fixture
def ssh_shim(tmp_path):
    """PATH-prepended fake ssh that emits a restart JSON envelope and rc=0,
    recording each invocation's argv so tests can assert the right node +
    verb + flags were dispatched.
    """
    import json
    import sys

    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "ssh.argv.jsonl"
    payload = '{"name":"zeta","restarted":true,"a2a_port":18888,"dispatched":false}'
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        f"sys.stdout.write({json.dumps(payload)})\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


def _ssh_invocations(bin_dir):
    import json as _json

    log = bin_dir / "ssh.argv.jsonl"
    if not log.exists():
        return []
    return [_json.loads(ln) for ln in log.read_text().splitlines() if ln.strip()]


def test_cross_host_restart_dispatches_via_ssh(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_cross_host_restart_ssh_argv_targets_peer_node(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert — the recorded host (peer-x), not the local node, is targeted.
    assert "peer-x" in argv


def test_cross_host_restart_ssh_argv_carries_restart_verb(
    remote_row_for_zeta, ssh_shim
):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = " ".join(_ssh_invocations(ssh_shim)[-1])
    # Assert
    assert "sac agents restart zeta" in argv


def test_cross_host_restart_ssh_argv_includes_json_flag(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert
    assert "--json" in argv


def test_cross_host_restart_does_not_call_local_agent_restart(
    remote_row_for_zeta, ssh_shim
):
    # Arrange — when the row is remote, the local restart MUST NOT run.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert called == []


def test_cross_host_restart_json_envelope_marks_dispatched(
    remote_row_for_zeta, ssh_shim
):
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert envelope.get("dispatched") is True


def test_cross_host_restart_reopens_fresh_remote_row(remote_row_for_zeta, ssh_shim):
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    runner.invoke(restart, ["zeta", "-y"])
    rows = [r for r in list_active_instances() if r["name"] == "zeta"]
    # Assert — exactly one active row, still on the peer (old closed, new opened).
    assert len(rows) == 1 and rows[0]["host"] == "peer-x"


# ---------------------------------------------------------------------------
# No-registry-row fallback at the CLI level: an agent with NO active state.db
# row (ad-hoc / pre-autorecord launch) restarts locally via the spec, instead
# of attempting any cross-host ssh.
# ---------------------------------------------------------------------------


def test_no_row_agent_restarts_locally_without_ssh(cross_host_state_db, ssh_shim):
    # Arrange — no row seeded for ``solo``; agent_restart swapped to a recorder.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda name: called.append(name)):
        result = runner.invoke(restart, ["solo", "-y"])
    # Assert — local path taken (agent_restart called), no ssh dispatched.
    assert called == ["solo"] and _ssh_invocations(ssh_shim) == []
