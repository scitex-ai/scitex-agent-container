"""CLI tests for the ``sac host`` noun-group rich-formatted surface.

PA-306: no ``unittest.mock``. Real ``CliRunner``, real ``tmp_path``
config.yaml (via the ``SCITEX_AGENT_CONTAINER_CONFIG`` env override),
real subprocess shim for the ``host probe`` ssh round-trip.

Targets the lines uncovered by the JSON-only end-to-end suite in
``tests/scitex_agent_container/_state/test_host_config.py`` — namely
the rich non-JSON renders for ``host list`` / ``host validate`` /
``host probe``, the config_path header branch, and the malformed-
remote-stdout fallback in ``host probe``.

Each test follows AAA (TQ002), asserts exactly one fact (TQ007), and
carries a 3+-word behaviour name (TQ003).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.host_group import (
    host_list,
    host_probe,
    host_validate,
)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# `sac host list` — rich (non-JSON) renders.
# ---------------------------------------------------------------------------


def test_host_list_rich_shows_config_path_header(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "config_path" in result.output


def test_host_list_rich_notes_missing_config_file(cfg_path: Path):
    # Arrange
    # (cfg_path env points at a file we deliberately do not create)
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "no config.yaml found" in result.output


def test_host_list_rich_prints_local_row_label(cfg_path: Path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "local" in result.output


def test_host_list_rich_renders_peer_ssh_target(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "ssh=ywatanabe@mba.local" in result.output


def test_host_list_rich_renders_via_chain_for_multi_hop(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
  spartan:
    ssh: ywatanabe@spartan-login1
    via: [mba]
"""
    )
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "via=mba" in result.output


def test_host_list_rich_states_no_peers_when_empty(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers: {}\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "no peers configured" in result.output


def test_host_list_rich_renders_alias_arrow_for_local(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
host:
  aliases:
    raw-name: friendly
"""
    )
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "raw-name  ->  friendly" in result.output


def test_host_list_all_interfaces_flag_runs_clean(cfg_path: Path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_list, ["--all-interfaces"])
    # Assert
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# `sac host validate` — rich (non-JSON) renders.
# ---------------------------------------------------------------------------


def test_host_validate_rich_prints_ok_for_clean_config(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    # Act
    result = CliRunner().invoke(host_validate, [])
    # Assert
    assert "config.yaml is valid" in result.output


def test_host_validate_rich_lists_error_for_unknown_via(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x@spartan
    via: [does-not-exist]
"""
    )
    # Act
    result = CliRunner().invoke(host_validate, [])
    # Assert
    assert "does-not-exist" in result.output


def test_host_validate_rich_exits_nonzero_on_errors(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x@spartan
    via: [does-not-exist]
"""
    )
    # Act
    result = CliRunner().invoke(host_validate, [])
    # Assert
    assert result.exit_code == 1


# ---------------------------------------------------------------------------
# `sac host probe` — unknown-peer error branch + rich renders.
# ---------------------------------------------------------------------------


def test_host_probe_unknown_peer_json_reports_unreachable(cfg_path: Path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_probe, ["ghost", "--json"])
    # Assert
    assert json.loads(result.output)["reachable"] is False


def test_host_probe_unknown_peer_json_exits_with_code_2(cfg_path: Path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_probe, ["ghost", "--json"])
    # Assert
    assert result.exit_code == 2


def test_host_probe_unknown_peer_rich_exits_with_code_2(cfg_path: Path):
    # Arrange
    # Act
    result = CliRunner().invoke(host_probe, ["ghost"])
    # Assert
    assert result.exit_code == 2


def test_host_probe_rich_reachable_prints_ok(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", stdout=json.dumps({"canonical": "mba"}), exit=0)
    # Act
    result = CliRunner().invoke(host_probe, ["mba"])
    # Assert
    assert "ok" in result.output


def test_host_probe_rich_unreachable_prints_label(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=255, stderr="connect timeout")
    # Act
    result = CliRunner().invoke(host_probe, ["mba"])
    # Assert
    assert "unreachable" in result.output


def test_host_probe_rich_unreachable_shows_stderr_tail(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=255, stderr="connect timeout")
    # Act
    result = CliRunner().invoke(host_probe, ["mba"])
    # Assert
    assert "connect timeout" in result.output


def test_host_probe_rich_unreachable_handles_empty_stderr(
    cfg_path: Path, subprocess_shim
):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=255, stderr="")
    # Act
    result = CliRunner().invoke(host_probe, ["mba"])
    # Assert
    assert result.exit_code == 1


def test_host_probe_malformed_remote_stdout_yields_null_canonical(
    cfg_path: Path, subprocess_shim
):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", stdout="not-json", exit=0)
    # Act
    result = CliRunner().invoke(host_probe, ["mba", "--json"])
    # Assert
    assert json.loads(result.output)["remote_canonical"] is None
