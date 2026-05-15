"""Tests for ``_handover`` — sac runtime side of ZOO#12.

Covers:
  - spec-key readers tolerate flat + ``spec:``-wrapped YAML
  - ``ensure_instance_uuid`` writes ``SAC_INSTANCE_UUID`` once
  - ``_should_step_aside`` priority+healthy logic
  - ``hydrate_from_hub`` writes the snapshot file atomically
  - ``push_pre_stop_snapshot`` calls hub_client.push_snapshot

The hub HTTP layer is replaced at the ``hub_client`` attribute boundary
via a save/restore swap so no real network is touched. This is not a
mock library: the substitute is a hand-rolled callable and the original
binding is restored on teardown.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle import handover as _handover


def _write_spec(path: Path, body: dict) -> Path:
    import yaml

    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


# ---------- spec readers ----------------------------------------------------


@pytest.mark.parametrize(
    "spec_body",
    [
        {"cardinality_enforced_at_hub": True},
        {"spec": {"cardinality_enforced_at_hub": True}},
    ],
    ids=["flat_yaml", "nested_under_spec"],
)
def test_read_cardinality_enforced_true_under_either_layout(tmp_path, spec_body):
    # Arrange
    spec_path = _write_spec(tmp_path / "spec.yaml", spec_body)
    # Act
    result = _handover.read_cardinality_enforced(str(spec_path))
    # Assert
    assert result is True


def test_read_priority_list_filters_blanks_and_none(tmp_path):
    # Arrange
    spec_path = _write_spec(
        tmp_path / "c.yaml",
        {"priority_list": ["spartan", "", None, "mba"]},
    )
    # Act
    result = _handover.read_priority_list(str(spec_path))
    # Assert
    assert result == ["spartan", "mba"]


def test_read_priority_list_missing_returns_empty(tmp_path):
    # Arrange
    spec_path = _write_spec(tmp_path / "d.yaml", {})
    # Act
    result = _handover.read_priority_list(str(spec_path))
    # Assert
    assert result == []


# ---------- ensure_instance_uuid -------------------------------------------


def test_ensure_instance_uuid_writes_36char_uuid(tmp_path):
    # Arrange
    cfg = SimpleNamespace(env={}, expanded_workdir=str(tmp_path))
    # Act
    out = _handover.ensure_instance_uuid(cfg)
    # Assert: uuid4 string format — 36 chars including 4 hyphens.
    assert len(out) == 36


def test_ensure_instance_uuid_propagates_into_env(tmp_path):
    # Arrange
    cfg = SimpleNamespace(env={}, expanded_workdir=str(tmp_path))
    # Act
    out = _handover.ensure_instance_uuid(cfg)
    # Assert
    assert cfg.env["SAC_INSTANCE_UUID"] == out


def test_ensure_instance_uuid_idempotent_preserves_preset(tmp_path):
    # Arrange
    cfg = SimpleNamespace(
        env={"SAC_INSTANCE_UUID": "preset-id"}, expanded_workdir=str(tmp_path)
    )
    # Act
    out = _handover.ensure_instance_uuid(cfg)
    # Assert
    assert (out, cfg.env["SAC_INSTANCE_UUID"]) == ("preset-id", "preset-id")


# ---------- _should_step_aside ---------------------------------------------


def test_should_step_aside_higher_priority_healthy_yields_true():
    # Arrange: we're "mba"; spartan is higher and healthy.
    payload = {
        "priority_list": ["spartan", "mba", "ywata-note-win"],
        "healthy": {"spartan": True, "mba": False},
    }
    # Act
    result = _handover._should_step_aside("mba", payload)
    # Assert
    assert result is True


def test_should_step_aside_higher_priority_unhealthy_yields_false():
    # Arrange: spartan is higher but unhealthy → keep running on mba.
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": False, "mba": True},
    }
    # Act
    result = _handover._should_step_aside("mba", payload)
    # Assert
    assert result is False


def test_should_step_aside_self_top_of_priority_list():
    # Arrange: spartan is us and the top of priority_list.
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": True, "mba": True},
    }
    # Act
    result = _handover._should_step_aside("spartan", payload)
    # Assert: never step aside if we are the top.
    assert result is False


def test_should_step_aside_self_not_in_priority_list():
    # Arrange
    payload = {
        "priority_list": ["spartan", "mba"],
        "healthy": {"spartan": True},
    }
    # Act
    result = _handover._should_step_aside("strange-host", payload)
    # Assert
    assert result is False


# ---------- hub_client swap helper -----------------------------------------


def _swap_hub(name: str, fn):
    """Swap ``_handover.hub_client.<name>`` for ``fn``; return restore fn."""
    saved = getattr(_handover.hub_client, name)
    setattr(_handover.hub_client, name, fn)

    def _restore():
        setattr(_handover.hub_client, name, saved)

    return _restore


# ---------- hydrate_from_hub -----------------------------------------------


def test_hydrate_from_hub_writes_snapshot_returns_true(tmp_path):
    # Arrange
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))

    def fake_fetch(name):
        return {
            "agent_name": name,
            "owner_host": "spartan",
            "payload": {"memory": "alpha"},
            "updated_at": "2026-04-29T00:00:00Z",
        }

    restore = _swap_hub("fetch_snapshot", fake_fetch)
    # Act
    try:
        result = _handover.hydrate_from_hub(cfg)
    finally:
        restore()
    # Assert
    assert result is True


def test_hydrate_from_hub_persists_payload_to_snapshot_file(tmp_path):
    # Arrange
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))

    def fake_fetch(name):
        return {
            "agent_name": name,
            "owner_host": "spartan",
            "payload": {"memory": "alpha"},
            "updated_at": "2026-04-29T00:00:00Z",
        }

    restore = _swap_hub("fetch_snapshot", fake_fetch)
    # Act
    try:
        _handover.hydrate_from_hub(cfg)
    finally:
        restore()
    written = (tmp_path / ".scitex" / "handover" / "snapshot.json").read_text()
    # Assert
    assert json.loads(written)["payload"] == {"memory": "alpha"}


def test_hydrate_from_hub_missing_snapshot_returns_false(tmp_path):
    # Arrange
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))
    restore = _swap_hub("fetch_snapshot", lambda n: None)
    # Act
    try:
        result = _handover.hydrate_from_hub(cfg)
    finally:
        restore()
    # Assert
    assert result is False


def test_hydrate_from_hub_missing_snapshot_does_not_write_file(tmp_path):
    # Arrange
    cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))
    restore = _swap_hub("fetch_snapshot", lambda n: None)
    # Act
    try:
        _handover.hydrate_from_hub(cfg)
    finally:
        restore()
    # Assert
    assert not (tmp_path / ".scitex" / "handover" / "snapshot.json").exists()


# ---------- push_pre_stop_snapshot -----------------------------------------


def _capture_push_call(tmp_path, host_value):
    """Drive ``push_pre_stop_snapshot`` against a recording fake and return
    the captured argv dict plus the call's return value. Save/restore env
    and the hub_client attribute so the call has no global side effects.
    """
    import os

    seen: dict = {}

    def fake_push(name, payload, owner_host=""):
        seen["name"] = name
        seen["payload"] = payload
        seen["owner_host"] = owner_host
        return True

    restore = _swap_hub("push_snapshot", fake_push)
    saved_host = os.environ.get("SCITEX_AGENT_CONTAINER_HOSTNAME")
    os.environ["SCITEX_AGENT_CONTAINER_HOSTNAME"] = host_value
    try:
        cfg = SimpleNamespace(name="lead", expanded_workdir=str(tmp_path))
        rc = _handover.push_pre_stop_snapshot(cfg)
    finally:
        restore()
        if saved_host is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_HOSTNAME", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_HOSTNAME"] = saved_host
    return seen, rc


def test_push_pre_stop_snapshot_returns_true_when_hub_accepts(tmp_path):
    # Arrange
    host = "spartan"
    # Act
    _, rc = _capture_push_call(tmp_path, host)
    # Assert
    assert rc is True


def test_push_pre_stop_snapshot_forwards_agent_name(tmp_path):
    # Arrange
    host = "spartan"
    # Act
    seen, _ = _capture_push_call(tmp_path, host)
    # Assert
    assert seen["name"] == "lead"


def test_push_pre_stop_snapshot_forwards_owner_host_from_env(tmp_path):
    # Arrange
    host = "spartan"
    # Act
    seen, _ = _capture_push_call(tmp_path, host)
    # Assert
    assert seen["owner_host"] == "spartan"


def test_push_pre_stop_snapshot_default_payload_reason_is_pre_stop(tmp_path):
    # Arrange
    host = "spartan"
    # Act
    seen, _ = _capture_push_call(tmp_path, host)
    # Assert
    assert seen["payload"]["reason"] == "pre_stop"


# ---------- start_failback_poller ------------------------------------------


def test_start_failback_poller_no_priority_list_returns_none(tmp_path):
    # Arrange
    spec_path = _write_spec(tmp_path / "e.yaml", {})
    cfg = SimpleNamespace(
        name="freerunner", expanded_workdir=str(tmp_path), config_path=str(spec_path)
    )
    # Act
    result = _handover.start_failback_poller(cfg)
    # Assert
    assert result is None
