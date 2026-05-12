"""Tests for scitex_agent_container._state.host_config (F-CS12).

Covers:
- ``load`` reads config.yaml or returns sensible defaults on missing file.
- ``Config.canonical_host`` resolution chain: env > config > alias > hostname.
- ``Config.validate`` flags via-references to unknown peers and bad fallbacks.
- ``sac host show`` / ``host list`` / ``host validate`` end-to-end.
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


@pytest.fixture
def cfg_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "config.yaml"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    import importlib

    import scitex_agent_container._state.host_config as mod

    importlib.reload(mod)
    return p


def test_load_missing_file_yields_defaults(cfg_path: Path):
    cfg = load()
    assert cfg.host.aliases == {}
    assert cfg.host.fallback == "hostname-short"
    assert cfg.peers == {}
    # source_path points to where it WOULD load from, even if missing.
    assert cfg.source_path == cfg_path


def test_load_parses_full_yaml(cfg_path: Path):
    cfg_path.write_text(
        """
host:
  canonical: $SAC_HOST
  aliases:
    Yusukes-MacBook-Air: mba
    spartan-login1: spartan
  fallback: hostname-short

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
    cfg = load()
    assert cfg.host.aliases == {
        "Yusukes-MacBook-Air": "mba",
        "spartan-login1": "spartan",
    }
    assert set(cfg.peers) == {"mba", "spartan", "bm198"}
    assert cfg.peers["spartan"].via == ("mba",)
    assert cfg.peers["bm198"].via == ("mba", "spartan")
    # Jump chain renders ssh targets in order.
    assert cfg.peers["bm198"].jump_chain(cfg.peers) == [
        "ywatanabe@mba.local",
        "ywatanabe@spartan-login1",
    ]


def test_canonical_host_env_override_wins(cfg_path: Path, monkeypatch):
    monkeypatch.setenv("SAC_HOST", "explicit-override")
    cfg = Config(host=HostBlock(canonical="from-yaml"))
    assert cfg.canonical_host() == "explicit-override"


def test_canonical_host_uses_yaml_when_no_env(monkeypatch):
    monkeypatch.delenv("SAC_HOST", raising=False)
    cfg = Config(host=HostBlock(canonical="from-yaml"))
    assert cfg.canonical_host() == "from-yaml"


def test_canonical_host_uses_alias(monkeypatch):
    monkeypatch.delenv("SAC_HOST", raising=False)
    import socket

    raw = socket.gethostname().split(".")[0]
    cfg = Config(host=HostBlock(aliases={raw: "aliased-name"}))
    assert cfg.canonical_host() == "aliased-name"


def test_canonical_host_falls_back_to_hostname(monkeypatch):
    """No env, no yaml canonical, no alias hit → hostname -s."""
    monkeypatch.delenv("SAC_HOST", raising=False)
    cfg = Config()
    import socket

    assert cfg.canonical_host() == socket.gethostname().split(".")[0]


def test_canonical_host_treats_placeholder_as_unset(monkeypatch):
    """``host.canonical: $SAC_HOST`` with no env should NOT win — the
    placeholder string is reserved for opt-in env override."""
    monkeypatch.delenv("SAC_HOST", raising=False)
    cfg = Config(host=HostBlock(canonical="$SAC_HOST"))
    import socket

    assert cfg.canonical_host() == socket.gethostname().split(".")[0]


def test_validate_flags_unknown_via_peer():
    cfg = Config(
        peers={
            "spartan": PeerSpec(name="spartan", ssh="x@spartan", via=("nope",)),
        }
    )
    errors = cfg.validate()
    assert any("via=" in e and "'nope'" in e for e in errors)


def test_validate_flags_missing_ssh():
    cfg = Config(peers={"x": PeerSpec(name="x", ssh="")})
    errors = cfg.validate()
    assert any("ssh: is required" in e for e in errors)


def test_validate_flags_bad_fallback():
    cfg = Config(host=HostBlock(fallback="garbage"))
    errors = cfg.validate()
    assert any("host.fallback" in e for e in errors)


def test_load_rejects_non_mapping_top_level(cfg_path: Path):
    cfg_path.write_text("- a list, not a map\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        load()


def test_load_rejects_non_list_via(cfg_path: Path):
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x
    via: not-a-list
"""
    )
    with pytest.raises(ValueError, match="via:"):
        load()


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_host_show_renders_canonical(cfg_path: Path, monkeypatch):
    monkeypatch.setenv("SAC_HOST", "smoke-host")
    from scitex_agent_container.cli_pkg.host_group import host_show

    runner = CliRunner()
    result = runner.invoke(host_show, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["canonical"] == "smoke-host"


def test_host_list_empty_by_default(cfg_path: Path):
    from scitex_agent_container.cli_pkg.host_group import host_list

    runner = CliRunner()
    result = runner.invoke(host_list, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["peers"] == []


def test_host_list_renders_peers(cfg_path: Path):
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

    runner = CliRunner()
    result = runner.invoke(host_list, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    names = sorted(p["name"] for p in body["peers"])
    assert names == ["mba", "spartan"]
    spartan = next(p for p in body["peers"] if p["name"] == "spartan")
    assert spartan["via"] == ["mba"]


def test_host_validate_passes_for_clean_config(cfg_path: Path):
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    from scitex_agent_container.cli_pkg.host_group import host_validate

    runner = CliRunner()
    result = runner.invoke(host_validate, ["--json"])
    assert result.exit_code == 0
    body = json.loads(result.output)
    assert body["errors"] == []


def test_host_validate_fails_with_unknown_via(cfg_path: Path):
    cfg_path.write_text(
        """
peers:
  spartan:
    ssh: x@spartan
    via: [does-not-exist]
"""
    )
    from scitex_agent_container.cli_pkg.host_group import host_validate

    runner = CliRunner()
    result = runner.invoke(host_validate, ["--json"])
    assert result.exit_code == 1
    body = json.loads(result.output)
    assert body["errors"]
    assert "does-not-exist" in body["errors"][0]


# ---------------------------------------------------------------------------
# F-CS12 phase 2 — build_ssh_argv + host exec / probe
# ---------------------------------------------------------------------------


def test_build_ssh_argv_single_hop():
    from scitex_agent_container._state.host_config import (
        PeerSpec,
        build_ssh_argv,
    )

    peers = {"mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local")}
    argv = build_ssh_argv("mba", ["agent", "list"], peers)
    # No -J (no jumps).
    assert "-J" not in argv
    assert "ywatanabe@mba.local" in argv
    # Required defensive ssh options applied.
    assert "BatchMode=yes" in argv
    assert "ConnectTimeout=10" in argv
    # Command appended after `--`.
    assert argv[-3:] == ["--", "agent", "list"]


def test_build_ssh_argv_multi_hop_renders_proxy_jump():
    from scitex_agent_container._state.host_config import (
        PeerSpec,
        build_ssh_argv,
    )

    peers = {
        "mba": PeerSpec(name="mba", ssh="ywatanabe@mba.local"),
        "spartan": PeerSpec(
            name="spartan",
            ssh="ywatanabe@spartan-login1",
            via=("mba",),
        ),
        "bm198": PeerSpec(name="bm198", ssh="bm198", via=("mba", "spartan")),
    }
    argv = build_ssh_argv("bm198", ["sac", "agent", "list"], peers)
    j_idx = argv.index("-J")
    assert argv[j_idx + 1] == "ywatanabe@mba.local,ywatanabe@spartan-login1"
    # Final shape: [..., "bm198", "--", "sac", "agent", "list"]
    sep = argv.index("--")
    assert argv[sep - 1] == "bm198"
    assert argv[sep + 1 :] == ["sac", "agent", "list"]


def test_build_ssh_argv_unknown_peer_raises():
    from scitex_agent_container._state.host_config import build_ssh_argv

    with pytest.raises(KeyError):
        build_ssh_argv("ghost", ["echo", "hi"], {})


def test_host_exec_unknown_peer_exits_2(cfg_path: Path):
    from scitex_agent_container.cli_pkg.host_group import host_exec

    runner = CliRunner()
    result = runner.invoke(host_exec, ["ghost", "--", "echo", "hi"])
    assert result.exit_code == 2
    assert "not defined" in (result.output + (result.stderr or ""))


def test_host_exec_missing_command_exits_2(cfg_path: Path):
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    from scitex_agent_container.cli_pkg.host_group import host_exec

    runner = CliRunner()
    result = runner.invoke(host_exec, ["mba"])
    assert result.exit_code == 2


def test_host_exec_invokes_subprocess_with_built_argv(cfg_path: Path, monkeypatch):
    """The exec callback should hand build_ssh_argv's output to
    subprocess.run unchanged. Mocked so the test doesn't ssh anywhere."""
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    seen = {}

    class _Result:
        returncode = 0

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        return _Result()

    from scitex_agent_container.cli_pkg import host_group

    monkeypatch.setattr(host_group.subprocess, "run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(host_group.host_exec, ["mba", "--", "echo", "hello"])
    assert result.exit_code == 0
    assert "ywatanabe@mba.local" in seen["argv"]
    assert seen["argv"][-3:] == ["--", "echo", "hello"]


def test_host_probe_reports_reachable_with_remote_canonical(
    cfg_path: Path, monkeypatch
):
    """A successful remote ``sac host show`` returning JSON should
    surface as ``reachable=True`` with the parsed canonical name."""
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    import json as _json

    class _Result:
        returncode = 0
        stdout = _json.dumps({"canonical": "mba"})
        stderr = ""

    def _fake_run(argv, **kw):
        return _Result()

    from scitex_agent_container.cli_pkg import host_group

    monkeypatch.setattr(host_group.subprocess, "run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(host_group.host_probe, ["mba", "--json"])
    assert result.exit_code == 0
    body = _json.loads(result.output)
    assert body["reachable"] is True
    assert body["remote_canonical"] == "mba"


# ---------------------------------------------------------------------------
# F-CS12 phase 3 — split_on_flag + dispatch_remote (--on global flag)
# ---------------------------------------------------------------------------


def test_split_on_flag_no_flag_is_passthrough():
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    peer, rest = split_on_flag(["agent", "list", "--json"])
    assert peer is None
    assert rest == ["agent", "list", "--json"]


def test_split_on_flag_separated_form():
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    peer, rest = split_on_flag(["--on", "spartan", "agent", "list"])
    assert peer == "spartan"
    assert rest == ["agent", "list"]


def test_split_on_flag_equals_form():
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    peer, rest = split_on_flag(["--on=mba", "db", "show"])
    assert peer == "mba"
    assert rest == ["db", "show"]


def test_split_on_flag_missing_value_raises():
    import click as _click

    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    with pytest.raises(_click.UsageError):
        split_on_flag(["--on"])


def test_split_on_flag_keeps_other_flags():
    from scitex_agent_container.cli_pkg.host_group import split_on_flag

    peer, rest = split_on_flag(
        ["--json", "--on", "spartan", "agent", "list", "--limit", "5"]
    )
    assert peer == "spartan"
    assert rest == ["--json", "agent", "list", "--limit", "5"]


def test_dispatch_remote_unknown_peer_returns_2(cfg_path: Path):
    from scitex_agent_container.cli_pkg.host_group import dispatch_remote

    rc = dispatch_remote("ghost", ["agent", "list"])
    assert rc == 2


def test_dispatch_remote_invokes_subprocess(cfg_path: Path, monkeypatch):
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    seen = {}

    class _Result:
        returncode = 7

    def _fake_run(argv, **kw):
        seen["argv"] = argv
        return _Result()

    from scitex_agent_container.cli_pkg import host_group

    monkeypatch.setattr(host_group.subprocess, "run", _fake_run)

    rc = host_group.dispatch_remote("mba", ["agent", "list"])
    assert rc == 7
    # Remote command must always start with `sac`.
    sep = seen["argv"].index("--")
    assert seen["argv"][sep + 1] == "sac"
    assert seen["argv"][sep + 2 :] == ["agent", "list"]


def test_host_probe_reports_unreachable_on_nonzero_exit(cfg_path: Path, monkeypatch):
    cfg_path.write_text(
        """
peers:
  mba: { ssh: ywatanabe@mba.local }
"""
    )
    import json as _json

    class _Result:
        returncode = 255
        stdout = ""
        stderr = "ssh: connect to host mba.local port 22: timed out"

    def _fake_run(argv, **kw):
        return _Result()

    from scitex_agent_container.cli_pkg import host_group

    monkeypatch.setattr(host_group.subprocess, "run", _fake_run)

    runner = CliRunner()
    result = runner.invoke(host_group.host_probe, ["mba", "--json"])
    assert result.exit_code == 1
    body = _json.loads(result.output)
    assert body["reachable"] is False
    assert body["exit_code"] == 255
    assert "timed out" in body["stderr"]
