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

TQ cleanup: each test carries explicit Arrange / Act / Assert markers
(TQ002), spells out the behaviour in its name (TQ003), asserts exactly
one fact (TQ007), and collapses same-shape invariants into
``pytest.parametrize`` cases (TQ001).

STX-NM cleanup: no ``unittest.mock`` / monkeypatch. ``Path.home()`` is
redirected by swapping ``$HOME`` (which is what ``Path.home`` reads on
POSIX) through the shared ``env_save_restore`` fixture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._state.agent_meta import (
    _parse_mcp_servers,
    collect_rich,
)

# ---------------------------------------------------------------------------
# Shared helpers / fixtures
# ---------------------------------------------------------------------------


def _write_mcp(workdir: Path, body: dict) -> None:
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / ".mcp.json").write_text(json.dumps(body))


def _write_fake_home(
    home: Path,
    *,
    expires_at: int,
    access_token: str = "sk-ant-DO-NOT-LEAK",
    rate_limit_tier: str = "default_claude_max_20x",
    subscription_type: str = "max",
) -> None:
    home.mkdir(parents=True, exist_ok=True)
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
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": access_token,
                    "refreshToken": "REFRESH",
                    "expiresAt": expires_at,
                    "subscriptionType": subscription_type,
                    "rateLimitTier": rate_limit_tier,
                }
            }
        )
    )


# ---------------------------------------------------------------------------
# 1. MCP-server parser
# ---------------------------------------------------------------------------


@pytest.fixture
def stdio_mcp_entry(tmp_path: Path) -> dict:
    """One stdio entry parsed from a real ``.mcp.json`` on disk."""
    # Arrange
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "fleet-hub": {
                    "type": "stdio",
                    "command": "bun",
                    "args": ["/path/to/mcp/server.ts"],
                }
            }
        },
    )
    # Act
    out = _parse_mcp_servers(str(tmp_path))
    # Assert (precondition for downstream one-fact tests)
    assert len(out) == 1
    return out[0]


@pytest.mark.parametrize(
    "key,expected",
    [
        ("name", "fleet-hub"),
        ("transport", "stdio"),
        ("command", "bun"),
        ("url_host", None),
    ],
)
def test_parse_mcp_stdio_server_field(
    stdio_mcp_entry: dict, key: str, expected
) -> None:
    # Arrange — fixture parsed the stdio entry.
    # Act — fixture already invoked the parser.
    # Assert
    assert stdio_mcp_entry[key] == expected


@pytest.fixture
def http_mcp_entry(tmp_path: Path) -> dict:
    """One http entry parsed from a real ``.mcp.json`` on disk."""
    # Arrange
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "remote-hub": {
                    "type": "http",
                    "url": "https://fleet-hub.example.com/mcp/sse?token=secret",
                }
            }
        },
    )
    # Act
    out = _parse_mcp_servers(str(tmp_path))
    # Assert (precondition)
    assert len(out) == 1
    return out[0]


def test_parse_mcp_http_server_extracts_only_host(http_mcp_entry: dict) -> None:
    # Arrange — fixture parsed the http entry.
    # Act — fixture already invoked the parser.
    # Assert
    assert http_mcp_entry["url_host"] == "fleet-hub.example.com"


def test_parse_mcp_http_server_strips_query_string_token(
    http_mcp_entry: dict,
) -> None:
    """Regression: extract host only, never the secret-bearing query string."""
    # Arrange — fixture parsed the http entry.
    # Act — fixture already invoked the parser.
    # Assert
    assert not any(
        v is not None and "secret" in str(v) for v in http_mcp_entry.values()
    )


def test_parse_mcp_missing_file_returns_empty(tmp_path: Path) -> None:
    # Arrange — directory has no ``.mcp.json``.
    # Act
    result = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert result == []


def test_parse_mcp_malformed_json_returns_empty(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / ".mcp.json").write_text("not json")
    # Act
    result = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert result == []


def test_parse_mcp_no_mcp_servers_key_returns_empty(tmp_path: Path) -> None:
    # Arrange
    _write_mcp(tmp_path, {"some_other_field": 1})
    # Act
    result = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert result == []


def test_parse_mcp_multi_servers_returns_all_names(tmp_path: Path) -> None:
    # Arrange
    _write_mcp(
        tmp_path,
        {
            "mcpServers": {
                "a": {"type": "stdio", "command": "bun"},
                "b": {"type": "http", "url": "https://example.com/mcp"},
            }
        },
    )
    # Act
    out = _parse_mcp_servers(str(tmp_path))
    # Assert
    assert sorted(e["name"] for e in out) == ["a", "b"]


# ---------------------------------------------------------------------------
# 2. Auth-rotation NDJSON log
# ---------------------------------------------------------------------------


def _rotation_file(home: Path) -> Path:
    return (
        home
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "_rotations"
        / "rotator@example.com.ndjson"
    )


def _nonempty_lines(path: Path) -> list[str]:
    return [l for l in path.read_text().splitlines() if l.strip()]


@pytest.fixture
def rotation_home_after_idempotent_collect(tmp_path: Path, env_save_restore) -> Path:
    """Run ``collect_rich`` twice with the SAME credentials and return $HOME."""
    # Arrange
    home = tmp_path / "home"
    _write_fake_home(home, expires_at=1_000_000_000_000)
    env_save_restore.set("HOME", str(home))

    workdir = tmp_path / "workspace"
    workdir.mkdir()
    # Act — same credentials twice should still produce one entry.
    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")
    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")
    return home


def test_rotation_log_file_is_created_when_email_known(
    rotation_home_after_idempotent_collect: Path,
) -> None:
    # Arrange — fixture collected twice.
    # Act — already done.
    # Assert
    assert _rotation_file(rotation_home_after_idempotent_collect).is_file()


def test_rotation_log_is_idempotent_for_unchanged_expires_at(
    rotation_home_after_idempotent_collect: Path,
) -> None:
    """Same ``expiresAt`` seen twice → still exactly one line on disk."""
    # Arrange — fixture collected twice with same credentials.
    # Act — already done.
    # Assert
    assert (
        len(_nonempty_lines(_rotation_file(rotation_home_after_idempotent_collect)))
        == 1
    )


@pytest.fixture
def rotation_home_after_token_rotation(
    rotation_home_after_idempotent_collect: Path, tmp_path: Path
) -> Path:
    """Rotate the token (new expiresAt + new accessToken) and re-collect."""
    home = rotation_home_after_idempotent_collect
    # Arrange — write a *new* expiresAt.
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
    workdir = tmp_path / "workspace"
    # Act
    collect_rich(name="agent-x", workdir=str(workdir), session="agent-x")
    return home


def test_rotation_log_appends_one_line_per_observed_change(
    rotation_home_after_token_rotation: Path,
) -> None:
    # Arrange — fixture rotated then re-collected.
    # Act — already done.
    # Assert
    assert len(_nonempty_lines(_rotation_file(rotation_home_after_token_rotation))) == 2


@pytest.fixture
def last_rotation_entry(rotation_home_after_token_rotation: Path) -> dict:
    return json.loads(
        _nonempty_lines(_rotation_file(rotation_home_after_token_rotation))[-1]
    )


@pytest.mark.parametrize(
    "key,expected",
    [
        ("oauth_expires_at", 2_000_000_000_000),
        ("email", "rotator@example.com"),
        ("plan_label", "Max 20x"),
    ],
)
def test_rotation_log_entry_records_field(
    last_rotation_entry: dict, key: str, expected
) -> None:
    # Arrange — fixture loaded the last NDJSON line.
    # Act — already parsed.
    # Assert
    assert last_rotation_entry[key] == expected


def test_rotation_log_does_not_leak_access_token_material(
    rotation_home_after_token_rotation: Path,
) -> None:
    """No ``sk-ant`` prefix anywhere in the NDJSON blob."""
    # Arrange — fixture wrote rotation log.
    # Act
    blob = _rotation_file(rotation_home_after_token_rotation).read_text().lower()
    # Assert
    assert "sk-ant" not in blob


def test_rotation_log_directory_is_not_created_without_email(
    tmp_path: Path, env_save_restore
) -> None:
    """No ``oauthAccount.emailAddress`` → no rotation log written at all."""
    # Arrange — credentials present, but ``.claude.json`` has no oauthAccount.
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
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
    env_save_restore.set("HOME", str(home))
    workdir = tmp_path / "workspace"
    workdir.mkdir()
    # Act
    collect_rich(name="agent-y", workdir=str(workdir), session="agent-y")
    # Assert
    rot_dir = home / ".scitex" / "agent-container" / "accounts" / "_rotations"
    assert not rot_dir.exists()


# ---------------------------------------------------------------------------
# 3. Payload shape: plan label + plugins + mcp_servers
# ---------------------------------------------------------------------------


@pytest.fixture
def rich_payload_with_plugins_and_mcp(tmp_path: Path, env_save_restore) -> dict:
    """A ``collect_rich`` result built against a fake $HOME with a plugin
    file and a workspace that has a stdio MCP server registered.
    """
    # Arrange
    home = tmp_path / "home"
    _write_fake_home(home, expires_at=1_234)
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
    env_save_restore.set("HOME", str(home))

    workdir = tmp_path / "workspace"
    _write_mcp(
        workdir,
        {"mcpServers": {"fleet-hub": {"type": "stdio", "command": "bun"}}},
    )
    # Act
    return collect_rich(name="agent-z", workdir=str(workdir), session="agent-z")


@pytest.mark.parametrize(
    "key,expected",
    [
        ("account_email", "rotator@example.com"),
        ("account_plan_label", "Max 20x"),
        ("account_subscription_type", "max"),
        ("account_rate_limit_tier", "default_claude_max_20x"),
        ("oauth_expires_at", 1_234),
    ],
)
def test_collect_rich_exposes_account_field(
    rich_payload_with_plugins_and_mcp: dict, key: str, expected
) -> None:
    # Arrange — fixture built the payload.
    # Act — already collected.
    # Assert
    assert rich_payload_with_plugins_and_mcp[key] == expected


def test_collect_rich_lists_installed_plugin_by_name(
    rich_payload_with_plugins_and_mcp: dict,
) -> None:
    # Arrange — fixture built the payload.
    # Act
    plugins = rich_payload_with_plugins_and_mcp["installed_plugins"]
    # Assert
    assert plugins and plugins[0]["name"] == "claude-hud@claude-hud"


def test_collect_rich_emits_structured_mcp_servers_entry(
    rich_payload_with_plugins_and_mcp: dict,
) -> None:
    """``mcp_servers`` is a structured list (not a raw JSON blob)."""
    # Arrange — fixture built the payload.
    # Act — already collected.
    # Assert
    assert rich_payload_with_plugins_and_mcp["mcp_servers"] == [
        {
            "name": "fleet-hub",
            "transport": "stdio",
            "url_host": None,
            "command": "bun",
        }
    ]
