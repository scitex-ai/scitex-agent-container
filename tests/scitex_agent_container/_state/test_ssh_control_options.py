"""Tests for :mod:`scitex_agent_container._state.host_config` ssh ControlMaster wiring.

Conventions (mirroring test_dispatch_ledger.py / test_state_db_turns_*.py):

  * One assertion per test (STX-TQ007). Related invariants collapse
    into ``pytest.parametrize``.
  * AAA markers (Arrange / Act / Assert).
  * No mocks / monkeypatch (PA-306). Env mutation goes through the
    ``env_save_restore`` fixture; subprocess calls run for real against
    a PATH-installed fake ``ssh`` binary via the ``subprocess_shim``
    fixture (`tests/scitex_agent_container/_helpers/subprocess_shim.py`).

Coverage map:

  * ``ssh_control_options`` — default shape, explicit dir arg,
    ``$SAC_SSH_CONTROL_DIR`` env override, ``SAC_SSH_CONTROL_MASTER``
    opt-out matrix, fall-through to ``[]`` on read-only parent.
  * ``ssh_control_options_str`` — shell-quoted shape; empty when opted out.
  * ``build_ssh_argv`` — control options appear, opt-out preserves the
    pre-patch shape, caller ``extra_opts`` wins (last-flag-wins on ssh).
  * Direct-ssh sites — ``_network.peer._post_turn_via_ssh``,
    ``cli_pkg.priority_cmds._ssh_start_agent``,
    ``cli_pkg._send_preflight.default_ssh_runner`` all run real ssh
    against the PATH shim and we read their argv back.
  * CLI — ``sac host ssh-opts`` round-trip (CliRunner is in-process, no
    subprocess.run involved, so the shim isn't needed there).
"""

from __future__ import annotations

import shlex
import stat
import tempfile

import pytest
from click.testing import CliRunner

# Re-import the shared helpers package fixtures (env_save_restore,
# subprocess_shim) via the conftest at tests/scitex_agent_container/.


# ---------------------------------------------------------------------------
# ssh_control_options — pure function, default shape
# ---------------------------------------------------------------------------


def test_ssh_control_options_emits_three_opt_pairs(tmp_path, env_save_restore):
    # Arrange — pin control_dir away from $TMPDIR so the test is hermetic.
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert
    assert opts[0::2] == ["-o", "-o", "-o"]


def test_ssh_control_options_first_pair_is_ControlMaster_auto(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert
    assert opts[1] == "ControlMaster=auto"


def test_ssh_control_options_second_pair_is_ControlPersist_60s(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert
    assert opts[3] == "ControlPersist=60s"


def test_ssh_control_options_third_pair_starts_with_ControlPath(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert
    assert opts[5].startswith("ControlPath=")


# ---------------------------------------------------------------------------
# ssh_control_options — control_dir creation + ControlPath shape
# ---------------------------------------------------------------------------


def test_ssh_control_options_creates_missing_control_dir(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    target = tmp_path / "freshly-created-cm-dir"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options(control_dir=target)

    # Assert
    assert target.is_dir()


def test_ssh_control_options_control_path_points_inside_control_dir(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    target = tmp_path / "cm"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=target)

    # Assert — %C is OpenSSH's hashed (user,host,port) token; keeps the
    # socket name short + collision-free across simultaneously-active peers.
    assert f"ControlPath={target}/%C" in opts


# ---------------------------------------------------------------------------
# ssh_control_options — env override + arg precedence
# ---------------------------------------------------------------------------


def test_ssh_control_options_env_var_creates_override_dir(tmp_path, env_save_restore):
    # Arrange
    override = tmp_path / "env-override-cm"
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(override))
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options()

    # Assert
    assert override.is_dir()


def test_ssh_control_options_env_var_appears_in_control_path(
    tmp_path, env_save_restore
):
    # Arrange
    override = tmp_path / "env-override-cm"
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(override))
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options()

    # Assert
    assert f"ControlPath={override}/%C" in opts


def test_ssh_control_options_explicit_arg_creates_arg_dir(tmp_path, env_save_restore):
    # Arrange — arg should win over env.
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "env"))
    arg_dir = tmp_path / "arg"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options(control_dir=arg_dir)

    # Assert
    assert arg_dir.is_dir()


def test_ssh_control_options_explicit_arg_skips_env_dir(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_dir = tmp_path / "env"
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(env_dir))
    arg_dir = tmp_path / "arg"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options(control_dir=arg_dir)

    # Assert — env_dir is NOT materialized because the explicit arg won.
    assert not env_dir.exists()


def test_ssh_control_options_explicit_arg_path_appears_in_control_path(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "env"))
    arg_dir = tmp_path / "arg"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=arg_dir)

    # Assert
    assert f"ControlPath={arg_dir}/%C" in opts


# ---------------------------------------------------------------------------
# ssh_control_options — opt-out semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("opt_out_value", ["0", "no", "false", "off", "NO", "FALSE"])
def test_ssh_control_options_opt_out_returns_empty(
    tmp_path, env_save_restore, opt_out_value
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", opt_out_value)
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm-should-not-be-used")

    # Assert — empty list = ssh argv falls back to pre-patch shape.
    assert opts == []


def test_ssh_control_options_opt_out_does_not_create_control_dir(
    tmp_path, env_save_restore
):
    # Arrange — opt-out must short-circuit BEFORE mkdir so we don't leave
    # scratch dirs behind when the operator has disabled multiplexing.
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    target = tmp_path / "should-not-be-created"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options(control_dir=target)

    # Assert
    assert not target.exists()


# ---------------------------------------------------------------------------
# ssh_control_options — fall-through when control_dir parent is read-only
# ---------------------------------------------------------------------------


@pytest.fixture
def readonly_parent_dir(tmp_path):
    """tmp_path/ro-parent with mode 0o500 (no write) — auto-restored."""
    parent = tmp_path / "ro-parent"
    parent.mkdir()
    parent.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        yield parent
    finally:
        parent.chmod(stat.S_IRWXU)


def test_ssh_control_options_falls_through_when_mkdir_fails(
    readonly_parent_dir, env_save_restore
):
    # Arrange — read-only parent makes ``mkdir(parents=True)`` fail.
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    target = readonly_parent_dir / "cm"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=target)

    # Assert — empty list (no exception); ssh argv stays byte-identical
    # to pre-patch behavior.
    assert opts == []


def test_ssh_control_options_does_not_partially_create_target_on_eros(
    readonly_parent_dir, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    target = readonly_parent_dir / "cm"
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    ssh_control_options(control_dir=target)

    # Assert
    assert not target.exists()


# ---------------------------------------------------------------------------
# ssh_control_options — default (no arg, no env) lives under $TMPDIR
# ---------------------------------------------------------------------------


def test_ssh_control_options_default_dir_starts_with_tempdir(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options()
    control_path = next(o for o in opts if o.startswith("ControlPath="))
    path = control_path.split("=", 1)[1]

    # Assert
    assert path.startswith(tempfile.gettempdir())


def test_ssh_control_options_default_dir_ends_with_sac_ssh_cm_percent_C(
    env_save_restore,
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options()
    control_path = next(o for o in opts if o.startswith("ControlPath="))
    path = control_path.split("=", 1)[1]

    # Assert
    assert path.endswith("/.sac-ssh-cm/%C")


# ---------------------------------------------------------------------------
# ssh_control_options_str — shell-quoted convenience
# ---------------------------------------------------------------------------


def test_ssh_control_options_str_shlex_split_matches_options_list(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    env_save_restore.delete("SAC_SSH_CONTROL_DIR")
    from scitex_agent_container._state.host_config import (
        ssh_control_options,
        ssh_control_options_str,
    )

    # Act
    s = ssh_control_options_str(control_dir=tmp_path / "cm")
    expected = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert — shlex.split of the rendered string returns the exact opts list.
    assert shlex.split(s) == expected


def test_ssh_control_options_str_is_empty_when_opted_out(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container._state.host_config import ssh_control_options_str

    # Act
    s = ssh_control_options_str()

    # Assert — empty string so `ssh $(sac host ssh-opts) host cmd` is safe.
    assert s == ""


# ---------------------------------------------------------------------------
# build_ssh_argv — central call site wired through ssh_control_options()
# ---------------------------------------------------------------------------


@pytest.fixture
def mba_peer():
    from scitex_agent_container._state.host_config import PeerSpec

    return {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}


def test_build_ssh_argv_includes_ControlMaster_auto(
    tmp_path, env_save_restore, mba_peer
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], mba_peer)

    # Assert
    assert "ControlMaster=auto" in argv


def test_build_ssh_argv_includes_ControlPersist_60s(
    tmp_path, env_save_restore, mba_peer
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], mba_peer)

    # Assert
    assert "ControlPersist=60s" in argv


def test_build_ssh_argv_control_path_ends_with_percent_C(
    tmp_path, env_save_restore, mba_peer
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], mba_peer)
    cp = next(a for a in argv if a.startswith("ControlPath="))

    # Assert
    assert cp.endswith("/%C")


def test_build_ssh_argv_opt_out_has_no_control_options(env_save_restore, mba_peer):
    # Arrange — escape hatch keeps existing ssh-config-based multiplexing
    # setups working as-is; the rendered argv carries no ``Control*``.
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], mba_peer)

    # Assert
    assert not any("Control" in a for a in argv)


def test_build_ssh_argv_caller_extra_opts_emit_two_ControlPersist_entries(
    tmp_path, env_save_restore, mba_peer
):
    # Arrange — both the package default and the caller override should
    # appear in the rendered argv (ssh's last-flag-wins decides at parse).
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv(
        "mba",
        ["echo", "hi"],
        mba_peer,
        extra_opts=["-o", "ControlPersist=300s"],
    )
    persist_positions = [i for i, a in enumerate(argv) if "ControlPersist=" in a]

    # Assert
    assert len(persist_positions) == 2


def test_build_ssh_argv_caller_extra_opts_position_after_package_default(
    tmp_path, env_save_restore, mba_peer
):
    # Arrange — caller-supplied value MUST come AFTER the package default so
    # ssh's last-flag-wins semantics give the caller's value precedence.
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Act
    argv = build_ssh_argv(
        "mba",
        ["echo", "hi"],
        mba_peer,
        extra_opts=["-o", "ControlPersist=300s"],
    )
    persist_positions = [i for i, a in enumerate(argv) if "ControlPersist=" in a]
    later = max(persist_positions)

    # Assert
    assert argv[later] == "ControlPersist=300s"


# ---------------------------------------------------------------------------
# Direct-ssh sites — verified via PATH-shim, NOT mocked.
# Each helper invokes real ``subprocess.run(["ssh", ...])``; the shim
# fixture installs a fake ``ssh`` at the front of $PATH, the real
# subprocess machinery exec's it, and the fake records its argv to a log.
# ---------------------------------------------------------------------------


def test_priority_cmds_ssh_start_agent_argv_includes_ControlMaster_auto(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0)
    from scitex_agent_container.cli_pkg.priority_cmds import _ssh_start_agent

    # Act
    _ssh_start_agent("some-host", "agent-x")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert "ControlMaster=auto" in argv


def test_priority_cmds_ssh_start_agent_argv_includes_control_path(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0)
    from scitex_agent_container.cli_pkg.priority_cmds import _ssh_start_agent

    # Act
    _ssh_start_agent("some-host", "agent-x")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert any(isinstance(a, str) and a.startswith("ControlPath=") for a in argv)


def test_send_preflight_default_ssh_runner_argv_includes_ControlMaster_auto(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="")
    from scitex_agent_container.cli_pkg._send_preflight import default_ssh_runner

    # Act
    default_ssh_runner("some-host", "/remote/creds.json")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert "ControlMaster=auto" in argv


def test_send_preflight_default_ssh_runner_argv_includes_control_path(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="")
    from scitex_agent_container.cli_pkg._send_preflight import default_ssh_runner

    # Act
    default_ssh_runner("some-host", "/remote/creds.json")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert any(isinstance(a, str) and a.startswith("ControlPath=") for a in argv)


def test_send_preflight_default_ssh_runner_argv_includes_peer_host(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — sanity check that the rest of the argv (peer_host,
    # python3 probe) survives the option prepending.
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="")
    from scitex_agent_container.cli_pkg._send_preflight import default_ssh_runner

    # Act
    default_ssh_runner("my-peer-host", "/remote/creds.json")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert "my-peer-host" in argv


def test_send_preflight_default_ssh_runner_argv_invokes_python3_probe(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout="")
    from scitex_agent_container.cli_pkg._send_preflight import default_ssh_runner

    # Act
    default_ssh_runner("some-host", "/remote/creds.json")
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert "python3" in argv


def test_network_peer_post_turn_via_ssh_argv_includes_ControlMaster_auto(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — fake ssh emits the JSON envelope the parser expects so
    # ``_post_turn_via_ssh`` reaches its return path; we then inspect argv.
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout='{"text": "ok"}')
    from scitex_agent_container._network.peer import _post_turn_via_ssh

    # Act
    _post_turn_via_ssh(
        "ssh://example.invalid:9999/v1/turn",
        text="hello",
        exit_after=False,
        timeout_s=5,
    )
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert "ControlMaster=auto" in argv


def test_network_peer_post_turn_via_ssh_argv_includes_control_path(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout='{"text": "ok"}')
    from scitex_agent_container._network.peer import _post_turn_via_ssh

    # Act
    _post_turn_via_ssh(
        "ssh://example.invalid:9999/v1/turn",
        text="hello",
        exit_after=False,
        timeout_s=5,
    )
    argv = subprocess_shim.argv_for("ssh")

    # Assert
    assert any(isinstance(a, str) and a.startswith("ControlPath=") for a in argv)


def test_network_peer_post_turn_via_ssh_returns_parsed_text(
    tmp_path, env_save_restore, subprocess_shim
):
    # Arrange — sanity check the full happy path through the shim.
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    subprocess_shim.install("ssh", exit=0, stdout='{"text": "ok"}')
    from scitex_agent_container._network.peer import _post_turn_via_ssh

    # Act
    reply = _post_turn_via_ssh(
        "ssh://example.invalid:9999/v1/turn",
        text="hello",
        exit_after=False,
        timeout_s=5,
    )

    # Assert
    assert reply == "ok"


# ---------------------------------------------------------------------------
# CLI — sac host ssh-opts
# ---------------------------------------------------------------------------


def test_sac_host_ssh_opts_exit_code_is_zero(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])

    # Assert
    assert result.exit_code == 0


def test_sac_host_ssh_opts_output_parses_to_three_opt_pairs(tmp_path, env_save_restore):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])
    parsed = shlex.split(result.output.strip())

    # Assert
    assert parsed[0::2] == ["-o", "-o", "-o"]


def test_sac_host_ssh_opts_output_includes_ControlMaster_auto(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])
    parsed = shlex.split(result.output.strip())

    # Assert
    assert "ControlMaster=auto" in parsed


def test_sac_host_ssh_opts_output_includes_ControlPersist_60s(
    tmp_path, env_save_restore
):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    env_save_restore.delete("SAC_SSH_CONTROL_MASTER")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])
    parsed = shlex.split(result.output.strip())

    # Assert
    assert "ControlPersist=60s" in parsed


def test_sac_host_ssh_opts_empty_output_when_opted_out(env_save_restore):
    # Arrange — empty stdout so `ssh $(sac host ssh-opts) host cmd` is a no-op.
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])

    # Assert
    assert result.output.strip() == ""


def test_sac_host_ssh_opts_exit_code_zero_when_opted_out(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])

    # Assert
    assert result.exit_code == 0
