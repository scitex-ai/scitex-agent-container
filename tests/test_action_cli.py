"""Tests for ``cli_pkg/action_cmds.py``.

Uses click's ``CliRunner`` to exercise ``query``, ``stats``, and
``purge`` directly without needing a live agent. ``run`` is not
covered here because it requires a running multiplexer session;
``test_action_base`` + ``test_nonce_probe_action`` already
exercise the engine side.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container import action_store
from scitex_agent_container.cli_pkg.action_cmds import actions_cli


@pytest.fixture
def root(tmp_path, monkeypatch):
    """Redirect the action store to an isolated tmp dir for the test."""
    monkeypatch.setattr(action_store, "DEFAULT_ROOT", tmp_path)
    return tmp_path


def _seed(root):
    records = [
        {
            "agent": "alpha",
            "action": "nonce-probe",
            "outcome": "success",
            "elapsed_s": 1.5,
            "ts": "2026-04-17T00:00:01+00:00",
        },
        {
            "agent": "alpha",
            "action": "nonce-probe",
            "outcome": "completion_timeout",
            "elapsed_s": 30.0,
            "ts": "2026-04-17T00:00:02+00:00",
        },
        {
            "agent": "beta",
            "action": "compact",
            "outcome": "success",
            "elapsed_s": 4.5,
            "ts": "2026-04-17T00:00:03+00:00",
        },
    ]
    for r in records:
        action_store.append_attempt(r, root=root)


class TestQueryCommand:
    def test_query_all(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["query"])
        assert result.exit_code == 0
        assert "nonce-probe" in result.output
        assert "compact" in result.output
        assert "alpha" in result.output
        assert "beta" in result.output

    def test_query_filter_by_agent(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["query", "--agent", "alpha"])
        assert result.exit_code == 0
        assert "alpha" in result.output
        assert "beta" not in result.output

    def test_query_filter_by_outcome(self, root):
        _seed(root)
        result = CliRunner().invoke(
            actions_cli, ["query", "--outcome", "completion_timeout"]
        )
        assert result.exit_code == 0
        assert "completion_timeout" in result.output
        # Only one matching row seeded -> success rows absent.
        assert "success" not in result.output

    def test_query_json_emits_list(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["query", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 3
        assert {r["agent"] for r in data} == {"alpha", "beta"}

    def test_query_empty_result_prints_friendly_message(self, root):
        result = CliRunner().invoke(actions_cli, ["query"])
        assert result.exit_code == 0
        assert "No matching attempts." in result.output


class TestStatsCommand:
    def test_stats_prints_group_rows(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["stats"])
        assert result.exit_code == 0
        # Header present and both actions represented.
        assert "action" in result.output
        assert "outcome" in result.output
        assert "nonce-probe" in result.output
        assert "compact" in result.output

    def test_stats_filter_by_agent(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["stats", "--agent", "alpha"])
        assert result.exit_code == 0
        assert "nonce-probe" in result.output
        assert "compact" not in result.output  # beta's action absent

    def test_stats_json_is_list_of_dicts(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["stats", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert isinstance(data, list)
        for row in data:
            assert {
                "action",
                "outcome",
                "count",
                "mean_elapsed_s",
                "p95_elapsed_s",
            }.issubset(row)


class TestPurgeCommand:
    def test_purge_removes_old_rows(self, root):
        from datetime import datetime, timedelta, timezone

        old_ts = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        action_store.append_attempt(
            {
                "agent": "ghost",
                "action": "nonce-probe",
                "outcome": "success",
                "elapsed_s": 1.0,
                "ts": old_ts,
            },
            root=root,
        )
        action_store.append_attempt(
            {
                "agent": "ghost",
                "action": "nonce-probe",
                "outcome": "success",
                "elapsed_s": 1.0,
            },
            root=root,
        )
        result = CliRunner().invoke(actions_cli, ["purge", "--days", "30", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data == {"deleted": 1}
        # The fresh row survives.
        rows = action_store.query(root=root)
        assert len(rows) == 1

    def test_purge_no_old_rows_reports_zero(self, root):
        _seed(root)
        result = CliRunner().invoke(actions_cli, ["purge", "--days", "30", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"deleted": 0}


class TestRunCommandErrors:
    """The happy path needs a live multiplexer, but the error paths
    (unknown agent / dead session) are easy to pin here."""

    def test_unknown_agent_exits_nonzero(self, root):
        result = CliRunner().invoke(
            actions_cli, ["run", "nonce-probe", "does-not-exist"]
        )
        assert result.exit_code == 2
        assert "not found" in result.output.lower()

    def test_rejects_unknown_action_name(self, root):
        """Click enforces the action_name choice set."""
        result = CliRunner().invoke(actions_cli, ["run", "bogus-action", "alpha"])
        assert result.exit_code != 0
        assert (
            "Invalid value" in result.output
            or "invalid choice" in result.output.lower()
        )
