"""CLI tests for ``sac registry sync`` — ADR-0014 anti-entropy.

PA-306 — no mocks; real CliRunner against the real click command;
real subprocess.run paths intercepted via the project-standard
``subprocess_shim`` fixture (a PATH-prepended fake ssh binary that
records argv and returns canned stdout). The fixture lives at
``tests/scitex_agent_container/_helpers/subprocess_shim.py`` and is
exposed session-wide via the root ``tests/conftest.py``.

Mocked-ssh tests:
* ``--from PEER`` parses the peer's exported JSON and imports locally.
* ``--to PEER`` serialises the local comms_nodes and pipes it to ssh.
* ``--all`` walks every static peer (pull then push), continuing on
  per-peer ssh failures.
* ``--dry-run`` never invokes ssh.

FOUR TESTS WERE REMOVED HERE ON 2026-08-28, NOT REPAIRED, and the
distinction is the point. ``comms_nodes`` moved to the shared PostgreSQL
store, so what this verb ships is the abandoned SQLite table:

* ``test_registry_sync_from_imports_peer_comms_nodes_row``,
  ``..._imports_correct_peer_host`` and ``..._stamps_source_host_from_
  peer_payload`` asserted that a pulled payload became a row
  ``lookup_comms_node`` could find. It cannot: the import writes SQLite
  and the lookup reads PostgreSQL. There is no version of those tests
  that still measures the thing they were written for, because the thing
  no longer happens.
* ``test_registry_sync_dry_run_does_not_write_imported_rows`` asserted
  ``lookup_comms_node(...) is None`` after a dry run. That now holds
  whether ssh ran or not, so the test would be PERMANENTLY GREEN while
  its name promised a dry-run guarantee it can no longer observe — the
  worse failure, because nothing forces anyone to look at it. The
  dry-run guarantee that IS still observable — ssh is never invoked —
  is covered by ``test_registry_sync_dry_run_does_not_invoke_ssh``.

What survives here is the CLI plumbing: argv construction, exit codes,
peer iteration and per-peer error handling, none of which the storage
change touched.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner


@pytest.fixture
def db_path(tmp_path: Path, env_save_restore):
    """Pin state.db under tmp_path so registrations land in our fixture."""
    p = tmp_path / "state.db"
    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(p))
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    yield p
    importlib.reload(mod)


@pytest.fixture
def cfg_path(tmp_path: Path, env_save_restore) -> Path:
    """Write a minimal config.yaml with two named peers."""
    p = tmp_path / "config.yaml"
    p.write_text(
        yaml.safe_dump(
            {
                "host": {"canonical": "local"},
                "peers": {
                    "spartan": {"ssh": "spartan-host"},
                    "mba": {"ssh": "mba-host"},
                },
            }
        )
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(p))
    return p


def _peer_export_payload(*, host: str, name: str, port: int) -> str:
    """Render the JSON a peer's `sac db export --tables comms_nodes` would emit."""
    return json.dumps(
        {
            "schema": 1,
            "exported_at": "2026-05-28T00:00:00Z",
            "since": None,
            "host": host,
            "tables": {
                # Only comms_nodes carries data — every other known table
                # has to be present-but-empty so import_state's
                # KNOWN_TABLES loop doesn't trip.
                "definitions": [],
                "instances": [],
                "instance_heartbeats": [],
                "events": [],
                "attempts": [],
                "turns": [],
                "errors": [],
                "heartbeats": [],
                "channel_events": [],
                "node_tokens": [],
                "lineage": [],
                "comms_grants": [],
                "comms_nodes": [
                    {
                        "name": name,
                        "host": host,
                        "a2a_port": port,
                        "registered_at": 1700000000.0,
                        "updated_at": 1700000000.0,
                        "source_host": None,
                        "ended_at": None,
                    }
                ],
            },
        }
    )


# ---------------------------------------------------------------------------
# --from PEER — pull
# ---------------------------------------------------------------------------


def test_registry_sync_from_exits_zero_on_success(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange
    payload = _peer_export_payload(host="spartan", name="spartan-agent", port=9001)
    subprocess_shim.install("ssh", stdout=payload, exit=0)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--from", "spartan"])
    # Assert
    assert result.exit_code == 0, result.output


def test_registry_sync_from_runs_ssh_with_expected_remote_argv(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange
    subprocess_shim.install(
        "ssh",
        stdout=_peer_export_payload(host="spartan", name="x", port=1),
        exit=0,
    )
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    runner.invoke(registry_sync, ["--from", "spartan"])
    argv = subprocess_shim.argv_for("ssh")
    # Assert — the dispatched remote command is `sac db export --tables comms_nodes`.
    assert argv[-4:] == ["sac", "db", "export", "--tables"] or argv[-5:-1] == [
        "sac",
        "db",
        "export",
        "--tables",
    ]


def test_registry_sync_from_unknown_peer_exits_non_zero(
    db_path: Path, cfg_path: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--from", "no-such-peer"])
    # Assert
    assert result.exit_code != 0


def test_registry_sync_from_unknown_peer_names_peer_in_output(
    db_path: Path, cfg_path: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--from", "no-such-peer"])
    # Assert
    assert "no-such-peer" in result.output


def test_registry_sync_from_ssh_failure_reported_non_zero(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange
    subprocess_shim.install("ssh", stdout="", stderr="boom", exit=255)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--from", "spartan"])
    # Assert
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --to PEER — push
# ---------------------------------------------------------------------------


def test_registry_sync_to_exits_zero(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange — no comms_nodes seeding: the primitive writes PostgreSQL
    # now, and what --to ships is the SQLite table, so seeding through it
    # would prove nothing about the payload this test inspects.
    subprocess_shim.install("ssh", stdout="", exit=0)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--to", "mba"])
    # Assert
    assert result.exit_code == 0, result.output


def test_registry_sync_to_invokes_remote_db_import_dash(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange — no comms_nodes seeding: the primitive writes PostgreSQL
    # now, and what --to ships is the SQLite table, so seeding through it
    # would prove nothing about the payload this test inspects.
    subprocess_shim.install("ssh", stdout="", exit=0)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    runner.invoke(registry_sync, ["--to", "mba"])
    argv = subprocess_shim.argv_for("ssh")
    # Assert
    assert argv[-4:] == ["sac", "db", "import", "-"]


# ---------------------------------------------------------------------------
# --all — both directions on every static peer, continue on failure
# ---------------------------------------------------------------------------


def test_registry_sync_all_invokes_ssh_for_every_static_peer(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange — return an empty-but-valid payload for every call.
    subprocess_shim.install(
        "ssh",
        stdout=json.dumps(
            {
                "schema": 1,
                "exported_at": "x",
                "since": None,
                "host": "peer",
                "tables": {
                    t: []
                    for t in (
                        "definitions",
                        "instances",
                        "instance_heartbeats",
                        "events",
                        "attempts",
                        "turns",
                        "errors",
                        "heartbeats",
                        "channel_events",
                        "node_tokens",
                        "lineage",
                        "comms_grants",
                        "comms_nodes",
                    )
                },
            }
        ),
        exit=0,
    )
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    runner.invoke(registry_sync, ["--all"])
    # Assert
    assert subprocess_shim.call_count("ssh") == 4


def test_registry_sync_all_per_peer_error_continues(
    db_path: Path, cfg_path: Path, tmp_path: Path, env_save_restore
) -> None:
    # Arrange — install a custom ssh shim that fails for one peer but
    # succeeds for the other, so we exercise the "continue past
    # per-peer error" branch without aborting the run.
    bin_dir = tmp_path / "_custom_shim_bin"
    bin_dir.mkdir()
    payload = json.dumps(
        {
            "schema": 1,
            "exported_at": "x",
            "since": None,
            "host": "peer",
            "tables": {
                t: []
                for t in (
                    "definitions",
                    "instances",
                    "instance_heartbeats",
                    "events",
                    "attempts",
                    "turns",
                    "errors",
                    "heartbeats",
                    "channel_events",
                    "node_tokens",
                    "lineage",
                    "comms_grants",
                    "comms_nodes",
                )
            },
        }
    )
    import sys

    script = bin_dir / "ssh"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "args = sys.argv[1:]\n"
        "# find the destination — first non-flag positional.\n"
        "dest = None\n"
        "i = 0\n"
        "while i < len(args):\n"
        "    a = args[i]\n"
        "    if a in ('-J','-o','-i','-p','-l','-F'):\n"
        "        i += 2; continue\n"
        "    if a.startswith('-'):\n"
        "        i += 1; continue\n"
        "    dest = a; break\n"
        "if dest and 'spartan' in dest:\n"
        "    sys.stderr.write('boom\\n')\n"
        "    sys.exit(255)\n"
        f"sys.stdout.write({json.dumps(payload)})\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    env_save_restore.set("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--all"])
    # Assert
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# --dry-run — no ssh, no DB writes
# ---------------------------------------------------------------------------


def test_registry_sync_dry_run_exit_zero(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange
    subprocess_shim.install("ssh", stdout="should-not-run", exit=99)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, ["--all", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_registry_sync_dry_run_does_not_invoke_ssh(
    db_path: Path, cfg_path: Path, subprocess_shim
) -> None:
    # Arrange
    subprocess_shim.install("ssh", stdout="should-not-run", exit=99)
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    runner.invoke(registry_sync, ["--all", "--dry-run"])
    # Assert
    assert subprocess_shim.call_count("ssh") == 0


# ---------------------------------------------------------------------------
# Usage — no mode flag is an error
# ---------------------------------------------------------------------------


def test_registry_sync_no_mode_flag_raises_usage_error(
    db_path: Path, cfg_path: Path
) -> None:
    # Arrange
    from scitex_agent_container.cli_pkg._registry_sync import registry_sync

    runner = CliRunner()
    # Act
    result = runner.invoke(registry_sync, [])
    # Assert
    assert result.exit_code != 0
