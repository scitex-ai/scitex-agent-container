"""Forks preserve context without sharing identity, bot ownership or leases."""
import sqlite3
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._lifecycle._fork import copy_hermes_context, derive_fork_spec
from scitex_agent_container.config import load_config


def test_lead_fork_has_independent_identity_and_valid_spec(tmp_path):
    path = Path(__file__).resolve().parents[3] / "examples/lead-agents/scitex-hub/spec.yaml"
    original = yaml.safe_load(path.read_text())
    child = derive_fork_spec(original, "scitex-hub", "app-worker", "review tests", tmp_path / "overlay")
    dest = tmp_path / "app-worker/spec.yaml"
    dest.parent.mkdir()
    dest.write_text(yaml.safe_dump(child))
    config = load_config(str(dest))
    assert config.env["SCITEX_CARDS_AGENT_ID"] == "app-worker"
    assert config.env["CCT_BOT_TOKEN"] == ""
    assert "server:claude-code-telegrammer" not in config.claude.channels
    assert not config.lineage.may_spawn
    assert config.a2a.port == "auto"
    assert "domain_lead" not in config.extensions
    assert original["spec"]["apptainer"]["env"]["SCITEX_CARDS_AGENT_ID"] == "scitex-hub"


def _database(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, content TEXT);
        CREATE TABLE session_turn_leases(session_id TEXT);
        INSERT INTO sessions VALUES ('session-1', 'sac:lead:gpt-6-astra');
        INSERT INTO messages VALUES (1, 'session-1', 'inherited context');
        INSERT INTO session_turn_leases VALUES ('session-1');
    """)
    conn.commit()
    return conn


def test_wal_snapshot_is_independent_and_drops_runtime_leases(tmp_path):
    source, dest = tmp_path / "parent.db", tmp_path / "child.db"
    parent = _database(source)
    try:
        assert copy_hermes_context(source, dest, "lead", "child") == 1
        parent.execute("INSERT INTO messages VALUES (2,'session-1','later parent turn')")
        parent.commit()
        with sqlite3.connect(dest) as child:
            assert child.execute("SELECT content FROM messages").fetchall() == [("inherited context",)]
            assert child.execute("SELECT title FROM sessions").fetchone()[0] == "sac:child:gpt-6-astra"
            assert child.execute("SELECT count(*) FROM session_turn_leases").fetchone()[0] == 0
        assert parent.execute("SELECT title FROM sessions").fetchone()[0] == "sac:lead:gpt-6-astra"
        assert parent.execute("SELECT count(*) FROM session_turn_leases").fetchone()[0] == 1
        with pytest.raises(ValueError, match="already exists"):
            copy_hermes_context(source, dest, "lead", "child")
    finally:
        parent.close()


def test_unknown_parent_conversation_does_not_publish_empty_fork(tmp_path):
    source, dest = tmp_path / "parent.db", tmp_path / "child.db"
    parent = _database(source)
    parent.close()
    with pytest.raises(ValueError, match="no named SAC"):
        copy_hermes_context(source, dest, "other", "child")
    assert not dest.exists()


def test_pending_launch_is_reported_without_failure_or_retry(monkeypatch, tmp_path):
    from click.testing import CliRunner
    from scitex_agent_container.cli_pkg.lifecycle._fork import fork
    from scitex_agent_container.cli_pkg.lifecycle import _attach
    from scitex_agent_container._lifecycle import _fork, _in_sif_broker, _spawn_client
    from scitex_agent_container._listen import _acl, _config
    from scitex_agent_container._state import state_store_nodes
    import json

    monkeypatch.setattr(_in_sif_broker, 'is_in_sif', lambda: False)
    monkeypatch.setattr(_attach, '_classify_agent_host', lambda name: ('local', 'host'))
    monkeypatch.setattr(_acl, 'check_lineage_acl', lambda **kw: ('allow', None))
    monkeypatch.setattr(_acl, 'check_spawn', lambda **kw: ('allow', None))
    monkeypatch.setattr(_fork, 'prepare_fork', lambda *a, **kw: tmp_path / 'spec.yaml')
    monkeypatch.setattr(state_store_nodes, 'record_lineage', lambda **kw: None)
    monkeypatch.setattr(_config, 'listen_base_url', lambda: 'http://localhost:7878')
    launches = []
    def launch(*a, **kw):
        launches.append(a)
        return {'status': 'accepted', 'phase': 'launch', 'poll': '/agents/child/status'}
    monkeypatch.setattr(_spawn_client, 'request_spawn', launch)
    result = CliRunner().invoke(fork, ['lead', '--name', 'child', '--task', 'inspect', '--json'])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output['pending'] is True and output['started'] is False
    assert len(launches) == 1
