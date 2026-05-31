"""Tests for cli_pkg.lifecycle._stop.

PA-306: no ``unittest.mock``. Collaborators are swapped at the
module namespace via a small ``_swap`` context manager.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

import pytest
from click.testing import CliRunner

import scitex_agent_container.cli_pkg.lifecycle._stop as stop_mod
from scitex_agent_container.cli_pkg.lifecycle._stop import stop


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
    saved = getattr(stop_mod, name)
    setattr(stop_mod, name, fn)
    try:
        yield
    finally:
        setattr(stop_mod, name, saved)


def _seed(tmp_path: Path, names) -> Path:
    """Seed a directory with N agents (each <name>/<name>.yaml)."""
    root = tmp_path / "agents"
    for n in names:
        d = root / n
        d.mkdir(parents=True)
        (d / f"{n}.yaml").write_text(f"name: {n}\n")
    return root


class _FakeCfg:
    def __init__(self, name: str) -> None:
        self.name = name


# ---------------------------------------------------------------------------
# Dry-run: enumerates targets without invoking agent_stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def dry_run_result(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, [str(root), "extra", "--dry-run"])
    # Assert
    return result


def test_dry_run_lists_targets_exits_zero(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_dry_run_lists_targets_mentions_single_name(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    out = result.output
    # Assert
    assert "would stop agent 'extra'" in out


def test_dry_run_lists_targets_mentions_bulk_yaml(dry_run_result):
    # Arrange
    result = dry_run_result
    # Act
    out = result.output
    # Assert
    assert "would stop agent at" in out


# ---------------------------------------------------------------------------
# Bulk-without-yes refuses.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_no_yes_result(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, [str(root)])
    # Assert
    return result


def test_bulk_without_yes_refuses_exits_two(bulk_no_yes_result):
    # Arrange
    result = bulk_no_yes_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 2


def test_bulk_without_yes_refuses_prints_message(bulk_no_yes_result):
    # Arrange
    result = bulk_no_yes_result
    # Act
    out = result.output
    # Assert
    assert "Refusing to stop 2 agents" in out


# ---------------------------------------------------------------------------
# Bulk-with-yes: invokes agent_stop for each seeded agent.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_with_yes_run(tmp_path):
    # Arrange
    root = _seed(tmp_path, ["a", "b"])
    stopped: list = []
    # Act
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    # Assert
    return result, stopped


def test_bulk_with_yes_stops_all_exits_zero(bulk_with_yes_run):
    # Arrange
    result, _ = bulk_with_yes_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0, result.output


def test_bulk_with_yes_stops_all_invokes_agent_stop_per_agent(bulk_with_yes_run):
    # Arrange
    _, stopped = bulk_with_yes_run
    # Act
    names = sorted(s[0] for s in stopped)
    # Assert
    assert names == ["a", "b"]


# ---------------------------------------------------------------------------
# Bulk failure: continues past errors and exits nonzero.
# ---------------------------------------------------------------------------


@pytest.fixture
def bulk_failure_result(tmp_path):
    # Arrange
    def _boom(_name, _force):
        raise RuntimeError("boom")

    root = _seed(tmp_path, ["a", "b"])
    # Act
    with (
        _swap("load_config", lambda p: _FakeCfg(Path(p).stem)),
        _swap("agent_stop", _boom),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(root), "-y"])
    # Assert
    return result


def test_bulk_failure_reports_and_exits_nonzero_exit_code(bulk_failure_result):
    # Arrange
    result = bulk_failure_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_bulk_failure_reports_and_exits_nonzero_prints_error(bulk_failure_result):
    # Arrange
    result = bulk_failure_result
    # Act
    out = result.output
    # Assert
    assert "boom" in out


# ---------------------------------------------------------------------------
# Single-target by name: forwarded straight to agent_stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_name_run():
    # Arrange
    stopped: list = []
    # Act
    with _swap("agent_stop", lambda name, force: stopped.append((name, force))):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    # Assert
    return result, stopped


def test_single_name_path_exits_zero(single_name_run):
    # Arrange
    result, _ = single_name_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_single_name_path_invokes_agent_stop_with_name(single_name_run):
    # Arrange
    _, stopped = single_name_run
    # Act
    calls = list(stopped)
    # Assert
    assert calls == [("alpha", False)]


def test_single_name_path_prints_stopped_message(single_name_run):
    # Arrange
    result, _ = single_name_run
    # Act
    out = result.output
    # Assert
    assert "Agent 'alpha' stopped" in out


# ---------------------------------------------------------------------------
# Single-target by YAML path: resolved to config.name before stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_yaml_run(tmp_path):
    # Arrange
    p = tmp_path / "foo.yaml"
    p.write_text("name: foo\n")
    stopped: list = []
    # Act
    with (
        _swap("resolve_with_prefix", lambda *_a, **_kw: str(p)),
        _swap("load_config", lambda *_a, **_kw: _FakeCfg("resolved-foo")),
        _swap("agent_stop", lambda name, force: stopped.append((name, force))),
    ):
        runner = CliRunner()
        result = runner.invoke(stop, [str(p), "--force"])
    # Assert
    return result, stopped


def test_single_yaml_path_resolves_name_exits_zero(single_yaml_run):
    # Arrange
    result, _ = single_yaml_run
    # Act
    code = result.exit_code
    # Assert
    assert code == 0


def test_single_yaml_path_resolves_name_invokes_agent_stop_with_resolved_name(
    single_yaml_run,
):
    # Arrange
    _, stopped = single_yaml_run
    # Act
    calls = list(stopped)
    # Assert
    assert calls == [("resolved-foo", True)]


# ---------------------------------------------------------------------------
# Single-target failure: exits nonzero and surfaces the error.
# ---------------------------------------------------------------------------


@pytest.fixture
def single_failure_result():
    # Arrange
    def _boom(name, force=False):
        raise RuntimeError("nope")

    # Act
    with _swap("agent_stop", _boom):
        runner = CliRunner()
        result = runner.invoke(stop, ["alpha"])
    # Assert
    return result


def test_single_failure_exits_nonzero_exit_code(single_failure_result):
    # Arrange
    result = single_failure_result
    # Act
    code = result.exit_code
    # Assert
    assert code == 1


def test_single_failure_exits_nonzero_prints_error(single_failure_result):
    # Arrange
    result = single_failure_result
    # Act
    out = result.output
    # Assert
    assert "nope" in out


# ---------------------------------------------------------------------------
# Cross-host dispatch: state.db row on a peer → ssh + remote sac stop.
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_host_state_db(tmp_path):
    """Per-test state.db at tmp_path; SCITEX_AGENT_CONTAINER_STATE_DB +
    module reload so the env override actually takes effect.
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

    iid = record_instance_start(name="zeta", host="peer-x", a2a_port=18888)
    return iid


@pytest.fixture
def ssh_shim(tmp_path):
    """PATH-prepended fake ssh that emits a stop JSON envelope and rc=0."""
    import json
    import sys

    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "ssh.argv.jsonl"
    payload = '{"name":"zeta","stopped":true,"exit_reason":"stopped","ended_at":"2026-05-16T01:00:00Z"}'
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


def test_cross_host_stop_dispatches_via_ssh(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["zeta"])
    # Assert
    assert result.exit_code == 0, result.output


def test_cross_host_stop_ssh_argv_targets_peer(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(stop, ["zeta"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert
    assert "peer-x" in argv


def test_cross_host_stop_ssh_argv_carries_stop_verb(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(stop, ["zeta"])
    argv = " ".join(_ssh_invocations(ssh_shim)[-1])
    # Assert
    assert "sac agents stop zeta" in argv


def test_cross_host_stop_ssh_argv_includes_json_flag(remote_row_for_zeta, ssh_shim):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(stop, ["zeta"])
    argv = _ssh_invocations(ssh_shim)[-1]
    # Assert
    assert "--json" in argv


def test_cross_host_stop_updates_lead_side_row(remote_row_for_zeta, ssh_shim):
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    runner.invoke(stop, ["zeta"])
    rows = [r for r in list_active_instances() if r["name"] == "zeta"]
    # Assert — row was closed (no longer in active list).
    assert rows == []


def test_cross_host_stop_json_envelope_marks_dispatched(remote_row_for_zeta, ssh_shim):
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["zeta", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert envelope.get("dispatched") is True


# ---------------------------------------------------------------------------
# stop --force release-on-unreachable.
#
# Lead's bm025 stale-binding repro: a singleton's instances row pointed
# at a dead prior host (no SLURM job → pam_slurm_adopt denied ssh), so
# stop --force itself aborted on the transport failure and the stale
# binding never cleared. With the new fall-through, --force on an
# unreachable peer tombstones the instances row + removes the
# comms_nodes pin so the singleton can re-bind to the current spec.host.
# WITHOUT --force, the ssh failure still surfaces as an error (operator
# must opt in to the destructive release).
# ---------------------------------------------------------------------------


@pytest.fixture
def ssh_shim_unreachable(tmp_path):
    """PATH-prepended fake ssh that mimics an unreachable peer
    (rc 255, stderr line that resembles ``pam_slurm_adopt`` denial)."""
    import json as _json
    import sys

    bin_dir = tmp_path / "_shim_bin_unreachable"
    bin_dir.mkdir(exist_ok=True)
    log = bin_dir / "ssh.argv.jsonl"
    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"with open({_json.dumps(str(log))}, 'a') as fh:\n"
        "    fh.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "sys.stderr.write('Access denied by pam_slurm_adopt: "
        "you have no SLURM jobs on this node.\\n')\n"
        "sys.exit(255)\n"
    )
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{saved_path}"
    try:
        yield bin_dir
    finally:
        os.environ["PATH"] = saved_path


@pytest.fixture
def remote_row_for_clew(cross_host_state_db):
    """Seed an active singleton row for ``clew`` on the unreachable
    peer ``peer-x`` AND the matching comms_nodes pin so the test can
    verify BOTH stores are cleared on force-release."""
    from scitex_agent_container._state.state_db import record_instance_start
    from scitex_agent_container._state.state_db_comms_nodes import register_comms_node

    iid = record_instance_start(
        name="clew", host="peer-x", a2a_port=19500, bound_port=19500, remote=True
    )
    register_comms_node(name="clew", host="peer-x", a2a_port=19500, source_host=None)
    return iid


def test_force_release_on_unreachable_peer_exits_zero(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["clew", "--force"])
    # Assert — operator unblocked (otherwise the bm025 repro returns rc=1).
    assert result.exit_code == 0, result.output


def test_force_release_tombstones_instance_row(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    runner.invoke(stop, ["clew", "--force"])
    # Assert — no active row for clew anywhere; the binding was released.
    rows = [r for r in list_active_instances() if r["name"] == "clew"]
    assert rows == []


def test_force_release_clears_comms_nodes_binding(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange — the federated comms_nodes pin must ALSO clear, otherwise
    # subsequent a2a routing still tries the unreachable peer even after
    # the instances row is closed.
    from scitex_agent_container._state.state_db_comms_nodes import lookup_comms_node

    runner = CliRunner()
    # Act
    runner.invoke(stop, ["clew", "--force"])
    # Assert
    assert lookup_comms_node(name="clew") is None


def test_force_release_json_envelope_carries_force_released_flag(
    remote_row_for_clew, ssh_shim_unreachable
):
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["clew", "--force", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert envelope.get("force_released") is True


def test_force_release_json_envelope_carries_release_exit_reason(
    remote_row_for_clew, ssh_shim_unreachable
):
    import json as _json

    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["clew", "--force", "--json"])
    envelope = _json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert envelope.get("exit_reason") == "peer-unreachable-force-released"


def test_no_force_on_unreachable_peer_exits_nonzero(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange — without --force, the ssh transport failure MUST surface
    # as an error (operator hasn't opted in to the destructive release).
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["clew"])
    # Assert
    assert result.exit_code != 0


def test_no_force_on_unreachable_peer_leaves_instance_row(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange — without --force, the binding MUST remain so the
    # operator can investigate before discarding it.
    from scitex_agent_container._state.state_db import list_active_instances

    runner = CliRunner()
    # Act
    runner.invoke(stop, ["clew"])
    # Assert
    rows = [r for r in list_active_instances() if r["name"] == "clew"]
    assert len(rows) == 1


def test_no_force_on_unreachable_peer_message_surfaces_peer_diagnostic(
    remote_row_for_clew, ssh_shim_unreachable
):
    # Arrange — operator needs the underlying ssh stderr to diagnose
    # (per the project's no-silent-fallback rule).
    runner = CliRunner()
    # Act
    result = runner.invoke(stop, ["clew"])
    # Assert
    assert "pam_slurm_adopt" in result.output or "peer-x" in result.output
