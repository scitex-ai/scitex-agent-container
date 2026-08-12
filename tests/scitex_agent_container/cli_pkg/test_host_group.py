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
import logging
from pathlib import Path

import click
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


@pytest.fixture
def empty_registry(tmp_path: Path, env_save_restore) -> Path:
    """A real, EMPTY scitex-dev host registry at $SCITEX_DIR/dev/hosts.yaml.

    ``sac host list`` now reports config peers UNION the registry's
    routable hosts, so any test asserting on the peers block has to pin
    the registry half. Without this the assertions read the OPERATOR's
    real hosts.yaml and their outcome depends on which machine CI runs
    on — the suite passed here only because the ambient registry happened
    to be empty in the environment where these tests were written.
    """
    hosts_dir = tmp_path / "registry" / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text("hosts: {}\n")
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "registry"))
    return hosts_dir / "hosts.yaml"


@pytest.fixture
def registry_with_one_host(tmp_path: Path, env_save_restore) -> Path:
    """A registry declaring exactly one routable host and one unroutable."""
    hosts_dir = tmp_path / "registry" / "dev"
    hosts_dir.mkdir(parents=True)
    (hosts_dir / "hosts.yaml").write_text(
        "hosts:\n"
        "  mba:\n"
        "    kind: workstation\n"
        "    ssh_alias: mba\n"
        '    scitex_root: "~/.scitex"\n'
        "  ywata-note-win:\n"
        "    kind: workstation\n"
        "    ssh_alias: null\n"
        '    scitex_root: "~/.scitex"\n'
    )
    env_save_restore.set("SCITEX_DIR", str(tmp_path / "registry"))
    return hosts_dir / "hosts.yaml"


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


def test_host_list_rich_states_no_peers_when_nothing_is_routable(
    cfg_path: Path, empty_registry: Path
):
    """"No peers" now means neither source offered a route.

    Before the registry became a route source this only had to check
    config.yaml. A host with an empty ``peers:`` block but a populated
    registry is NOT peerless — it can reach every registry host — so the
    registry has to be empty too for this message to be truthful.
    """
    # Arrange
    cfg_path.write_text("peers: {}\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "no peers configured" in result.output


def test_host_list_rich_lists_a_registry_host_as_a_peer(
    cfg_path: Path, registry_with_one_host: Path
):
    """THE fix: a registry host is reachable without any config.yaml.

    Measured on scitex-compute-04 (2026-08-12): this command printed six
    registry rows above ``peers: []`` and ``sac host probe`` then refused
    every one of them.
    """
    # Arrange
    cfg_path.write_text("peers: {}\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "ssh=mba" in result.output


def test_host_list_rich_labels_a_registry_sourced_route(
    cfg_path: Path, registry_with_one_host: Path
):
    """The operator must be able to tell a filled-in route from their own."""
    # Arrange
    cfg_path.write_text("peers: {}\n")
    # Act
    result = CliRunner().invoke(host_list, [])
    # Assert
    assert "(registry)" in result.output


def test_host_list_omits_a_registry_host_with_no_ssh_alias(
    cfg_path: Path, registry_with_one_host: Path
):
    """``ssh_alias: null`` means no route — offering it would fail blankly."""
    # Arrange
    cfg_path.write_text("peers: {}\n")
    # Act
    result = CliRunner().invoke(host_list, ["--json"])
    # Assert
    assert "ywata-note-win" not in [p["name"] for p in json.loads(result.stdout)["peers"]]


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
    assert json.loads(result.stdout)["reachable"] is False


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
    assert json.loads(result.stdout)["remote_canonical"] is None


# ---------------------------------------------------------------------------
# `--json` payloads belong to STDOUT — the stream a `| jq` consumer reads.
# ---------------------------------------------------------------------------


_RETIRED_ALIAS_WARNING = (
    "'nas' is a RETIRED host alias — use 'scitex-nas-03'. "
    "The seeder cannot repair this on its own."
)


@pytest.fixture
def json_run_with_library_warning(cfg_path: Path, empty_registry: Path):
    """Run sac's real ``host list --json`` while a library logs a WARNING.

    Reproduces the condition that took ``pytest-matrix`` red across 18 open
    PRs. ``sac host list --json`` resolves the host registry through
    ``scitex_dev.hosts``, whose seeder logs a WARNING when it meets a RETIRED
    host alias — it names the successor and explains why it cannot repair
    itself. The warning is correctly on STDERR, so a real
    ``sac host list --json | jq`` was never broken; what broke was every
    ASSERTION written against ``result.output``. From click 8.2 that
    attribute stopped proxying stdout and became an independent stream
    MIXING stdout and stderr in write order, and scitex-logging's console
    handler re-resolves ``sys.stderr`` on every emit — so it follows click's
    isolated streams and its ``WARN:`` line interleaves ahead of the payload.

    Everything is real (PA-306, no mocks): a real ``logging`` logger, a real
    click command, and sac's real ``host list`` callback reached through
    ``ctx.invoke``. ``cfg_path`` names a file that is never written and
    ``empty_registry`` pins the OTHER route source, so the payload's
    ``peers`` list is legitimately empty on any machine — this fixture
    used to read whatever hosts.yaml the host it ran on happened to have.
    """

    @click.command("warn-then-list")
    @click.pass_context
    def warn_then_list(ctx: click.Context) -> None:
        logging.getLogger("scitex_dev.hosts._retired").warning(_RETIRED_ALIAS_WARNING)
        ctx.invoke(host_list, all_interfaces=False, as_json=True)

    return CliRunner().invoke(warn_then_list, [])


def test_json_payload_parses_from_stdout_despite_a_library_warning(
    json_run_with_library_warning,
):
    """The ``--json`` payload parses from STDOUT ALONE — what ``| jq`` gets.

    ``result.stdout``, never ``result.output``: an assertion on the merged
    stream passes while the bug is present, which is worse than no test. It
    also fails loudly under click < 8.2, where ``mix_stderr=True`` made
    ``result.stdout`` the merged stream too — that is what the ``click>=8.2``
    floor in pyproject.toml exists to guarantee.
    """
    # Arrange
    result = json_run_with_library_warning
    # Act
    payload = json.loads(result.stdout)
    # Assert
    assert payload["peers"] == []


def test_library_warning_reaches_the_merged_cli_runner_output(
    json_run_with_library_warning,
):
    """The warning IS emitted — so the sibling stdout test cannot pass vacuously.

    Its presence in ``result.output`` is also the direct evidence for why
    that attribute is unusable as a JSON source: click 8.2 interleaves the
    stderr ``WARN:`` line into it, ahead of the payload.
    """
    # Arrange
    result = json_run_with_library_warning
    # Act
    merged = result.output
    # Assert
    assert _RETIRED_ALIAS_WARNING in merged
