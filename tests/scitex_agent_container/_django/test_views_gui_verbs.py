"""Tests for the Agents GUI create/forget/delete verbs (leaf side).

End to end over real HTTP against the ``loopback`` listener where a gate
needs the fleet read; no mocks, no ``monkeypatch`` (STX-NM002 / PA-306 §3).
Each test has AAA markers and a single assertion (STX-TQ002/TQ007).

Hermeticity rule for this module: the POST gates are tested (403s fail
before any local mutation), the create validation chain is tested (400s
fail before any write), and the shared scaffold core is tested against a
``tmp_path`` base dir. Forget/delete SUCCESS paths mutate the live
leaf store by design (same backend as the CLI) and are covered by the
CLI suites + the c03 deploy check — this module never touches them.
"""

from __future__ import annotations

import json

from scitex_agent_container._django._constants import (
    CROSSHOST_OPERATORS_ENV,
    IDENTITY_ENV,
    OPERATORS_ENV,
)

OPS_ENV = OPERATORS_ENV
CROSS_ENV = CROSSHOST_OPERATORS_ENV


def _seeded_fleet_html(client, identity) -> str:
    from scitex_agent_container._django._inventory_cache import CACHE

    CACHE.put(identity, [{"name": "alpha", "state_label": "Alive", "state_tone": "good",
                         "runtime": "apptainer", "harness": "anthropic", "role": "worker",
                         "engine": "anthropic", "model": "sonnet", "project": "p",
                         "host": "this node", "scope": "own", "cross_host": False,
                         "a2a_port": 19000, "pid": 1, "activity": {}}])
    try:
        return client.get("/").content.decode()
    finally:
        CACHE.clear()


# ── fleet surface: the new verbs render for operators ──────────────────────
def test_fleet_offers_forget_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = _seeded_fleet_html(client, "op1")
    # Assert
    assert "/alpha/forget" in html and ">Forget</button>" in html


def test_fleet_does_not_offer_a_force_override(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = _seeded_fleet_html(client, "op1")
    # Assert
    assert 'name="force"' not in html


def test_forget_rejects_legacy_force_before_store_mutation(
    client, loopback, env_save_restore, audit_log
):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    response = client.post("/alpha/forget", {"force": "on"})
    # Assert
    assert (
        response.status_code == 400
        and "force is unsupported" in response.json()["error"]
    )


def test_fleet_offers_delete_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = _seeded_fleet_html(client, "op1")
    # Assert
    assert "/alpha/delete" in html and ">Delete</button>" in html


def test_fleet_offers_create_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = _seeded_fleet_html(client, "op1")
    # Assert
    assert "/create/" in html and "Create agent" in html


def test_fleet_hides_create_forget_delete_from_non_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = _seeded_fleet_html(client, "alice")
    # Assert: none of the three verbs leak to a read-only identity.
    assert "/create/" not in html and "/forget" not in html and "/delete" not in html


# ── POST gates: deny before any local mutation ─────────────────────────────
def test_forget_denied_for_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    resp = client.post("/alpha/forget", {"force": "on"})
    # Assert
    assert resp.status_code == 403


def test_delete_denied_for_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    resp = client.post("/alpha/delete")
    # Assert
    assert resp.status_code == 403


def test_create_denied_for_non_operator(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    resp = client.post("/create/", {"name": "nope", "template": "minimal"})
    # Assert: denied before the scaffold backend is reached (no disk write).
    assert resp.status_code == 403


def test_create_denied_for_crosshost_only_operator(client, loopback, env_save_restore, audit_log):
    # Arrange — create lands on THIS node, so the cross-host list alone grants nothing.
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(CROSS_ENV, "op1")
    # Act
    resp = client.post("/create/", {"name": "nope", "template": "minimal"})
    # Assert
    assert resp.status_code == 403


# ── create validation: loud before any write ───────────────────────────────
def test_create_rejects_invalid_name(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    resp = client.post("/create/", {"name": "Bad Name!", "template": "minimal"})
    # Assert
    assert resp.status_code == 400 and "Invalid agent name" in json.loads(resp.content)["error"]


def test_create_rejects_unknown_template(client, loopback, env_save_restore, audit_log):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    resp = client.post("/create/", {"name": "gui-probe-agent", "template": "no-such-template"})
    # Assert: the CLI's own UsageError surfaces loud, nothing written.
    assert resp.status_code == 400 and "Unknown template" in json.loads(resp.content)["error"]


# ── create form ────────────────────────────────────────────────────────────
def test_create_form_shown_to_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "op1")
    env_save_restore.set(OPS_ENV, "op1")
    # Act
    html = client.get("/create/").content.decode()
    # Assert
    assert 'name="name"' in html and 'name="template"' in html and "Create agent" in html


def test_create_form_readonly_for_non_operator(client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = client.get("/create/").content.decode()
    # Assert
    assert "Read-only" in html and 'name="name"' not in html


def test_mounted_create_uses_hub_shell(hub_client, loopback, env_save_restore):
    # Arrange
    env_save_restore.set(IDENTITY_ENV, "alice")
    # Act
    html = hub_client.get("/apps/agents/create/").content.decode()
    # Assert
    assert 'id="hub-global-header"' in html and "workspace-three-col" not in html and "agents-app" in html and "agents.css" in html


# ── shared backend: the GUI scaffolds through the CLI's own core ───────────
def test_gui_create_uses_cli_scaffold_core(tmp_path):
    # Arrange
    from scitex_agent_container.cli_pkg._create import scaffold_agent

    base = tmp_path / "agents"
    # Act: the exact function the GUI view calls, against an isolated root.
    spec_path = scaffold_agent("gui-probe-agent", template_name="minimal", base_dir=base)
    # Assert
    assert spec_path.is_file() and (base / "gui-probe-agent" / "to_home").is_dir()
