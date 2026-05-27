"""Tests for ``sac fleet notify`` (ADR-0013 Phase 1).

PA-306 no-mocks: runs the real click command via :class:`CliRunner` and
exercises the real ``--dry-run`` envelope-build path. The wire-push
roundtrip is covered by ``tests/_state/test_lead_inbox.py``; this file
pins the CLI surface — flag parsing, exit codes, ``$SAC_NAME`` default,
and the ``--dry-run`` printout shape.

One assertion per test (PA-307).
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from scitex_agent_container.cli_pkg.fleet_group import fleet_group


def _write_lead_cfg(tmp_path: Path, env_save_restore) -> Path:
    """Real config.yaml with a lead: block; surfaced via env override."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "lead:\n  name: lead\n  host: mba\n  a2a_port: 8642\n",
    )
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


# ---------------------------------------------------------------------------
# --dry-run — exercises the real envelope+resolve path without a POST.
# ---------------------------------------------------------------------------


def test_notify_dry_run_exits_zero(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["notify", "done", "--summary", "x", "--dry-run", "--json"],
    )
    # Assert
    assert result.exit_code == 0, result.output


def test_notify_dry_run_json_includes_envelope(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["notify", "blocker", "--summary", "creds gone", "--dry-run", "--json"],
    )
    payload = json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert payload["envelope"]["method"] == "message/send"


def test_notify_dry_run_carries_kind(tmp_path: Path, env_save_restore) -> None:
    # Arrange
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["notify", "status", "--summary", "x", "--dry-run", "--json"],
    )
    payload = json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert payload["envelope"]["params"]["metadata"]["kind"] == "status"


def test_notify_dry_run_reports_lead_address(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        ["notify", "done", "--summary", "x", "--dry-run", "--json"],
    )
    payload = json.loads(result.output.strip().splitlines()[-1])
    # Assert — the dry-run output must reveal where production WOULD
    # have POSTed so the operator can sanity-check the address.
    assert payload["lead"] == {"name": "lead", "host": "mba", "a2a_port": 8642}


# ---------------------------------------------------------------------------
# Flag parsing — bad inputs surface loudly via click before any I/O.
# ---------------------------------------------------------------------------


def test_notify_rejects_unknown_kind(tmp_path: Path, env_save_restore) -> None:
    # Arrange — click.Choice rejects bad kinds with usage-error exit 2.
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["notify", "frobnicate", "--summary", "x", "--dry-run"]
    )
    # Assert
    assert result.exit_code == 2


def test_notify_no_sender_identity_is_usage_error(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — no --from-agent flag AND no SAC_NAME env. click renders
    # UsageError as exit 2.
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["notify", "done", "--summary", "x", "--dry-run"]
    )
    # Assert
    assert result.exit_code == 2


def test_notify_explicit_from_agent_wins_over_env(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — both flag and env present; flag must win.
    _write_lead_cfg(tmp_path, env_save_restore)
    env_save_restore.set("SAC_NAME", "env-name")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group,
        [
            "notify",
            "done",
            "--summary",
            "x",
            "--from-agent",
            "flag-name",
            "--dry-run",
            "--json",
        ],
    )
    payload = json.loads(result.output.strip().splitlines()[-1])
    # Assert
    assert payload["envelope"]["params"]["metadata"]["from_agent"] == "flag-name"


def test_notify_help_lists_three_kinds(tmp_path: Path, env_save_restore) -> None:
    # Arrange — the help text comes from click.Choice; pin that the
    # three kinds show in the rendered USAGE so operators can see
    # what's allowed without opening source.
    runner = CliRunner()
    # Act
    result = runner.invoke(fleet_group, ["notify", "--help"])
    # Assert
    assert "done|blocker|status" in result.output


def test_notify_surfaces_no_lead_block_as_exit_one(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — config exists, no ``lead:`` block. The dry-run path
    # calls ``resolve_lead()`` which raises ``LeadInboxError`` BEFORE
    # any POST; the CLI wraps that in ``SystemExit(1)`` so the
    # operator sees a stderr line, not a stack trace.
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers: {}\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    env_save_restore.set("SAC_NAME", "alice")
    runner = CliRunner()
    # Act
    result = runner.invoke(
        fleet_group, ["notify", "done", "--summary", "x", "--dry-run"]
    )
    # Assert
    assert result.exit_code == 1
