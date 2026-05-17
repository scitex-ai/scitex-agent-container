"""Tests for ``sac accounts status`` — one-shot quota snapshot.

Strategy: exercise the underlying helpers in
``scitex_agent_container.cli_pkg._account_status`` directly with
test-injected ``fetch_fn`` / ``meta_fn`` / ``run_fn`` callables (per
STX-NM002). The click command itself is a thin wrapper that we cover by
asserting the helper-level shape — going through ``CliRunner`` would add
nothing useful because the click layer only translates the helper dict
into stdout text.

AAA markers (STX-TQ007: one assert per test).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._account_status import (
    StatusError,
    build_remote_status_argv,
    collect_status,
    collect_status_remote,
    format_status_prose,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_creds(home: Path) -> None:
    """Write a placeholder ``~/.claude/.credentials.json`` so the precheck
    in :func:`collect_status` passes. The file is never parsed by these
    tests because we inject ``fetch_fn`` / ``meta_fn``.
    """
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text("{}")


@pytest.fixture
def home_with_creds(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    _seed_creds(home)
    return home


@pytest.fixture
def fake_fetch_ok():
    """Return a fake ``fetch_usage`` callable yielding a populated dict."""

    def _fetch(home=None):
        return {
            "used_pct_5h": 42.5,
            "used_pct_7d": 17.25,
            "fetched_at": "2026-05-17T00:00:00+00:00",
            "from_cache": False,
            "error": None,
        }

    return _fetch


@pytest.fixture
def fake_meta_ok():
    """Return a fake ``read_credentials_metadata`` callable."""

    def _meta(home=None):
        return {
            "email_address": "test@example.com",
            "rate_limit_tier": "tier-5h",
        }

    return _meta


# ---------------------------------------------------------------------------
# Local: happy path
# ---------------------------------------------------------------------------


def test_status_local_returns_zero_when_fetch_ok(
    home_with_creds, fake_fetch_ok, fake_meta_ok
):
    # Arrange — home seeded by fixture; fetch + meta injected.
    # Act
    snapshot = collect_status(
        home=home_with_creds, fetch_fn=fake_fetch_ok, meta_fn=fake_meta_ok
    )
    # Assert — single sentinel field; full shape covered by sibling tests.
    assert snapshot["used_pct_5h"] == 42.5


def test_status_emits_5h_pct_in_output(home_with_creds, fake_fetch_ok, fake_meta_ok):
    # Arrange
    snapshot = collect_status(
        home=home_with_creds, fetch_fn=fake_fetch_ok, meta_fn=fake_meta_ok
    )
    # Act
    rendered = format_status_prose(snapshot)
    # Assert
    assert "42.5%" in rendered


def test_status_emits_7d_pct_in_output(home_with_creds, fake_fetch_ok, fake_meta_ok):
    # Arrange
    snapshot = collect_status(
        home=home_with_creds, fetch_fn=fake_fetch_ok, meta_fn=fake_meta_ok
    )
    # Act
    rendered = format_status_prose(snapshot)
    # Assert
    assert "17.2%" in rendered  # float-format rounding of 17.25


def test_status_json_emits_used_pct_5h_key(
    home_with_creds, fake_fetch_ok, fake_meta_ok
):
    # Arrange
    snapshot = collect_status(
        home=home_with_creds, fetch_fn=fake_fetch_ok, meta_fn=fake_meta_ok
    )
    # Act
    payload = json.loads(json.dumps(snapshot))
    # Assert
    assert "used_pct_5h" in payload


def test_status_json_emits_used_pct_7d_key(
    home_with_creds, fake_fetch_ok, fake_meta_ok
):
    # Arrange
    snapshot = collect_status(
        home=home_with_creds, fetch_fn=fake_fetch_ok, meta_fn=fake_meta_ok
    )
    # Act
    payload = json.loads(json.dumps(snapshot))
    # Assert
    assert "used_pct_7d" in payload


# ---------------------------------------------------------------------------
# Local: missing credentials
# ---------------------------------------------------------------------------


def test_status_missing_creds_exits_with_error_message(tmp_path, fake_fetch_ok):
    # Arrange: fresh home, NO .credentials.json file.
    home = tmp_path / "home"
    home.mkdir()
    raised: StatusError | None = None
    # Act
    try:
        collect_status(home=home, fetch_fn=fake_fetch_ok)
    except StatusError as exc:
        raised = exc
    # Assert
    assert raised is not None and "no claude credentials at" in str(raised)


# ---------------------------------------------------------------------------
# Remote: unknown peer + ssh argv shape
# ---------------------------------------------------------------------------


def test_status_remote_host_unknown_peer_errors(tmp_path, env_save_restore):
    # Arrange: empty config dir so config.yaml is absent and config.peers is {}.
    cfg_dir = tmp_path / "agent-container"
    cfg_dir.mkdir()
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_dir / "config.yaml"))
    raised: StatusError | None = None
    # Act
    try:
        build_remote_status_argv("ghost-peer")
    except StatusError as exc:
        raised = exc
    # Assert
    assert raised is not None and "no such peer" in str(raised)


def test_status_remote_host_invokes_ssh_argv(tmp_path, env_save_restore):
    # Arrange — write a minimal config with one peer pointing at a fake host.
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("peers:\n  mba:\n    ssh: ywatanabe@mba.local\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))
    # Act
    argv = build_remote_status_argv("mba")
    # Assert — ssh target appears in argv, proving build_ssh_argv saw "mba".
    assert "ywatanabe@mba.local" in argv


# ---------------------------------------------------------------------------
# Remote: command shape carries the right verb chain
# ---------------------------------------------------------------------------


def test_status_remote_argv_carries_sac_accounts_status_command(
    tmp_path, env_save_restore
):
    # Arrange
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("peers:\n  mba:\n    ssh: u@mba\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))
    # Act
    argv = build_remote_status_argv("mba")
    # Assert — the remote command tail is the json variant of this same cmd.
    assert argv[-4:] == ["sac", "accounts", "status", "--json"]


# ---------------------------------------------------------------------------
# Remote: end-to-end with a fake run_fn
# ---------------------------------------------------------------------------


def test_status_remote_parses_json_payload(tmp_path, env_save_restore):
    # Arrange
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text("peers:\n  mba:\n    ssh: u@mba\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg_path))

    class _FakeProc:
        returncode = 0
        stdout = json.dumps({"used_pct_5h": 9.0, "used_pct_7d": 1.0})
        stderr = ""

    def _fake_run(argv, **kwargs):
        return _FakeProc()

    # Act
    snapshot = collect_status_remote("mba", run_fn=_fake_run)
    # Assert
    assert snapshot["used_pct_5h"] == 9.0
