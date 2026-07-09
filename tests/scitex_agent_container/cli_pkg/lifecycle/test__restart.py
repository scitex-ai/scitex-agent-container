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


def test_local_restart_json_envelope_marks_not_dispatched(
    cross_host_state_db, ssh_shim
):
    import json as _json

    # Arrange — no row; local restart with --json. agent_restart no-op.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["solo", "-y", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert — JSON envelope reports the local (non-dispatched) restart.
    assert envelope.get("dispatched") is False and envelope.get("restarted") is True


def test_local_restart_failure_json_envelope_carries_error(
    cross_host_state_db, ssh_shim
):
    import json as _json

    # Arrange — local restart raises; --json must surface the error.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", _boom):
        result = runner.invoke(restart, ["solo", "-y", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert "boom" in envelope.get("error", "")


# ---------------------------------------------------------------------------
# Cross-host dispatch error branches: a remote node that exits non-zero, or
# returns non-JSON stdout, must surface a clear RuntimeError (exit 1) — no
# silent fallback. Real fake-ssh shims drive each branch.
# ---------------------------------------------------------------------------


def _install_ssh_shim(bin_dir, *, rc: int, stdout: str):
    """Write a PATH-prepended fake ssh emitting ``stdout`` and exiting ``rc``."""
    import json
    import sys

    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.stdout.write({json.dumps(stdout)})\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    return saved_path


@pytest.fixture
def ssh_shim_rc1(tmp_path):
    """Fake ssh that exits non-zero (remote restart failed)."""
    bin_dir = tmp_path / "_shim_rc1"
    saved_path = _install_ssh_shim(bin_dir, rc=1, stdout="remote boom")
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


@pytest.fixture
def ssh_shim_nonjson(tmp_path):
    """Fake ssh that exits zero but emits non-JSON stdout (peer too old)."""
    bin_dir = tmp_path / "_shim_nonjson"
    saved_path = _install_ssh_shim(bin_dir, rc=0, stdout="not json at all")
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


def test_cross_host_restart_remote_failure_exits_one(remote_row_for_zeta, ssh_shim_rc1):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — remote rc=1 surfaces as a local exit-1, not a silent pass.
    assert result.exit_code == 1


def test_cross_host_restart_remote_failure_reports_peer(
    remote_row_for_zeta, ssh_shim_rc1
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — the failure message names the peer node.
    assert "peer-x" in result.output


def test_cross_host_restart_nonjson_stdout_exits_one(
    remote_row_for_zeta, ssh_shim_nonjson
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert — non-JSON peer stdout is a hard error, not a silent fallback.
    assert result.exit_code == 1


def test_cross_host_restart_nonjson_stdout_reports_non_json(
    remote_row_for_zeta, ssh_shim_nonjson
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["zeta", "-y"])
    # Assert
    assert "non-JSON" in result.output


# ---------------------------------------------------------------------------
# --fresh branch: a fresh (no-resume) restart is bypass-only. With the host
# listen reachable it brokers ``start --force --fresh`` (fresh=True) and never
# touches the local restart; on a bare host (no listen) it fails LOUD with the
# direct command rather than silently doing a resuming restart.
# ---------------------------------------------------------------------------


def test_fresh_without_bypass_exits_one():
    # Arrange — no listen base URL resolvable (bare host).
    runner = CliRunner()
    # Act
    with _swap("_bypass_base_url_available", lambda: False):
        result = runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert result.exit_code == 1


def test_fresh_without_bypass_reports_direct_start_command():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_bypass_base_url_available", lambda: False):
        result = runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert — fail loud with the deterministic bare-host command.
    assert "start alpha --force --fresh" in result.output


def test_fresh_with_bypass_brokers_fresh_true():
    # Arrange — bypass available; record the brokered (name, fresh).
    calls: list[tuple[str, bool]] = []

    def _rec(name, fresh=False):
        calls.append((name, fresh))
        return {"returncode": 0}

    runner = CliRunner()
    # Act
    with (
        _swap("_bypass_base_url_available", lambda: True),
        _swap("_restart_via_host_bypass", _rec),
    ):
        runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert calls == [("alpha", True)]


def test_fresh_does_not_call_local_agent_restart():
    # Arrange — fresh is bypass-only; the local restart must NOT run.
    called: list[str] = []
    runner = CliRunner()
    # Act
    with (
        _swap("_bypass_base_url_available", lambda: True),
        _swap(
            "_restart_via_host_bypass",
            lambda name, fresh=False: {"returncode": 0},
        ),
        _swap("agent_restart", lambda name: called.append(name)),
    ):
        runner.invoke(restart, ["alpha", "-y", "--fresh"])
    # Assert
    assert called == []


# ---------------------------------------------------------------------------
# Variadic NAME... + --all (operator TODO 2026-07-04): restart accepts
# multiple names in one call and an --all flag that enumerates the same
# fleet ``sac agents list`` shows. The per-agent restart is exercised via
# ``_restart_one``; the loop-level behaviour (fan-out, mutual exclusion,
# fail-loud aggregate, JSON aggregation) is asserted by swapping that seam.
# ---------------------------------------------------------------------------


def _ok_restart_one(name, *, as_json, fresh):
    """Recorder stand-in for ``_restart_one`` — always succeeds."""
    return {"name": name, "restarted": True, "dispatched": False}, True


def test_multiple_names_exit_zero():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_restart_one", _ok_restart_one):
        result = runner.invoke(restart, ["alpha", "beta", "-y"])
    # Assert
    assert result.exit_code == 0, result.output


def test_multiple_names_restart_each_once():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        runner.invoke(restart, ["alpha", "beta", "-y"])
    # Assert — each name restarted exactly once, in order.
    assert seen == ["alpha", "beta"]


def test_multiple_names_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_restart_one", _ok_restart_one):
        result = runner.invoke(restart, ["alpha", "beta", "-y", "--json"])
    payload = _json.loads(result.output.strip().splitlines()[-1])
    # Assert — multiple names aggregate into a JSON array.
    assert isinstance(payload, list) and len(payload) == 2


def test_all_flag_restarts_every_enumerated_agent():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_fleet", lambda: ["a1", "a2", "a3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all", "-y"])
    # Assert — every enumerated agent restarted.
    assert result.exit_code == 0 and seen == ["a1", "a2", "a3"]


def test_all_flag_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_fleet", lambda: ["only-one"]),
        _swap("_restart_one", _ok_restart_one),
    ):
        result = runner.invoke(restart, ["--all", "-y", "--json"])
    payload = _json.loads(result.output.strip().splitlines()[-1])
    # Assert — --all always emits an array, even for a single enumerated agent.
    assert isinstance(payload, list) and len(payload) == 1


def test_all_with_explicit_names_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all", "alpha", "-y"])
    # Assert — --all and explicit NAMEs are mutually exclusive (exit 2).
    assert result.exit_code == 2 and "cannot be combined" in result.output


def test_all_without_yes_refuses_exit_two():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_enumerate_fleet", lambda: ["a1", "a2"]):
        result = runner.invoke(restart, ["--all"])
    # Assert — a batch restart still requires -y/--yes.
    assert result.exit_code == 2 and "Refusing to restart" in result.output


def test_no_names_and_no_all_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["-y"])
    # Assert — nothing to restart is a usage error.
    assert result.exit_code == 2


def test_one_failure_still_attempts_rest():
    # Arrange
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        if name == "bad":
            return {"name": name, "error": "boom"}, False
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        runner.invoke(restart, ["ok1", "bad", "ok2", "-y"])
    # Assert — a mid-batch failure does not abort the remaining agents.
    assert seen == ["ok1", "bad", "ok2"]


def test_one_failure_exits_nonzero():
    # Arrange
    def _rec(name, *, as_json, fresh):
        if name == "bad":
            return {"name": name, "error": "boom"}, False
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with _swap("_restart_one", _rec):
        result = runner.invoke(restart, ["ok1", "bad", "-y"])
    # Assert — any failure surfaces as an overall non-zero exit.
    assert result.exit_code == 1


def test_dry_run_multiple_names_lists_each():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["alpha", "beta", "--dry-run"])
    # Assert — dry-run announces every target and invokes no restart.
    assert (
        "would restart agent 'alpha'" in result.output
        and "would restart agent 'beta'" in result.output
    )


def test_single_name_json_stays_bare_object():
    import json as _json

    # Arrange — single explicit name must keep the historical bare-object shape.
    runner = CliRunner()
    # Act
    with _swap("agent_restart", lambda _name: None):
        result = runner.invoke(restart, ["alpha", "-y", "--json"])
    payload = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert isinstance(payload, dict) and payload.get("name") == "alpha"


# ---------------------------------------------------------------------------
# Split selection flags (operator request 2026-07-09): --all was surprising
# because it restarted stopped agents too. It is now split into
# --all-running (only currently-running agents) and --all-registry (every
# registered agent == the old --all behaviour). --all stays a backward-compat
# alias for --all-registry. The two modes are mutually exclusive; more than
# one selection flag fails loud.
# ---------------------------------------------------------------------------


def test_all_running_restarts_only_running_agents():
    # Arrange — --all-running must enumerate via the RUNNING-only seam.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1", "live-2"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "live-2", "stopped-3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all-running", "-y"])
    # Assert — only the running agents restarted (stopped-3 excluded).
    assert result.exit_code == 0 and seen == ["live-1", "live-2"]


def test_all_registry_restarts_every_agent():
    # Arrange — --all-registry must enumerate via the full-fleet seam.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "stopped-2", "stopped-3"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all-registry", "-y"])
    # Assert — every registered agent restarted, stopped ones included.
    assert result.exit_code == 0 and seen == ["live-1", "stopped-2", "stopped-3"]


def test_all_alias_matches_all_registry_behaviour():
    # Arrange — --all is a backward-compat alias for --all-registry: it must
    # enumerate the FULL fleet, not the running-only subset.
    seen: list[str] = []

    def _rec(name, *, as_json, fresh):
        seen.append(name)
        return {"name": name, "restarted": True}, True

    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["live-1"]),
        _swap("_enumerate_fleet", lambda: ["live-1", "stopped-2"]),
        _swap("_restart_one", _rec),
    ):
        result = runner.invoke(restart, ["--all", "-y"])
    # Assert — --all == --all-registry (uses the full-fleet enumeration).
    assert result.exit_code == 0 and seen == ["live-1", "stopped-2"]


def test_all_running_and_all_registry_together_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all-running", "--all-registry", "-y"])
    # Assert — the two selection modes are mutually exclusive (exit 2).
    assert result.exit_code == 2 and "mutually exclusive" in result.output


def test_all_running_and_all_alias_together_is_usage_error():
    # Arrange — --all is the alias for --all-registry, so it conflicts with
    # --all-running just the same.
    runner = CliRunner()
    # Act
    result = runner.invoke(restart, ["--all-running", "--all", "-y"])
    # Assert
    assert result.exit_code == 2 and "mutually exclusive" in result.output


def test_all_running_with_explicit_names_is_usage_error():
    # Arrange
    runner = CliRunner()
    # Act
    with _swap("_enumerate_running", lambda: ["live-1"]):
        result = runner.invoke(restart, ["--all-running", "alpha", "-y"])
    # Assert — a selection flag cannot combine with explicit NAMEs (exit 2).
    assert result.exit_code == 2 and "cannot" in result.output


def test_all_running_json_emits_array():
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    with (
        _swap("_enumerate_running", lambda: ["only-live"]),
        _swap("_restart_one", _ok_restart_one),
    ):
        result = runner.invoke(restart, ["--all-running", "-y", "--json"])
    payload = _json.loads(result.output.strip().splitlines()[-1])
    # Assert — a batch selection flag always emits an array.
    assert isinstance(payload, list) and len(payload) == 1


def test_enumerate_running_keeps_only_running_status_rows():
    # Arrange — _enumerate_running reuses the SAME data (and liveness) as the
    # list command; it must keep only rows whose probed status is "running".
    import scitex_agent_container.cli_pkg._helpers as _helpers_mod

    rows = [
        {"name": "run-a", "status": "running"},
        {"name": "stop-b", "status": "stopped"},
        {"name": "unk-c", "status": "unknown"},
        {"name": "def-d", "status": "defined"},
        {"name": "run-e", "status": "running"},
    ]
    saved = getattr(_helpers_mod, "get_agent_list_data", None)
    _helpers_mod.get_agent_list_data = lambda *_a, **_kw: rows
    # Act
    try:
        got = restart_mod._enumerate_running()
    finally:
        if saved is None:
            delattr(_helpers_mod, "get_agent_list_data")
        else:
            _helpers_mod.get_agent_list_data = saved
    # Assert — only the two running agents survive, in order.
    assert got == ["run-a", "run-e"]
