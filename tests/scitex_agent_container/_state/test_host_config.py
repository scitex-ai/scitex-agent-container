"""Tests for scitex_agent_container._state.host_config (F-CS12).

Covers:
- ``load`` reads config.yaml or returns sensible defaults on missing file.
- ``Config.canonical_host`` resolution chain: env > config > alias > hostname.
- ``Config.validate`` flags via-references to unknown peers and bad fallbacks.
- ``sac host list`` / ``host validate`` end-to-end.

No-mocks pattern (PA-306):
- Env mutations go through the shared ``env_save_restore`` fixture.
- Subprocess shell-outs (ssh) use the shared ``subprocess_shim`` fixture
  to install a fake binary on PATH that records its argv — the real
  ``subprocess.run`` in production code finds the shim and execs it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.host_config import (
    Config,
    HostBlock,
    PeerSpec,
    load,
)


def _parse_probe_json(result, *, expect_exit: int = 0) -> dict:
    """Resiliently parse a ``host probe --json`` invocation.

    Guards both failure surfaces so a flaky run produces an actionable
    diagnostic instead of an opaque ``JSONDecodeError`` or a bare
    ``AssertionError`` with no context:

    1. The CLI exit code matches ``expect_exit`` (probe payload is only
       trustworthy when the command ran to completion).
    2. ``result.output`` parses as a JSON object.

    Returns the parsed payload so callers assert on a structured field
    (``payload["remote_canonical"]``) rather than on exact / ordered
    output text.
    """
    assert result.exit_code == expect_exit, (
        f"host probe exit_code={result.exit_code} (expected {expect_exit}); "
        f"exception={result.exception!r}; output={result.output!r}"
    )
    try:
        payload = json.loads(result.output)
    except ValueError as exc:  # JSONDecodeError subclasses ValueError
        raise AssertionError(
            f"host probe --json did not emit parseable JSON: {exc}; "
            f"raw output={result.output!r}"
        ) from exc
    assert isinstance(payload, dict), (
        f"host probe --json payload is not an object: {payload!r}"
    )
    return payload


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml at tmp_path, surfaced via the env override."""
    p = tmp_path / "config.yaml"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


def test_load_returns_defaults_when_file_missing(cfg_path: Path):
    # Arrange
    # (cfg_path env points at a file that doesn't exist yet)
    # Act
    cfg = load()
    # Assert
    assert cfg.host.aliases == {}


def test_load_records_source_path_even_when_missing(cfg_path: Path):
    # Arrange
    # Act
    cfg = load()
    # Assert
    assert cfg.source_path == cfg_path


def test_load_parses_full_yaml_aliases(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
host:
  aliases:
    Yusukes-MacBook-Air: mba
    spartan-login1: spartan
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.host.aliases == {
        "Yusukes-MacBook-Air": "mba",
        "spartan-login1": "spartan",
    }


def test_load_parses_peers_with_jump_chains(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
  spartan:
    ssh: ywatanabe@spartan-login1
    via: [mba]
  bm198:
    ssh: bm198
    via: [mba, spartan]
"""
    )
    # Act
    cfg = load()
    # Assert
    assert set(cfg.peers) == {"mba", "spartan", "bm198"}


def test_load_renders_jump_chain_ssh_targets_in_order(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
  spartan:
    ssh: ywatanabe@spartan-login1
    via: [mba]
  bm198:
    ssh: bm198
    via: [mba, spartan]
"""
    )
    # Act
    cfg = load()
    # Assert
    assert cfg.peers["bm198"].jump_chain(cfg.peers) == [
        "ywatanabe@mba.local",
        "ywatanabe@spartan-login1",
    ]


# ---------------------------------------------------------------------------
# Config.canonical_host()
# ---------------------------------------------------------------------------


def test_canonical_host_env_override_wins_over_yaml(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_HOST", "explicit-override")
    cfg = Config(host=HostBlock(canonical="from-yaml"))
    # Act
    name = cfg.canonical_host()
    # Assert
    assert name == "explicit-override"


def test_canonical_host_uses_yaml_when_env_absent(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_HOST")
    cfg = Config(host=HostBlock(canonical="from-yaml"))
    # Act
    name = cfg.canonical_host()
    # Assert
    assert name == "from-yaml"


def test_canonical_host_resolves_via_alias_when_no_env_or_canonical(env_save_restore):
    # Arrange
    import socket

    env_save_restore.delete("SAC_HOST")
    raw = socket.gethostname().split(".")[0]
    cfg = Config(host=HostBlock(aliases={raw: "aliased-name"}))
    # Act
    name = cfg.canonical_host()
    # Assert
    assert name == "aliased-name"


def test_canonical_host_falls_back_to_short_hostname(env_save_restore):
    # Arrange
    import socket

    env_save_restore.delete("SAC_HOST")
    cfg = Config()
    # Act
    name = cfg.canonical_host()
    # Assert
    assert name == socket.gethostname().split(".")[0]


def test_canonical_host_treats_dollar_placeholder_as_unset(env_save_restore):
    # Arrange
    import socket

    env_save_restore.delete("SAC_HOST")
    cfg = Config(host=HostBlock(canonical="$SAC_HOST"))
    # Act
    name = cfg.canonical_host()
    # Assert
    assert name == socket.gethostname().split(".")[0]


# ---------------------------------------------------------------------------
# Config.validate()
# ---------------------------------------------------------------------------


def test_validate_flags_unknown_via_peer():
    # Arrange
    cfg = Config(
        peers={
            "spartan": PeerSpec(name="spartan", ssh="x@spartan", via=("nope",)),
        }
    )
    # Act
    errors = cfg.validate()
    # Assert
    assert any("via=" in e and "'nope'" in e for e in errors)


def test_validate_flags_missing_ssh():
    # Arrange
    cfg = Config(peers={"x": PeerSpec(name="x", ssh="")})
    # Act
    errors = cfg.validate()
    # Assert
    assert any("ssh: is required" in e for e in errors)


def test_validate_flags_bad_fallback():
    # Arrange
    cfg = Config(host=HostBlock(fallback="garbage"))
    # Act
    errors = cfg.validate()
    # Assert
    assert any("host.fallback" in e for e in errors)


def test_load_rejects_non_mapping_top_level(cfg_path: Path):
    # Arrange
    cfg_path.write_text("- a list, not a map\n")
    # Act
    raised = pytest.raises(ValueError, match="must be a mapping")
    # Assert
    with raised:
        load()


def test_load_rejects_non_list_via(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x
    via: not-a-list
"""
    )
    # Act
    raised = pytest.raises(ValueError, match="via:")
    # Assert
    with raised:
        load()


# ---------------------------------------------------------------------------
# CLI surface (sac host list / validate)
# ---------------------------------------------------------------------------


def test_host_list_returns_empty_peers_with_no_config(cfg_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import host_list

    # Act
    result = CliRunner().invoke(host_list, ["--json"])
    # Assert
    assert json.loads(result.output)["peers"] == []


def test_host_list_renders_configured_peers(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: ywatanabe@spartan-login1
    via: [mba]
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    from scitex_agent_container.cli_pkg.host_group import host_list

    # Act
    result = CliRunner().invoke(host_list, ["--json"])
    # Assert
    assert sorted(p["name"] for p in json.loads(result.output)["peers"]) == [
        "mba",
        "spartan",
    ]


def test_host_validate_passes_for_clean_config(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    from scitex_agent_container.cli_pkg.host_group import host_validate

    # Act
    result = CliRunner().invoke(host_validate, ["--json"])
    # Assert
    assert json.loads(result.output)["errors"] == []


def test_host_validate_fails_for_unknown_via(cfg_path: Path):
    # Arrange
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x@spartan
    via: [does-not-exist]
"""
    )
    from scitex_agent_container.cli_pkg.host_group import host_validate

    # Act
    result = CliRunner().invoke(host_validate, ["--json"])
    # Assert
    assert "does-not-exist" in json.loads(result.output)["errors"][0]


# ---------------------------------------------------------------------------
# build_ssh_argv() — pure function, no shell-out
# ---------------------------------------------------------------------------


def test_build_ssh_argv_single_hop_omits_proxy_jump():
    # Arrange
    from scitex_agent_container._state.host_config import (
        PeerSpec,
        build_ssh_argv,
    )

    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}
    # Act
    argv = build_ssh_argv("mba", ["agent", "list"], peers)
    # Assert
    assert "-J" not in argv


def test_build_ssh_argv_renders_proxy_jump_for_multi_hop():
    # Arrange
    from scitex_agent_container._state.host_config import (
        PeerSpec,
        build_ssh_argv,
    )

    peers = {
        "mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local"),
        "spartan": PeerSpec(
            name="spartan", ssh="ywatanabe@spartan-login1", via=("mba",)
        ),
        "bm198": PeerSpec(name="bm198", ssh="bm198", via=("mba", "spartan")),
    }
    # Act
    argv = build_ssh_argv("bm198", ["sac", "agent", "list"], peers)
    # Assert
    assert argv[argv.index("-J") + 1] == "ywatanabe@mba.local,ywatanabe@spartan-login1"


def test_build_ssh_argv_unknown_peer_raises_keyerror():
    from scitex_agent_container._state.host_config import build_ssh_argv

    # Arrange
    peers = {}
    # Act
    raised = pytest.raises(KeyError)
    # Assert
    with raised:
        build_ssh_argv("ghost", ["echo", "hi"], peers)


# ---------------------------------------------------------------------------
# CLI subprocess wiring — real subprocess.run against the PATH-shimmed `ssh`.
# This verifies that host_exec / host_probe / dispatch_remote do invoke ssh
# with the argv that build_ssh_argv produced.
# ---------------------------------------------------------------------------


def test_host_exec_unknown_peer_exits_with_code_2(cfg_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import host_exec

    # Act
    result = CliRunner().invoke(host_exec, ["ghost", "--", "echo", "hi"])
    # Assert
    assert result.exit_code == 2


def test_host_exec_missing_command_exits_with_code_2(cfg_path: Path):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    from scitex_agent_container.cli_pkg.host_group import host_exec

    # Act
    result = CliRunner().invoke(host_exec, ["mba"])
    # Assert
    assert result.exit_code == 2


def test_host_exec_invokes_ssh_with_built_argv(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=0)
    from scitex_agent_container.cli_pkg.host_group import host_exec

    # Act
    result = CliRunner().invoke(host_exec, ["mba", "--", "echo", "hello"])
    # Assert
    assert result.exit_code == 0


def test_host_exec_passes_ssh_target_to_ssh(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=0)
    from scitex_agent_container.cli_pkg.host_group import host_exec

    # Act
    CliRunner().invoke(host_exec, ["mba", "--", "echo", "hello"])
    # Assert
    assert "ywatanabe@mba.local" in subprocess_shim.argv_for("ssh")


def test_host_exec_appends_command_after_double_dash(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=0)
    from scitex_agent_container.cli_pkg.host_group import host_exec

    # Act
    CliRunner().invoke(host_exec, ["mba", "--", "echo", "hello"])
    # Assert
    argv = subprocess_shim.argv_for("ssh")
    assert argv[-3:] == ["--", "echo", "hello"]


def test_host_probe_reports_reachable_with_remote_canonical(
    cfg_path: Path, subprocess_shim
):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", stdout=json.dumps({"canonical": "mba"}), exit=0)
    from scitex_agent_container.cli_pkg.host_group import host_probe

    # Act
    result = CliRunner().invoke(host_probe, ["mba", "--json"])
    # Assert
    assert _parse_probe_json(result)["reachable"] is True


def test_host_probe_surfaces_parsed_remote_canonical(cfg_path: Path, subprocess_shim):
    # Arrange — remote now runs `host list --json`, which puts the
    # canonical hostname under `local.name`.
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install(
        "ssh",
        stdout=json.dumps({"local": {"name": "mba"}, "peers": []}),
        exit=0,
    )
    from scitex_agent_container.cli_pkg.host_group import host_probe

    # Act
    result = CliRunner().invoke(host_probe, ["mba", "--json"])
    # Assert
    assert _parse_probe_json(result)["remote_canonical"] == "mba"


def test_host_probe_reports_unreachable_on_nonzero_exit(
    cfg_path: Path, subprocess_shim
):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install(
        "ssh",
        exit=255,
        stderr="ssh: connect to host mba.local port 22: timed out",
    )
    from scitex_agent_container.cli_pkg.host_group import host_probe

    # Act
    result = CliRunner().invoke(host_probe, ["mba", "--json"])
    # Assert
    assert _parse_probe_json(result, expect_exit=1)["reachable"] is False


def test_host_probe_surfaces_ssh_exit_code(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=255, stderr="connect timeout")
    from scitex_agent_container.cli_pkg.host_group import host_probe

    # Act
    result = CliRunner().invoke(host_probe, ["mba", "--json"])
    # Assert
    assert _parse_probe_json(result, expect_exit=1)["exit_code"] == 255


# ---------------------------------------------------------------------------
# split_on_flag / dispatch_remote
# ---------------------------------------------------------------------------


def test_split_on_flag_returns_passthrough_when_flag_absent():
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    # Act
    peer, rest = split_on_flag(["agent", "list", "--json"])
    # Assert
    assert peer is None and rest == ["agent", "list", "--json"]


def test_split_on_flag_extracts_separated_value():
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    # Act
    peer, _ = split_on_flag(["--on", "spartan", "agent", "list"])
    # Assert
    assert peer == "spartan"


def test_split_on_flag_extracts_equals_form_value():
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    # Act
    peer, _ = split_on_flag(["--on=mba", "db", "show"])
    # Assert
    assert peer == "mba"


def test_split_on_flag_missing_value_raises_usage_error():
    import click as _click

    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    # Arrange
    argv = ["--on"]
    # Act
    raised = pytest.raises(_click.UsageError)
    # Assert
    with raised:
        split_on_flag(argv)


def test_split_on_flag_preserves_other_flags_in_rest():
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    # Act
    _, rest = split_on_flag(
        ["--json", "--on", "spartan", "agent", "list", "--limit", "5"]
    )
    # Assert
    assert rest == ["--json", "agent", "list", "--limit", "5"]


def test_dispatch_remote_unknown_peer_returns_2(cfg_path: Path):
    # Arrange
    from scitex_agent_container.cli_pkg.host_group import dispatch_remote

    # Act
    rc = dispatch_remote("ghost", ["agent", "list"])
    # Assert
    assert rc == 2


def test_dispatch_remote_invokes_ssh_with_remote_sac_command(
    cfg_path: Path, subprocess_shim
):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=7)
    from scitex_agent_container.cli_pkg.host_group import dispatch_remote

    # Act
    dispatch_remote("mba", ["agent", "list"])
    # Assert
    argv = subprocess_shim.argv_for("ssh")
    sep = argv.index("--")
    assert argv[sep + 1 :] == ["sac", "agent", "list"]


def test_dispatch_remote_propagates_ssh_exit_code(cfg_path: Path, subprocess_shim):
    # Arrange
    cfg_path.write_text("peers:\n  mba: { ssh: ywatanabe@mba.local }\n")
    subprocess_shim.install("ssh", exit=7)
    from scitex_agent_container.cli_pkg.host_group import dispatch_remote

    # Act
    rc = dispatch_remote("mba", ["agent", "list"])
    # Assert
    assert rc == 7
