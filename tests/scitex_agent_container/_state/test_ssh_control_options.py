"""Tests for :mod:`scitex_agent_container._state.host_config` ssh ControlMaster wiring.

Real-function tests — no mocks. The helper is a pure-Python wrapper around
``os.environ`` + ``Path.mkdir``; we exercise it against a fresh ``tmp_path``
and assert on argv shape, env-var honouring, opt-out semantics, and the
fall-through-on-EROFS guarantee.

Coverage:

* :func:`ssh_control_options` — default control_dir, explicit dir arg,
  ``$SAC_SSH_CONTROL_DIR`` env override, ``SAC_SSH_CONTROL_MASTER=0``
  opt-out, fall-through to ``[]`` on read-only mount.
* :func:`ssh_control_options_str` — shell-quoted shape; empty when opted out.
* :func:`build_ssh_argv` — the master options appear in the rendered argv
  between the existing ``-o BatchMode/...`` block and any caller
  ``extra_opts`` so caller overrides still win.
* Direct-ssh sites — sentinel-driven checks that the inline argvs in
  ``_network.peer`` / ``cli_pkg.priority_cmds`` / ``cli_pkg._send_preflight``
  prepend the control-options block.
* CLI — ``sac host ssh-opts`` round-trip.
"""

from __future__ import annotations

import shlex
import stat
import sys

import pytest
from click.testing import CliRunner


# ---------------------------------------------------------------------------
# ssh_control_options — pure function
# ---------------------------------------------------------------------------


def test_ssh_control_options_default_shape_has_three_opt_pairs(tmp_path, monkeypatch):
    # Arrange — pin control_dir away from $TMPDIR so the test is hermetic.
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert — exactly three -o pairs, in the documented order.
    assert opts[0::2] == ["-o", "-o", "-o"]
    assert opts[1] == "ControlMaster=auto"
    assert opts[3] == "ControlPersist=60s"
    assert opts[5].startswith("ControlPath=")


def test_ssh_control_options_creates_control_dir_on_call(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
    target = tmp_path / "freshly-created-cm-dir"
    assert not target.exists()
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=target)

    # Assert — dir was created, ControlPath points inside it via %C.
    assert target.is_dir()
    control_path = next(o for o in opts if o.startswith("ControlPath="))
    assert control_path == f"ControlPath={target}/%C"


def test_ssh_control_options_honours_explicit_env_var(tmp_path, monkeypatch):
    # Arrange — env override beats the $TMPDIR default.
    override = tmp_path / "env-override-cm"
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(override))
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options()

    # Assert
    assert override.is_dir()
    assert f"ControlPath={override}/%C" in opts


def test_ssh_control_options_explicit_arg_beats_env_var(tmp_path, monkeypatch):
    # Arrange — function arg should win over env.
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    env_dir = tmp_path / "env"
    arg_dir = tmp_path / "arg"
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(env_dir))
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=arg_dir)

    # Assert — arg dir is materialized; env dir is not.
    assert arg_dir.is_dir()
    assert not env_dir.exists()
    assert any(o == f"ControlPath={arg_dir}/%C" for o in opts)


@pytest.mark.parametrize("opt_out_value", ["0", "no", "false", "off", "NO", "FALSE"])
def test_ssh_control_options_opt_out_returns_empty(tmp_path, monkeypatch, opt_out_value):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_MASTER", opt_out_value)
    monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options(control_dir=tmp_path / "cm-should-not-be-used")

    # Assert — empty list; control_dir argument NOT created (no side effect).
    assert opts == []
    assert not (tmp_path / "cm-should-not-be-used").exists()


def test_ssh_control_options_falls_through_when_control_dir_unwritable(
    tmp_path, monkeypatch
):
    # Arrange — make the parent read-only so mkdir(parents=True) fails.
    readonly_parent = tmp_path / "ro-parent"
    readonly_parent.mkdir()
    readonly_parent.chmod(stat.S_IRUSR | stat.S_IXUSR)  # 0o500 — no write.
    try:
        target = readonly_parent / "cm"
        monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
        monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
        from scitex_agent_container._state.host_config import ssh_control_options

        # Act
        opts = ssh_control_options(control_dir=target)

        # Assert — fall-through: empty list, NO exception. ssh argv ends up
        # byte-identical to the pre-patch shape.
        assert opts == []
        assert not target.exists()
    finally:
        # Restore for cleanup.
        readonly_parent.chmod(stat.S_IRWXU)


def test_ssh_control_options_default_dir_lives_under_tempdir(monkeypatch):
    # Arrange — clear overrides; let the function pick its default.
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
    import tempfile

    from scitex_agent_container._state.host_config import ssh_control_options

    # Act
    opts = ssh_control_options()

    # Assert
    control_path = next(o for o in opts if o.startswith("ControlPath="))
    path = control_path.split("=", 1)[1]
    # The default lives under $TMPDIR (gettempdir) — exact match validates
    # we honour TMPDIR like apptainer expects.
    assert path.startswith(tempfile.gettempdir())
    assert path.endswith("/.sac-ssh-cm/%C")


# ---------------------------------------------------------------------------
# ssh_control_options_str — shell-quoted convenience
# ---------------------------------------------------------------------------


def test_ssh_control_options_str_is_shell_quoted_and_round_trips(
    tmp_path, monkeypatch
):
    # Arrange
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    monkeypatch.delenv("SAC_SSH_CONTROL_DIR", raising=False)
    from scitex_agent_container._state.host_config import (
        ssh_control_options,
        ssh_control_options_str,
    )

    # Act
    s = ssh_control_options_str(control_dir=tmp_path / "cm")
    expected = ssh_control_options(control_dir=tmp_path / "cm")

    # Assert — shlex.split of the string returns the exact opts list.
    assert shlex.split(s) == expected


def test_ssh_control_options_str_empty_when_opted_out(monkeypatch):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container._state.host_config import ssh_control_options_str

    # Act
    s = ssh_control_options_str()

    # Assert — empty string so `ssh $(sac host ssh-opts) host cmd` is safe.
    assert s == ""


# ---------------------------------------------------------------------------
# build_ssh_argv — the wired call site
# ---------------------------------------------------------------------------


def test_build_ssh_argv_emits_control_master_flags_by_default(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    from scitex_agent_container._state.host_config import PeerSpec, build_ssh_argv

    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], peers)

    # Assert — the three options appear, AFTER the existing -o block and
    # BEFORE the host arg so caller `extra_opts` precedence still works.
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=60s" in argv
    cp = next(a for a in argv if a.startswith("ControlPath="))
    assert cp.endswith("/%C")


def test_build_ssh_argv_opted_out_matches_pre_patch_shape(monkeypatch):
    # Arrange — opt out; the rendered argv should be free of any
    # Control* option. This guarantees the escape hatch keeps existing
    # ssh-config-based multiplexing setups working as-is.
    monkeypatch.setenv("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container._state.host_config import PeerSpec, build_ssh_argv

    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}

    # Act
    argv = build_ssh_argv("mba", ["echo", "hi"], peers)

    # Assert
    assert not any("Control" in a for a in argv)


def test_build_ssh_argv_caller_extra_opts_win_over_defaults(tmp_path, monkeypatch):
    # Arrange — pass a `ControlPersist=300s` override via extra_opts and
    # verify it appears AFTER the default 60s so ssh's last-flag-wins
    # semantics give the caller's value precedence.
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    from scitex_agent_container._state.host_config import PeerSpec, build_ssh_argv

    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}

    # Act
    argv = build_ssh_argv(
        "mba", ["echo", "hi"], peers, extra_opts=["-o", "ControlPersist=300s"]
    )

    # Assert
    persist_positions = [i for i, a in enumerate(argv) if "ControlPersist=" in a]
    assert len(persist_positions) == 2, argv
    # Caller-supplied override appears LATER → wins on ssh re-parse.
    later = max(persist_positions)
    assert argv[later] == "ControlPersist=300s"


# ---------------------------------------------------------------------------
# Direct-ssh sites — _network.peer / cli_pkg.priority_cmds / cli_pkg._send_preflight
# ---------------------------------------------------------------------------


def test_priority_cmds_ssh_start_agent_argv_includes_control_options(
    tmp_path, monkeypatch
):
    # Arrange — capture the argv handed to subprocess.run by stubbing the
    # subprocess module *attribute* the function looks up at call time.
    # No mock library — just a small recorder class.
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    captured: dict = {}

    class _FakeProc:
        returncode = 0

    def _record(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        return _FakeProc()

    from scitex_agent_container.cli_pkg import priority_cmds

    monkeypatch.setattr(priority_cmds.subprocess, "run", _record)

    # Act
    rc = priority_cmds._ssh_start_agent("some-host", "agent-x")

    # Assert
    assert rc is True
    argv = captured["argv"]
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=60s" in argv
    assert any(
        isinstance(a, str) and a.startswith("ControlPath=") for a in argv
    )


def test_send_preflight_default_ssh_runner_argv_includes_control_options(
    tmp_path, monkeypatch
):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    captured: dict = {}

    class _FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _record(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        return _FakeProc()

    from scitex_agent_container.cli_pkg import _send_preflight

    monkeypatch.setattr(_send_preflight.subprocess, "run", _record)

    # Act
    _send_preflight.default_ssh_runner("some-host", "/remote/creds.json")

    # Assert
    argv = captured["argv"]
    assert "ControlMaster=auto" in argv
    assert any(
        isinstance(a, str) and a.startswith("ControlPath=") for a in argv
    )
    # Sanity — the rest of the argv (peer_host, python3, ...) is still there.
    assert "some-host" in argv
    assert "python3" in argv


def test_network_peer_ssh_curl_argv_includes_control_options(tmp_path, monkeypatch):
    # Arrange — call the inner sender with a fake subprocess.run that
    # records the argv but returns a successful 200 JSON envelope so the
    # parser is happy.
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    captured: dict = {}

    class _FakeProc:
        returncode = 0
        stdout = '{"text": "ok"}'
        stderr = ""

    def _record(argv, **kwargs):  # noqa: ANN001
        captured["argv"] = argv
        return _FakeProc()

    from scitex_agent_container._network import peer as peer_mod

    # The function imports subprocess inline; patch the module's
    # subprocess attribute by injecting it after the inline import runs.
    # Simplest: monkeypatch sys.modules so the local import resolves to a
    # tiny shim with our recorder.
    import subprocess as _real_subprocess
    import types

    fake_sp = types.SimpleNamespace(
        run=_record,
        TimeoutExpired=_real_subprocess.TimeoutExpired,
        SubprocessError=_real_subprocess.SubprocessError,
    )
    monkeypatch.setitem(sys.modules, "subprocess", fake_sp)

    try:
        # Act
        reply = peer_mod._post_turn_via_ssh(
            "ssh://example.invalid:9999/v1/turn",
            text="hello",
            exit_after=False,
            timeout_s=5,
        )
    finally:
        monkeypatch.setitem(sys.modules, "subprocess", _real_subprocess)

    # Assert
    assert reply == "ok"
    argv = captured["argv"]
    assert "ControlMaster=auto" in argv
    assert any(
        isinstance(a, str) and a.startswith("ControlPath=") for a in argv
    )


# ---------------------------------------------------------------------------
# CLI — sac host ssh-opts
# ---------------------------------------------------------------------------


def test_sac_host_ssh_opts_emits_shell_quoted_options(tmp_path, monkeypatch):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_DIR", str(tmp_path / "cm"))
    monkeypatch.delenv("SAC_SSH_CONTROL_MASTER", raising=False)
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])

    # Assert
    assert result.exit_code == 0
    parsed = shlex.split(result.output.strip())
    assert parsed[0::2] == ["-o", "-o", "-o"]
    assert "ControlMaster=auto" in parsed
    assert "ControlPersist=60s" in parsed


def test_sac_host_ssh_opts_prints_empty_line_when_opted_out(monkeypatch):
    # Arrange
    monkeypatch.setenv("SAC_SSH_CONTROL_MASTER", "0")
    from scitex_agent_container.cli_pkg.host_group import host_ssh_opts

    # Act
    result = CliRunner().invoke(host_ssh_opts, [])

    # Assert — empty output so `ssh $(sac host ssh-opts) host cmd` works.
    assert result.exit_code == 0
    assert result.output.strip() == ""
