"""Tests for cli_pkg.status_cmds (status + health + claude-account block)."""

from __future__ import annotations

import json

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.status_cmds as status_cmds
from scitex_agent_container.cli_pkg.status_cmds import (
    _format_claude_account_block,
    health,
    status,
)

# ---------------------------------------------------------------------------
# _format_claude_account_block — pure function
# ---------------------------------------------------------------------------


def test_format_account_block_returns_empty_when_all_none():
    meta = {
        "email_address": None,
        "organization_name": None,
        "display_name": None,
        "billing_type": None,
        "subscription_type": None,
        "rate_limit_tier": None,
        "has_available_subscription": None,
        "has_extra_usage_enabled": None,
        "cached_extra_usage_disabled_reason": None,
        "subscription_created_at": None,
    }
    assert _format_claude_account_block(meta) == []


def test_format_account_block_renders_all_fields():
    meta = {
        "email_address": "x@y.com",
        "organization_name": "Acme",
        "display_name": "Ada",
        "billing_type": "credit-card",
        "subscription_type": "pro",
        "rate_limit_tier": "tier-3",
        "has_available_subscription": True,
        "has_extra_usage_enabled": True,
        "cached_extra_usage_disabled_reason": None,
        "subscription_created_at": "2024-01-01",
    }
    lines = _format_claude_account_block(meta)
    joined = "\n".join(lines)
    assert "Claude Code account" in joined
    assert "x@y.com" in joined
    assert "Acme" in joined
    assert "pro" in joined
    assert "tier-3" in joined
    assert "yes" in joined
    assert "enabled" in joined
    assert "2024-01-01" in joined


def test_format_account_block_subscription_unknown_when_both_none():
    meta = {
        "email_address": "x@y.com",
        "subscription_type": None,
        "rate_limit_tier": None,
    }
    lines = _format_claude_account_block(meta)
    sub_line = next(line for line in lines if "Subscription" in line)
    assert sub_line.endswith("-")


def test_format_account_block_available_false_renders_no():
    meta = {"email_address": "x@y.com", "has_available_subscription": False}
    lines = _format_claude_account_block(meta)
    avail_line = next(line for line in lines if "Available" in line)
    assert avail_line.endswith("no")


def test_format_account_block_extra_disabled_with_reason():
    meta = {
        "email_address": "x@y.com",
        "has_extra_usage_enabled": False,
        "cached_extra_usage_disabled_reason": "limit-reached",
    }
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    assert "disabled" in extra_line
    assert "limit-reached" in extra_line


def test_format_account_block_extra_disabled_without_reason():
    meta = {"email_address": "x@y.com", "has_extra_usage_enabled": False}
    lines = _format_claude_account_block(meta)
    extra_line = next(line for line in lines if "Extra usage" in line)
    assert extra_line.rstrip().endswith("disabled")


def test_format_account_block_missing_keys_render_as_dash():
    # Only one non-None field; other lookups fall to ``-``.
    meta = {"email_address": "x@y.com"}
    lines = _format_claude_account_block(meta)
    org_line = next(line for line in lines if "Organization" in line)
    assert org_line.endswith("-")


# ---------------------------------------------------------------------------
# show-status CLI — fleet view (no NAME)
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entries=None, by_name=None):
        self._entries = entries or []
        self._by_name = by_name or {}

    def list_all(self):
        return list(self._entries)

    def get(self, name):
        return self._by_name.get(name)


def test_status_terse_without_name_returns_error(monkeypatch):
    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    runner = CliRunner()
    result = runner.invoke(status, ["--terse"])
    assert result.exit_code == 2
    payload = json.loads(result.output.strip())
    assert "error" in payload
    assert "--terse" in payload["error"]


def test_status_fleet_json(monkeypatch):
    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    import scitex_agent_container.cli_pkg._helpers as _h

    monkeypatch.setattr(
        _h,
        "get_agent_list_data",
        lambda registry, capability=None, machine=None: [{"name": "x"}],
    )
    runner = CliRunner()
    result = runner.invoke(status, ["--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["agents"] == [{"name": "x"}]


def test_status_fleet_table(monkeypatch):
    """Fleet view (no name, no --json) calls print_agent_list."""
    calls = {}

    def fake_print(registry, capability=None, machine=None):
        calls["called"] = True

    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(status_cmds, "print_agent_list", fake_print)
    runner = CliRunner()
    result = runner.invoke(status, [])
    assert result.exit_code == 0
    assert calls.get("called")


# ---------------------------------------------------------------------------
# show-status CLI — per-agent (NAME provided)
# ---------------------------------------------------------------------------


def test_status_per_agent_json(monkeypatch):
    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(
        status_cmds, "agent_status", lambda name: {"name": name, "status": "running"}
    )
    runner = CliRunner()
    result = runner.invoke(status, ["myagent", "--json"])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    assert info["name"] == "myagent"
    assert info["status"] == "running"


def test_status_per_agent_table(monkeypatch):
    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(
        status_cmds,
        "agent_status",
        lambda name: {"name": name, "status": "stopped", "host": "alpha"},
    )
    runner = CliRunner()
    result = runner.invoke(status, ["myagent"])
    assert result.exit_code == 0, result.output
    # Table rendering includes the field names + values.
    assert "myagent" in result.output
    assert "stopped" in result.output


def test_status_per_agent_error_handled(monkeypatch):
    def boom(name):
        raise RuntimeError("nope")

    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(status_cmds, "agent_status", boom)
    runner = CliRunner()
    result = runner.invoke(status, ["x", "--json"])
    assert result.exit_code == 1
    assert "nope" in result.output


def test_status_per_agent_error_human(monkeypatch):
    def boom(name):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(status_cmds, "agent_status", boom)
    runner = CliRunner()
    result = runner.invoke(status, ["x"])
    assert result.exit_code == 1
    # Plain stderr message (rich-rendered)
    assert "kaboom" in result.output or "Error" in result.output


def test_status_terse_projects_payload(monkeypatch):
    monkeypatch.setattr(status_cmds, "Registry", lambda: _FakeRegistry())
    monkeypatch.setattr(
        status_cmds,
        "agent_status",
        lambda name: {
            "agent": name,
            "state": "running",
            "_internal": "secret",
            "extra_drop_me": "x",
        },
    )
    runner = CliRunner()
    result = runner.invoke(status, ["myagent", "--terse"])
    assert result.exit_code == 0, result.output
    info = json.loads(result.output)
    # Terse keeps whitelisted fields and drops non-whitelisted ones.
    assert info.get("agent") == "myagent"
    assert "_internal" not in info
    assert "extra_drop_me" not in info


# ---------------------------------------------------------------------------
# check-health
# ---------------------------------------------------------------------------


def test_health_not_in_registry_human(monkeypatch):
    reg = _FakeRegistry(by_name={})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    runner = CliRunner()
    result = runner.invoke(health, ["ghost"])
    assert result.exit_code == 1
    assert "ghost" in result.output


def test_health_not_in_registry_json(monkeypatch):
    reg = _FakeRegistry(by_name={})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    runner = CliRunner()
    result = runner.invoke(health, ["ghost", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert "error" in payload


def test_health_load_config_failure_json(monkeypatch):
    reg = _FakeRegistry(by_name={"x": {"config": "/bad/path.yaml"}})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)

    def boom(_path):
        raise ValueError("bad yaml")

    monkeypatch.setattr(status_cmds, "load_config", boom)
    runner = CliRunner()
    result = runner.invoke(health, ["x", "--json"])
    assert result.exit_code == 1
    assert "bad yaml" in result.output


def test_health_healthy_json(monkeypatch):
    reg = _FakeRegistry(by_name={"x": {"config": "/p.yaml"}})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    monkeypatch.setattr(status_cmds, "load_config", lambda p: object())
    monkeypatch.setattr(status_cmds, "health_check", lambda c: (True, "all good"))
    runner = CliRunner()
    result = runner.invoke(health, ["x", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["healthy"] is True
    assert payload["message"] == "all good"


def test_health_unhealthy_json_exits_one(monkeypatch):
    reg = _FakeRegistry(by_name={"x": {"config": "/p.yaml"}})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    monkeypatch.setattr(status_cmds, "load_config", lambda p: object())
    monkeypatch.setattr(status_cmds, "health_check", lambda c: (False, "down"))
    runner = CliRunner()
    result = runner.invoke(health, ["x", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.output)
    assert payload["healthy"] is False
    assert payload["message"] == "down"


def test_health_healthy_human(monkeypatch):
    reg = _FakeRegistry(by_name={"x": {"config": "/p.yaml"}})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    monkeypatch.setattr(status_cmds, "load_config", lambda p: object())
    monkeypatch.setattr(status_cmds, "health_check", lambda c: (True, "✓ ok"))
    runner = CliRunner()
    result = runner.invoke(health, ["x"])
    assert result.exit_code == 0


def test_health_unhealthy_human_exits_one(monkeypatch):
    reg = _FakeRegistry(by_name={"x": {"config": "/p.yaml"}})
    monkeypatch.setattr(status_cmds, "Registry", lambda: reg)
    monkeypatch.setattr(status_cmds, "load_config", lambda p: object())
    monkeypatch.setattr(status_cmds, "health_check", lambda c: (False, "✗ bad"))
    runner = CliRunner()
    result = runner.invoke(health, ["x"])
    assert result.exit_code == 1
