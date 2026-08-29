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

``result.stdout`` vs ``result.output`` — read this before "simplifying"
---------------------------------------------------------------------
Every JSON assertion below parses ``result.stdout``, never
``result.output``. Under click >= 8.2 the ``mix_stderr`` knob is gone and
``result.output`` is the MERGED stdout+stderr transcript; ``result.stdout``
is the payload stream on its own. Parsing the merged transcript asserts
"this command emitted no diagnostics at all", which is a stricter and
different contract from the one ``--json`` actually offers ("stdout carries
only the JSON"). Any log record that lands on stderr while the command runs
— scitex-logging's handler resolves ``sys.stderr`` per emit, so it follows
click's isolated streams — then gets appended after the JSON and
``json.loads`` raises ``Extra data`` at exactly ``len(payload)``.

That is not hypothetical: develop went red on 2026-08-12 with
``Extra data: line 45 column 1 (char 1036)`` and ``... (char 546)``, where
1036 and 546 are the exact byte lengths of the two clean payloads. Only the
py3.12 xdist leg failed; py3.11 and py3.13 passed the same code, because
whether a background log record lands inside a given invoke is a race.

Switching to ``result.stdout`` narrows the assertion to the real contract
WITHOUT weakening it: a genuine stdout leak still fails here, because the
junk would still be in ``result.stdout``. See
``test_sync_json_stdout_stays_pure_json_when_a_log_record_is_emitted``,
which pins that contract directly.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click
import scitex_logging
import yaml
from click.testing import CliRunner

from scitex_agent_container._state.spec_manifest import build_manifest
from scitex_agent_container.cli_pkg._fleet_sync import fleet_sync_impl
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
    payload = json.loads(result.stdout)
    # Assert
    assert payload["host"] == "local"


def test_sync_collect_emits_spec_yaml_sha256(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_config(tmp_path, env_save_restore, canonical="local", peers={})
    home = _redirect_home(tmp_path, env_save_restore)
    agents = home / ".scitex/agent-container/agents"
    _make_agent(agents, "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--collect", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "sha256" in payload["agents"]["alpha"]["files"]["spec.yaml"]


# ---------------------------------------------------------------------------
# Lead mode — agreement / divergence / unreachable.
# ---------------------------------------------------------------------------


def test_sync_exits_zero_when_all_hosts_agree(tmp_path: Path, env_save_restore) -> None:
    # Arrange — local has alpha, peer "spartan" has identical alpha.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    local_agents = home / ".scitex/agent-container/agents"
    _make_agent(
        local_agents, "alpha", spec="same\n", to_home_files={"CLAUDE.md": "x\n"}
    )
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="same\n", to_home_files={"CLAUDE.md": "x\n"})
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
    payload = json.loads(result.stdout)
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
    payload = json.loads(result.stdout)
    spec_conflict = next(
        c for c in payload["agents"]["alpha"]["conflicts"] if c["file"] == "spec.yaml"
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


def test_sync_does_not_modify_any_peer_tree(tmp_path: Path, env_save_restore) -> None:
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
    payload = json.loads(result.stdout)
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
    payload = json.loads(result.stdout)
    # Assert
    assert payload["ok"] is True


# ---------------------------------------------------------------------------
# Fail-loud edge branches — single-host, unknown peer, malformed JSON,
# unresolvable peer (with and without --allow-unresolvable).
# ---------------------------------------------------------------------------


def test_sync_exits_two_when_no_peers_to_compare(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — a "fleet" of one host has nothing to diff.
    _write_config(tmp_path, env_save_restore, canonical="local", peers={})
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 2


def test_sync_exits_two_on_unknown_peer_filter(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — --peer names a host absent from config.yaml.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--peer", "ghost"])
    # Assert
    assert result.exit_code == 2


def test_sync_exits_two_on_malformed_peer_json(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — peer returns non-JSON on stdout; never accept it.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="x\n")
    _install_ssh_shim(
        tmp_path,
        mapping={"spartan-host": {"stdout": "not json at all {{{"}},
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 2


def test_sync_exits_two_on_unresolvable_peer_without_allow(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — peer has resolve: but no static ssh: target (Phase-1 dead end).
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"hpc": {"resolve": {"source": "scitex-hpc"}}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.exit_code == 2


def test_sync_allow_unresolvable_downgrades_unresolvable_peer_to_warning(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — same unresolvable peer, but --allow-unresolvable in play.
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"hpc": {"resolve": {"source": "scitex-hpc"}}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="x\n")
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync", "--json", "--allow-unresolvable"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["unreachable"][0]["peer"] == "hpc"


# ---------------------------------------------------------------------------
# The stdout/stderr split itself — the contract `--json` actually offers.
# ---------------------------------------------------------------------------


def _diverged_fleet_probe(tmp_path: Path, env_save_restore) -> click.Command:
    """Stage a one-conflict fleet and wrap the REAL impl in a click command
    that also emits a REAL sac diagnostic while the command is running.

    No mocks (PA-306): real ``fleet_sync_impl``, real ``scitex_logging``
    logger, real handler. This is the CI failure mode in miniature — sac's
    log handler re-resolves ``sys.stderr`` on every emit, so a record emitted
    mid-command lands in click's isolated stderr, and therefore in the merged
    ``result.output``, right after the JSON.
    """
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={"spartan-host": {"stdout": _manifest_json("spartan", peer_agents)}},
        env_save_restore=env_save_restore,
    )

    @click.command("probe")
    def _probe() -> None:
        try:
            fleet_sync_impl(
                as_json=True,
                only=(),
                peer_filter=(),
                allow_unresolvable=False,
                collect=False,
                agents_dir_override=None,
            )
        except SystemExit:
            pass  # exit 1 is the expected divergence verdict; keep emitting.
        scitex_logging.getLogger("scitex_agent_container").error(
            "github_ci_poll_loop: `gh` is not installed/authenticated"
        )

    return _probe


def test_sync_json_stdout_stays_pure_json_when_a_log_record_is_emitted(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    probe = _diverged_fleet_probe(tmp_path, env_save_restore)
    runner = CliRunner()
    # Act
    result = runner.invoke(probe, [])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["ok"] is False


def test_sync_diagnostic_log_record_goes_to_stderr_not_stdout(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — guards the test above against being vacuously true: if the
    # record never reached the captured stderr, the "stdout is clean"
    # assertion would prove nothing.
    probe = _diverged_fleet_probe(tmp_path, env_save_restore)
    runner = CliRunner()
    # Act
    result = runner.invoke(probe, [])
    # Assert
    assert "github_ci_poll_loop" in result.stderr


def test_sync_text_conflict_report_keeps_stdout_empty(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — text mode exits 1; the report is a diagnostic, so stdout must
    # stay empty rather than carrying the human report (the docstring on
    # ``_render_text_conflicts`` has always promised stderr).
    _write_config(
        tmp_path,
        env_save_restore,
        canonical="local",
        peers={"spartan": {"ssh": "spartan-host"}},
    )
    home = _redirect_home(tmp_path, env_save_restore)
    _make_agent(home / ".scitex/agent-container/agents", "alpha", spec="v1\n")
    peer_agents = tmp_path / "spartan_agents"
    _make_agent(peer_agents, "alpha", spec="v2\n")
    _install_ssh_shim(
        tmp_path,
        mapping={"spartan-host": {"stdout": _manifest_json("spartan", peer_agents)}},
        env_save_restore=env_save_restore,
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["sync"])
    # Assert
    assert result.stdout == ""


# EOF
