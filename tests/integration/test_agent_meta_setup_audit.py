"""Tests for the setup-audit and auth-rotation fields on the
``collect_rich`` payload.

These tests cover the three concerns raised alongside the claude-hud
statusline discussion (2026-04-17):

1. The plan label must come from ``rateLimitTier`` (credentials.json),
   not from ``billingType`` (claude.json). The latter only reports
   payment method (``stripe_subscription`` vs ``free``) and is not a
   plan identifier.
2. The auth-rotation log must append one line per observed
   ``expiresAt`` change, keyed on email, and must NOT duplicate when
   the same ``expiresAt`` is seen twice in a row.
3. The ``mcp_servers`` and ``installed_plugins`` lists must be
   structured (not raw JSON blobs) so the dashboard can render a
   setup-audit table.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._state.agent_meta import _parse_mcp_servers

# ---------------------------------------------------------------------------
# 1. MCP-server parser
# ---------------------------------------------------------------------------


def _write_mcp(workdir: Path, body: dict) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".mcp.json").write_text(json.dumps(body))


def test_parse_mcp_stdio_server(tmp_path: Path) -> None:
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "scitex-orochi": {
                    "type": "stdio",
                    "command": "bun",
                    "args": ["/path/to/mcp/server.ts"],
                }
            }
        },
    )
    out = _parse_mcp_servers(str(tmp_path))
    assert len(out) == 1
    e = out[0]
    assert e["name"] == "scitex-orochi"
    assert e["transport"] == "stdio"
    assert e["command"] == "bun"
    assert e["url_host"] is None


def test_parse_mcp_http_server_extracts_host(tmp_path: Path) -> None:
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "remote-hub": {
                    "type": "http",
                    "url": "https://scitex-orochi.com/mcp/sse?token=secret",
                }
            }
        },
    )
    out = _parse_mcp_servers(str(tmp_path))
    assert len(out) == 1
    assert out[0]["url_host"] == "scitex-orochi.com"
    # Regression: we extract host only, never the full URL (no secret
    # in query string).
    for v in out[0].values():
        assert v is None or "secret" not in str(v)


def test_parse_mcp_missing_file_returns_empty(tmp_path: Path) -> None:
    assert _parse_mcp_servers(str(tmp_path)) == []


def test_parse_mcp_malformed_json_returns_empty(tmp_path: Path) -> None:
    (tmp_path / ".mcp.json").write_text("not json")
    assert _parse_mcp_servers(str(tmp_path)) == []


def test_parse_mcp_no_mcp_servers_key(tmp_path: Path) -> None:
    _write_mcp(tmp_path, {"some_other_field": 1})
    assert _parse_mcp_servers(str(tmp_path)) == []


def test_parse_mcp_multi_servers(tmp_path: Path) -> None:
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "a": {"type": "stdio", "command": "bun"},
                "b": {"type": "http", "url": "https://example.com/mcp"},
            }
        },
    )
    out = _parse_mcp_servers(str(tmp_path))
    names = sorted(e["name"] for e in out)
    assert names == ["a", "b"]


# ---------------------------------------------------------------------------
# 2. Auth-rotation NDJSON log
# ---------------------------------------------------------------------------


def _setup_fake_home(tmp_path: Path, expires_at: int) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude.json").write_text(
        json.dumps(
            {
                "oauthAccount": {
                    "emailAddress": "rotator@example.com",
                    "accountUuid": "uuid-XYZ",
                    "billingType": "stripe_subscription",
                    "organizationName": "Test",
                }
            }
        )
    )
    claude_dir = home / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-DO-NOT-LEAK",
                    "refreshToken": "REFRESH",
                    "expiresAt": expires_at,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    return home


def test_rotation_log_writes_one_line_per_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same expires_at twice -> one line. Different expires_at -> two lines."""
    home = _setup_fake_home(tmp_path, expires_at=1_000_000_000_000)
    monkeypatch.setattr(Path, "home", lambda: home)

    # Use an empty workspace so the transcript-JSONL path is a no-op
    # and we are really only testing the rotation-log branch.
    workdir = tmp_path / "workspace"
    workdir.mkdir()

    from scitex_agent_container._state.agent_meta import collect_rich

    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")
    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")

    rot_file = (
        home
        / ".scitex"
        / "agent-container"
        / "auth-rotations"
        / "rotator@example.com.ndjson"
    )
    assert rot_file.is_file()
    lines = [l for l in rot_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 1, f"expected idempotent single entry, got {lines}"

    # Now rotate the token and collect again.
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-NEW",
                    "refreshToken": "REFRESH",
                    "expiresAt": 2_000_000_000_000,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")

    lines = [l for l in rot_file.read_text().splitlines() if l.strip()]
    assert len(lines) == 2
    new_entry = json.loads(lines[-1])
    assert new_entry["oauth_expires_at"] == 2_000_000_000_000
    assert new_entry["email"] == "rotator@example.com"
    assert new_entry["plan_label"] == "Max 20x"
    # No token material in the NDJSON.
    blob = rot_file.read_text().lower()
    assert "sk-ant" not in blob


def test_rotation_log_skipped_without_email(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No oauthAccount.emailAddress -> no rotation log is written."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    # Credentials present but .claude.json has no oauthAccount.
    (home / ".claude.json").write_text(json.dumps({}))
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-x",
                    "expiresAt": 1,
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    workdir = tmp_path / "workspace"
    workdir.mkdir()

    from scitex_agent_container._state.agent_meta import collect_rich

    collect_rich(name="agent-y", workdir=str(workdir), session="agent-y")

    # The rotations directory should not have been created.
    rot_dir = home / ".scitex" / "agent-container" / "auth-rotations"
    assert not rot_dir.exists()


# ---------------------------------------------------------------------------
# 3. Payload shape: plan label + plugins + mcp_servers
# ---------------------------------------------------------------------------


def test_collect_rich_exposes_plan_and_plugins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_fake_home(tmp_path, expires_at=1234)
    # Add plugins file.
    plugins_dir = home / ".claude" / "plugins"
    plugins_dir.mkdir()
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "claude-hud@claude-hud": [{"scope": "user", "version": "0.0.10"}]
                }
            }
        )
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    workdir = tmp_path / "workspace"
    _write_mcp(
        workdir,
        {"mcpServers": {"scitex-orochi": {"type": "stdio", "command": "bun"}}},
    )

    from scitex_agent_container._state.agent_meta import collect_rich

    payload = collect_rich(name="agent-z", workdir=str(workdir), session="agent-z")

    assert payload["account_email"] == "rotator@example.com"
    assert payload["account_plan_label"] == "Max 20x"
    assert payload["account_subscription_type"] == "max"
    assert payload["account_rate_limit_tier"] == "default_claude_max_20x"
    assert payload["oauth_expires_at"] == 1234
    assert (
        payload["installed_plugins"]
        and payload["installed_plugins"][0]["name"] == "claude-hud@claude-hud"
    )
    assert payload["mcp_servers"] == [
        {
            "name": "scitex-orochi",
            "transport": "stdio",
            "url_host": None,
            "command": "bun",
        }
    ]
