"""Tests for ``_handover`` — sac runtime side of ZOO#12.

Covers:
  - spec-key readers tolerate flat + ``spec:``-wrapped YAML
  - ``ensure_instance_uuid`` writes ``SCITEX_AGENT_INSTANCE_UUID`` once
  - ``_should_step_aside`` priority+healthy logic
  - ``hydrate_from_hub`` writes the snapshot file atomically
  - ``push_pre_stop_snapshot`` calls hub_client.push_snapshot

The hub HTTP layer is monkeypatched at the ``hub_client`` boundary so
no real network is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from scitex_agent_container import _handover


def _write_spec(path: Path, body: dict) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def test_read_cardinality_enforced_flat_yaml(tmp_path):
    p = _write_spec(tmp_path / "a.yaml", {"cardinality_enforced_at_hub": True})
    assert _handover.read_cardinality_enforced(str(p)) is True


def test_read_cardinality_enforced_nested_under_spec(tmp_path):
    p = _write_spec(
        tmp_path / "b.yaml", {"spec": {"cardinality_enforced_at_hub": True}}
    )
    assert _handover.read_cardinality_enforced(str(p)) is True


def test_read_priority_list_filters_blanks(tmp_path):
    p = _write_spec(
        tmp_path / "c.yaml",
        {"priority_list": ["spartan", "", None, "mba"]},
    )
    assert _handover.read_priority_list(str(p)) == ["spartan", "mba"]


def test_read_priority_list_missing_returns_empty(tmp_path):
    p = _write_spec(tmp_path / "d.yaml", {})
    assert _handover.read_priority_list(str(p)) == []


def test_ensure_instance_uuid_writes_env(tmp_path):
    cfg = SimpleNamespace(env={}, expanded_workdir=str(tmp_path))
    out = _handover.ensure_instance_uuid(cfg)
    # uuid4 string format: 36 chars including 4 hyphens.
    assert len(out) == 36
    assert cfg.env["SCITEX_AGENT_INSTANCE_UUID"] == out


def test_ensure_instance_uuid_idempotent(tmp_path):
    cfg = SimpleNamespace(
        env={"SCITEX_AGENT_INSTANCE_UUID": "preset-id"}, expanded_workdir=str(tmp_path)
    )
    out = _handover.ensure_instance_uuid(cfg)
    assert out == "preset-id"
    assert cfg.env["SCITEX_AGENT_INSTANCE_UUID"] == "preset-id"


def test_should_step_aside_higher_priority_healthy():
    payload = {
        "priority_list": ["spartan", "mba", "ywata-note-win"],
        "healthy": {"spartan": True, "mba": False},
    }
    # We're "mba" — spartan is higher and healthy → step aside.
    assert _handover._should_step_aside("mba", payload) is True


def test_should_step_aside_higher_priority_unhealthy():
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": False, "mba": True},
    }
    # spartan is higher but unhealthy → keep running on mba.
    assert _handover._should_step_aside("mba", payload) is False


def test_should_step_aside_self_top_of_list():
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": True, "mba": True},
    }
    # spartan is us and the top of priority_list — never step aside.
    assert _handover._should_step_aside("spartan", payload) is False


def test_should_step_aside_self_not_in_list():
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": True},
    }
    assert _handover._should_step_aside("strange-host", payload) is False


def test_hydrate_from_hub_writes_snapshot(tmp_path, monkeypatch):
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))

    def fake_fetch(name):
        return {
            "agent_name": name,
            "owner_host": "spartan",
            "payload": {"memory": "alpha"},
            "updated_at": "2026-04-29T00:00:00Z",
        }

    monkeypatch.setattr(_handover.hub_client, "fetch_snapshot", fake_fetch)
    assert _handover.hydrate_from_hub(cfg) is True
    written = (tmp_path / ".scitex" / "handover" / "snapshot.json").read_text()
    assert json.loads(written)["payload"] == {"memory": "alpha"}


def test_hydrate_from_hub_404_returns_false(tmp_path, monkeypatch):
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))
    monkeypatch.setattr(_handover.hub_client, "fetch_snapshot", lambda n: None)
    assert _handover.hydrate_from_hub(cfg) is False
    # No file should be written on miss.
    assert not (tmp_path / ".scitex" / "handover" / "snapshot.json").exists()


def test_push_pre_stop_snapshot_calls_hub_client(tmp_path, monkeypatch):
    seen = {}

    def fake_push(name, payload, owner_host=""):
        seen["name"] = name
        seen["payload"] = payload
        seen["owner_host"] = owner_host
        return True

    monkeypatch.setattr(_handover.hub_client, "push_snapshot", fake_push)
    monkeypatch.setenv("SCITEX_OROCHI_MACHINE", "spartan")
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))
    assert _handover.push_pre_stop_snapshot(cfg) is True
    assert seen["name"] == "lead"
    assert seen["owner_host"] == "spartan"
    assert seen["payload"]["reason"] == "pre_stop"


def test_start_failback_poller_no_priority_list_is_noop(tmp_path):
    p = _write_spec(tmp_path / "e.yaml", {})
    cfg = SimpleNamespace(
        name="freerunner", expanded_workdir=str(tmp_path), config_path=str(p)
    )
    assert _handover.start_failback_poller(cfg) is None
