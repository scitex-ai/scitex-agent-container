"""CLI tests for ``sac host push-config``.

PA-306: no ``unittest.mock``. Real ``CliRunner``, a real config.yaml
via the ``SCITEX_AGENT_CONTAINER_CONFIG`` env override, and a real
PATH-installed ``ssh`` fake for the remote round-trip (the shared
``subprocess_shim`` for fixed replies; a hand-rolled stateful fake for
the end-to-end push, so the verify read-back sees exactly what the
write captured).

The assertions that matter are the EXIT CODES: ``--check`` is a cron
alarm, and an alarm that exits 0 on drift is a report nobody reads.
Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name
(TQ003).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._hostsync import is_generated, render_peer_config
from scitex_agent_container._state.host_config import load as load_cfg
from scitex_agent_container.cli_pkg._host_push_config import host_push_config

_NOW = datetime(2026, 7, 16, 8, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real master config with a pinned canonical name, via env override.

    ``SAC_HOST`` / ``SCITEX_AGENT_CONTAINER_HOST`` are cleared so the
    canonical-host resolution reads the file, not the agent's ambient
    identity.
    """
    p = tmp_path / "config.yaml"
    p.write_text("host:\n  canonical: master-x\npeers:\n  spartan:\n    ssh: sp\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    env_save_restore.delete("SAC_HOST")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HOST")
    return p


def _rendered_for(cfg_path: Path, *, peer: str = "spartan", sha: str = "") -> str:
    """Render what the CLI itself would render (bar the timestamp)."""
    cfg = load_cfg(cfg_path)
    master_sha = sha or hashlib.sha256(cfg_path.read_bytes()).hexdigest()
    return render_peer_config(
        peer, cfg, master_name="master-x", now=_NOW, master_sha=master_sha
    )


def _b64_block(text: str) -> str:
    b64 = base64.b64encode(text.encode()).decode()
    return f"SAC_PUSHCFG b64={b64}\nSAC_PUSHCFG end\n"


_ABSENT_BLOCK = "SAC_PUSHCFG __ABSENT__\nSAC_PUSHCFG end\n"


# ---------------------------------------------------------------------------
# --check exit codes (the cron contract)
# ---------------------------------------------------------------------------


def test_check_on_current_peer_exits_zero(cfg_path, subprocess_shim):
    # Arrange — the peer holds our render (older timestamp, same sha).
    subprocess_shim.install("ssh", stdout=_b64_block(_rendered_for(cfg_path)))
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 0


def test_check_on_stale_generated_peer_exits_one(cfg_path, subprocess_shim):
    # Arrange — generated, but stamped with an older master-config sha.
    subprocess_shim.install(
        "ssh", stdout=_b64_block(_rendered_for(cfg_path, sha="0" * 64))
    )
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert — an alarm that exits 0 on drift is not an alarm.
    assert result.exit_code == 1


def test_check_on_hand_edited_peer_exits_one(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 1


def test_check_on_absent_peer_exits_one(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_ABSENT_BLOCK)
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 1


def test_check_on_unreachable_peer_exits_two(cfg_path, subprocess_shim):
    # Arrange — UNKNOWN is neither clean nor drifted; it is its own code.
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused\n")
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_check_never_runs_the_write_snippet(cfg_path, subprocess_shim):
    # Arrange — a drifted peer under --check must stay untouched.
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    CliRunner().invoke(host_push_config, ["--check", "spartan"])
    calls = subprocess_shim.invocations("ssh")
    # Assert — --check is read-only, structurally.
    assert not any("umask 077" in " ".join(argv) for argv in calls)


def test_check_names_the_drifted_peer_in_output(cfg_path, subprocess_shim):
    # Arrange — never silent: the operator must see WHICH peer drifted.
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan"])
    # Assert
    assert "spartan" in result.output


def test_json_output_carries_the_exit_code(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_ABSENT_BLOCK)
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "spartan", "--json"])
    # Assert
    assert '"exit_code": 1' in result.output


def test_all_visits_only_concrete_non_centre_peers(
    cfg_path, subprocess_shim, env_save_restore
):
    # Arrange — the centre itself and the glob template must be skipped.
    cfg_path.write_text(
        "host:\n  canonical: master-x\n"
        "peers:\n"
        "  master-x: {ssh: master-x}\n"
        "  mba: {ssh: m}\n"
        "  spartan: {ssh: sp}\n"
        "  spartan-*: {via: [spartan]}\n"
    )
    subprocess_shim.install("ssh", stdout=_ABSENT_BLOCK)
    # Act
    result = CliRunner().invoke(host_push_config, ["--check", "--all", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert [p["peer"] for p in payload["peers"]] == ["mba", "spartan"]


# ---------------------------------------------------------------------------
# usage errors — misuse dies loudly before any ssh
# ---------------------------------------------------------------------------


def test_peer_and_all_together_is_a_usage_error(cfg_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--check", "--all", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_neither_peer_nor_all_is_a_usage_error(cfg_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--check"])
    # Assert
    assert result.exit_code == 2


def test_adopt_with_check_is_a_usage_error(cfg_path):
    # Arrange — --adopt mutates; --check is read-only.
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--check", "--adopt", "spartan"])
    # Assert
    assert result.exit_code == 2


def test_adopt_with_all_is_a_usage_error(cfg_path):
    # Arrange — adopting fleet-wide contradicts its surgical contract.
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--adopt", "--all"])
    # Assert
    assert result.exit_code == 2


def test_glob_peer_argument_is_a_usage_error(cfg_path):
    # Arrange — a template is not a host.
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--check", "spartan-*"])
    # Assert
    assert result.exit_code == 2


def test_pushing_to_the_master_itself_is_a_usage_error(cfg_path):
    # Arrange — the master's config.yaml is the hand-edited SSOT.
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["master-x"])
    # Assert
    assert result.exit_code == 2


def test_unknown_peer_is_a_usage_error(cfg_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["--check", "ghost"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# push mode — refusal path (fixed-reply fake)
# ---------------------------------------------------------------------------


def test_push_refuses_hand_edited_with_exit_one(cfg_path, subprocess_shim):
    # Arrange
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    result = CliRunner().invoke(host_push_config, ["spartan"])
    # Assert
    assert result.exit_code == 1


def test_push_hand_edited_refusal_dispatches_no_write(cfg_path, subprocess_shim):
    # Arrange — the refusal must be structural, not cosmetic.
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    CliRunner().invoke(host_push_config, ["spartan"])
    calls = subprocess_shim.invocations("ssh")
    # Assert
    assert not any("umask 077" in " ".join(argv) for argv in calls)


def test_push_hand_edited_refusal_shows_the_diff(cfg_path, subprocess_shim):
    # Arrange — the diff prints WITHOUT --diff: nobody should have to
    # re-run to see what a refusal was protecting.
    subprocess_shim.install("ssh", stdout=_b64_block("peers: {}\n"))
    # Act
    result = CliRunner().invoke(host_push_config, ["spartan"])
    # Assert
    assert "rendered (master truth)" in result.output


def test_push_unreachable_peer_exits_two(cfg_path, subprocess_shim):
    # Arrange — UNDETERMINED never mutates and never passes as clean.
    subprocess_shim.install("ssh", exit=255, stderr="ssh: connect: refused\n")
    # Act
    result = CliRunner().invoke(host_push_config, ["spartan"])
    # Assert
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# push mode — end-to-end with a stateful fake ssh: the verify read-back
# sees exactly the bytes the write captured, real stdin piping included.
# ---------------------------------------------------------------------------


@pytest.fixture
def stateful_ssh(tmp_path: Path, env_save_restore) -> Path:
    """A REAL ``ssh`` fake on PATH backed by one peer 'filesystem' file.

    Write dispatches (identified by the write snippet's ``umask 077``)
    capture stdin into the state file; read dispatches emit the
    marker-framed base64 of that file, or ``__ABSENT__`` when it does
    not exist. Returns the state-file path.
    """
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    state = tmp_path / "peer-config.yaml"
    script = bin_dir / "ssh"
    script.write_text(
        "#!/bin/sh\n"
        f'state="{state}"\n'
        'case "$*" in\n'
        "  *'umask 077'*) cat > \"$state\"; exit 0 ;;\n"
        "esac\n"
        'if [ -f "$state" ]; then\n'
        "  printf 'SAC_PUSHCFG b64=%s\\n' \"$(base64 < \"$state\" | tr -d '\\n')\"\n"
        "else\n"
        "  printf 'SAC_PUSHCFG __ABSENT__\\n'\n"
        "fi\n"
        "printf 'SAC_PUSHCFG end\\n'\n"
    )
    script.chmod(0o755)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    return state


def test_push_creates_config_on_absent_peer_and_exits_zero(cfg_path, stateful_ssh):
    # Arrange
    runner = CliRunner()
    # Act — real subprocess, real PATH lookup, real stdin piping.
    result = runner.invoke(host_push_config, ["spartan"])
    # Assert — created AND verified by read-back.
    assert result.exit_code == 0


def test_pushed_peer_file_carries_the_generated_header(cfg_path, stateful_ssh):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(host_push_config, ["spartan"])
    # Assert — the peer-side file is renderer output.
    assert is_generated(stateful_ssh.read_text())


def test_check_after_push_reports_current(cfg_path, stateful_ssh):
    # Arrange — a push has landed on the peer.
    runner = CliRunner()
    runner.invoke(host_push_config, ["spartan"])
    # Act
    result = runner.invoke(host_push_config, ["--check", "spartan"])
    # Assert — the loop closes: what was pushed reads back CURRENT.
    assert result.exit_code == 0


def test_adopt_replaces_a_hand_edited_peer_file(cfg_path, stateful_ssh):
    # Arrange — the peer holds a hand-written config.
    stateful_ssh.write_text("peers:\n  relic: {ssh: old}\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(host_push_config, ["spartan", "--adopt"])
    # Assert
    assert result.exit_code == 0


def test_adopt_on_current_peer_is_refused_nonzero(cfg_path, stateful_ssh):
    # Arrange — nothing hand-edited to adopt once the push has landed.
    runner = CliRunner()
    runner.invoke(host_push_config, ["spartan"])
    # Act
    result = runner.invoke(host_push_config, ["spartan", "--adopt"])
    # Assert
    assert result.exit_code == 1
