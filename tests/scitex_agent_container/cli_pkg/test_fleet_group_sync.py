"""CLI tests for ``sac fleet sync`` — cross-host spec audit (no auto-merge).

PA-306 — no mocks. Real ``CliRunner`` against the real ``fleet_group``
command tree; real on-disk ``config.yaml`` via the
``SCITEX_AGENT_CONTAINER_CONFIG`` env override; real ``ssh`` calls
intercepted by a PATH-prepended shim binary that dispatches per-peer
to pre-staged manifest JSON.

The shim emulates how a real peer would respond to
``ssh peer -- sac fleet sync --collect --json`` — it doesn't actually
run ``sac`` remotely; it just reads a per-destination mapping file
(``$FLEET_SYNC_SHIM_MAP``) that the test prepares ahead of time. This
keeps tests hermetic while exercising the full real argv-build /
real-subprocess path through production.

One assertion per test (TQ007), AAA layout (TQ002).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import yaml
from click.testing import CliRunner

from scitex_agent_container._state.spec_manifest import build_manifest
from scitex_agent_container.cli_pkg.fleet_group import fleet_group


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_agent(
    agents_dir: Path,
    name: str,
    *,
    spec: str = "kind: Agent\nmetadata:\n  name: x\n",
    to_home_files: dict[str, str] | None = None,
) -> Path:
    """Lay down `<agents_dir>/<name>/{spec.yaml, to_home/...}`."""
    d = agents_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text(spec)
    if to_home_files:
        for rel, content in to_home_files.items():
            f = d / "to_home" / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content)
    return d


def _install_ssh_shim(
    tmp_path: Path,
    *,
    mapping: dict[str, dict],
    env_save_restore,
) -> Path:
    """Install a PATH-prepended ``ssh`` shim that emulates remote peers.

    ``mapping`` is destination-string -> {stdout, exit, stderr, fail}.
    The shim's destination key is parsed out of argv (the first non-flag
    positional, mirroring how real ``ssh user@host`` arguments stack).
    The map file is JSON; the shim reads it via $FLEET_SYNC_SHIM_MAP so
    tests can stage / mutate it independently of the script body.
    """
    bin_dir = tmp_path / "_shim_bin"
    bin_dir.mkdir(exist_ok=True)
    map_path = tmp_path / "shim_map.json"
    map_path.write_text(json.dumps(mapping))
    env_save_restore.set("FLEET_SYNC_SHIM_MAP", str(map_path))

    script = bin_dir / "ssh"
    body = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "dest = None\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    a = args[i]\n"
        "    if a in ('-J', '-o', '-i', '-p', '-l', '-F'):\n"
        "        i += 2; continue\n"
        "    if a.startswith('-'):\n"
        "        i += 1; continue\n"
        "    dest = a; break\n"
        "if dest is None:\n"
        "    sys.stderr.write('shim: no destination found\\n'); sys.exit(255)\n"
        "with open(os.environ['FLEET_SYNC_SHIM_MAP']) as fh:\n"
        "    mapping = json.load(fh)\n"
        "entry = mapping.get(dest)\n"
        "if entry is None:\n"
        "    sys.stderr.write(f'shim: no mapping for {dest!r}\\n'); sys.exit(255)\n"
        "if entry.get('fail'):\n"
        "    sys.stderr.write(entry.get('stderr','peer unreachable\\n'))\n"
        "    sys.exit(int(entry.get('exit', 255)))\n"
        "sys.stdout.write(entry['stdout'])\n"
        "sys.exit(0)\n"
    )
    script.write_text(body)
    script.chmod(0o755)
    saved_path = os.environ.get("PATH", "")
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{saved_path}")
    return script


def _write_config(
    tmp_path: Path,
    env_save_restore,
    *,
    canonical: str,
    peers: dict[str, dict],
) -> Path:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump({"host": {"canonical": canonical}, "peers": peers})
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))
    return cfg_path


def _redirect_home(tmp_path: Path, env_save_restore) -> Path:
    home = tmp_path / "lead_home"
    home.mkdir(exist_ok=True)
    env_save_restore.set("HOME", str(home))
    return home


def _manifest_json(host: str, agents_dir: Path) -> str:
    """Build a manifest the way a real peer's --collect would emit it."""
    return json.dumps(build_manifest(host=host, agents_dir=agents_dir))


def _tree_mtimes(root: Path) -> dict[str, int]:
    """Snapshot every file's mtime in a tree for the "did sac touch it?" assertion."""
    out: dict[str, int] = {}
    for p in root.rglob("*"):
        if p.is_file():
            out[str(p.relative_to(root))] = p.stat().st_mtime_ns
    return out


# ---------------------------------------------------------------------------
# --collect (worker mode) — local-only, no ssh.
# ---------------------------------------------------------------------------


def test_sync_collect_emits_manifest_with_local_host_name(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, canonical="local", peers={})
    home = _redirect_home(tmp_path, env_save_restore)
    agents = home / ".scitex/agent-container/agents"
    _make_agent(agents, "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--collect", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["host"] == "local"


def test_sync_collect_emits_spec_yaml_sha256(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, canonical="local", peers={})
    home = _redirect_home(tmp_path, env_save_restore)
    agents = home / ".scitex/agent-container/agents"
    _make_agent(agents, "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--collect", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert "sha256" in payload["agents"]["alpha"]["files"]["spec.yaml"]


# ---------------------------------------------------------------------------
# Lead mode — agreement / divergence / unreachable.
# ---------------------------------------------------------------------------


def test_sync_exits_zero_when_all_hosts_agree(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — local has alpha, peer "spartan" has identical alpha.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="same\n",
                to_home_files={"CLAUDE.md": "x\n"})
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="same\n",
                to_home_files={"CLAUDE.md": "x\n"})
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 0


def test_sync_exits_one_when_any_spec_yaml_differs(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 1


def test_sync_text_output_prints_fleet_conflict_header_on_divergence(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert "FLEET SPEC CONFLICT" in result.output


def test_sync_text_output_names_conflicting_file_and_host(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert "spec.yaml" in result.output and "spartan" in result.output


def test_sync_json_output_marks_overall_ok_false_on_divergence(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["ok"] is False


def test_sync_json_output_lists_diverged_hosts_for_conflict(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — local & spartan agree on v1; bm198 differs (v2).
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={
            "spartan": {"ssh": "spartan-host"},
            "bm198": {"ssh": "bm198-host"},
        },
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    spartan_agents = tmp_path / "spartan_agents"
    _make_agent(spartan_agents, "alpha", spec="v1\n")
    bm_agents = tmp_path / "bm_agents"
    _make_agent(bm_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", spartan_agents)},
            "bm198-host": {"stdout": _manifest_json("bm198", bm_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--json"])
    payload = json.loads(result.output)
    spec_conflict = next(
        c for c in payload["agents"]["alpha"]["conflicts"]
        if c["file"] == "spec.yaml"
    )
    # Assert
    assert spec_conflict["diverged_hosts"] == ["bm198"]


def test_sync_exits_two_when_peer_unreachable_no_partial_fleet(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — spartan ssh fails (exit 255).
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {
                "fail": True,
                "stdout": "",
                "stderr": "kex_exchange_identification: read: Connection reset by peer\n",
                "exit": 255,
            },
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 2


def test_sync_unreachable_peer_message_names_the_peer(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"fail": True, "stdout": "", "stderr": "boom", "exit": 255},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert "spartan" in result.output


def test_sync_does_not_modify_any_peer_tree(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    before = _tree_mtimes(peer_agents)
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", peer_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    runner.invoke(fleet_group, ["sync"])
    after = _tree_mtimes(peer_agents)
    # Assert
    assert before == after


def test_sync_missing_agent_on_peer_flagged_as_agent_missing_on_host(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — local has alpha; spartan does NOT.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    spartan_agents = tmp_path / "spartan_agents"
    spartan_agents.mkdir()
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {"stdout": _manifest_json("spartan", spartan_agents)},
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--json"])
    payload = json.loads(result.output)
    kinds = {c["kind"] for c in payload["agents"]["alpha"]["conflicts"]}
    # Assert
    assert "agent_missing_on_host" in kinds


def test_sync_only_flag_narrows_audit_to_named_agents(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — two agents differ; --only alpha hides bravo's conflict.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(local_agents, "alpha", spec="v1\n")
    _make_agent(local_agents, "bravo", spec="b1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v1\n")
    _make_agent(peer_agents, "bravo", spec="b2\n")
    # The peer manifest the shim returns is filtered to alpha only,
    # mirroring what the remote --collect would emit under --only alpha.
    _install_ssh_shim(
        tmp_path,
        mapping={
            "spartan-host": {
                "stdout": json.dumps(
                    build_manifest(
                        host="spartan", agents_dir=peer_agents, only=["alpha"]
                    )
                ),
            },
        },
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--json", "--only", "alpha"])
    payload = json.loads(result.output)
    # Assert
    assert payload["ok"] is True


# EOF
