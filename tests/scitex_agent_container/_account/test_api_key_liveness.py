"""Tests for the TUI-independent API-key liveness sweep."""

from __future__ import annotations

import json
import urllib.error
from email.message import Message

from scitex_agent_container._account.api_key_liveness import (
    classify_status,
    collect_slots,
    sweep,
)


def _spec(agent, pools, primaries=None):
    engines = {
        key: {"provider": {"auth_token_env": env}}
        for key, env in (primaries or {}).items()
    }
    return {
        "agent_name": agent,
        "available_harnesses": {"hermes": {"failover": {"accounts": pools}}},
        "available_engines": engines,
    }


def test_collect_slots_maps_shared_pool_to_both_agents():
    # Arrange — two agents sharing one slot, one private slot.
    specs = [
        _spec("agent-a", {"engine-x": ["OPENCODE_GO_API_KEY_01"]}),
        _spec(
            "agent-b",
            {"engine-x": ["OPENCODE_GO_API_KEY_01"], "engine-y": ["COMMANDCODE_API_KEY_02"]},
        ),
    ]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots["OPENCODE_GO_API_KEY_01"]["agents"] == ["agent-a", "agent-b"]


def test_collect_slots_maps_private_pool_to_single_agent():
    # Arrange — the same two specs; the private slot belongs to agent-b only.
    specs = [
        _spec("agent-a", {"engine-x": ["OPENCODE_GO_API_KEY_01"]}),
        _spec(
            "agent-b",
            {"engine-x": ["OPENCODE_GO_API_KEY_01"], "engine-y": ["COMMANDCODE_API_KEY_02"]},
        ),
    ]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots["COMMANDCODE_API_KEY_02"]["agents"] == ["agent-b"]


def test_collect_slots_records_opencode_go_provider_name():
    # Arrange — the same two specs; the provider derives from the key prefix.
    specs = [
        _spec("agent-a", {"engine-x": ["OPENCODE_GO_API_KEY_01"]}),
        _spec(
            "agent-b",
            {"engine-x": ["OPENCODE_GO_API_KEY_01"], "engine-y": ["COMMANDCODE_API_KEY_02"]},
        ),
    ]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots["OPENCODE_GO_API_KEY_01"]["provider"] == "opencode-go"


def test_collect_slots_includes_primary_engine_env():
    # Arrange — an engine's own auth_token_env with no pool entry.
    specs = [_spec("agent-a", {}, {"engine-x": "COMMANDCODE_API_KEY_07"})]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots["COMMANDCODE_API_KEY_07"]["engines"] == ["engine-x"]


def test_collect_slots_reads_the_actual_v3_document_envelope():
    # Arrange: the CLI reads whole spec.yaml files, not flattened test records.
    inner = _spec("agent-a", {"muse": ["OPENCODE_GO_API_KEY_03"]})
    document = {
        "apiVersion": "scitex-agent-container/v3", "kind": "Agent",
        "metadata": {"name": "agent-a"},
        "spec": {key: value for key, value in inner.items() if key != "agent_name"},
    }
    document["spec"]["available_engines"]["unsupported-selector"] = "some-provider"
    # Act
    slots = collect_slots([document])
    # Assert
    assert slots["OPENCODE_GO_API_KEY_03"]["agents"] == ["agent-a"]


def test_collect_slots_ignores_non_key_env():
    # Arrange — a pool naming something that is not a key slot.
    specs = [_spec("agent-a", {"engine-x": ["SOME_OTHER_VAR"]})]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots == {}


def test_classify_dead_on_401():
    # Arrange — no setup; the classifier is a pure function.
    # Act
    verdict, _ = classify_status("commandcode", 401, "")
    # Assert
    assert verdict == "dead"


def test_classify_exhausted_on_insufficient_credits_400():
    # Arrange — the exact 2026-10-15 killer: valid key, empty balance.
    body = "You have insufficient credits to make this request."
    # Act
    verdict, _ = classify_status("commandcode", 400, body)
    # Assert
    assert verdict == "exhausted"


def test_classify_exhausted_on_zero_balance_200():
    # Arrange — a 200 credits payload with nothing left.
    body = json.dumps({"balances": {"purchased": 0, "free": 0}})
    # Act
    verdict, _ = classify_status("commandcode", 200, body)
    # Assert
    assert verdict == "exhausted"


def test_classify_alive_on_200_with_balance():
    # Arrange — a 200 credits payload with headroom left.
    # Act
    verdict, _ = classify_status(
        "commandcode", 200, json.dumps({"balances": {"purchased": 12.5}})
    )
    # Assert
    assert verdict == "alive"


def test_classify_unknown_when_probe_gives_no_answer():
    # Arrange — the probe never answered (connection failure, not a verdict).
    # Act
    verdict, _ = classify_status("commandcode", None, "")
    # Assert
    assert verdict == "unknown"


def test_classify_unknown_on_server_error_status():
    # Arrange — the provider answered 503: its problem, not the key's.
    # Act
    verdict, _ = classify_status("commandcode", 503, "")
    # Assert
    assert verdict == "unknown"


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, _n):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_sweep_marks_keyless_slot_unresolved():
    # Arrange — a slot with no secret must not touch the network.
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        raise AssertionError("must not be called")

    slots = {
        "OPENCODE_GO_API_KEY_01": {
            "provider": "opencode-go",
            "agents": ["agent-a"],
            "engines": ["engine-x"],
        }
    }
    # Act
    doc = sweep(slots, {}, opener=opener, now=1000.0)
    # Assert
    assert doc["slots"]["OPENCODE_GO_API_KEY_01"]["verdict"] == "unresolved"


def test_sweep_sends_no_requests_for_keyless_slots():
    # Arrange — the same keyless slot; the opener records any call.
    calls = []

    def opener(request, timeout=None):
        calls.append(request)
        raise AssertionError("must not be called")

    slots = {
        "OPENCODE_GO_API_KEY_01": {
            "provider": "opencode-go",
            "agents": ["agent-a"],
            "engines": ["engine-x"],
        }
    }
    # Act
    sweep(slots, {}, opener=opener, now=1000.0)
    # Assert
    assert calls == []


def _probe_opener(request, timeout=None):
    """Fake transport: opencode probes answer 200, everything else 401s."""
    url = request.full_url
    live = {"opencode": _FakeResponse(200, b'{"ok": true}')}
    try:
        provider = next(key for key in live if key in url)
    except StopIteration:
        raise urllib.error.HTTPError(url, 401, "Unauthorized", Message(), None) from None
    return live[provider]


def _two_provider_slots():
    """One dead commandcode slot and one live opencode slot, plus secrets."""
    slots = {
        "COMMANDCODE_API_KEY_01": {
            "provider": "commandcode",
            "agents": ["agent-a"],
            "engines": ["e1"],
        },
        "OPENCODE_GO_API_KEY_01": {
            "provider": "opencode-go",
            "agents": ["agent-a"],
            "engines": ["e2"],
        },
    }
    secrets = {"COMMANDCODE_API_KEY_01": "dead-token", "OPENCODE_GO_API_KEY_01": "live"}
    return slots, secrets


def test_sweep_marks_401_slot_dead():
    # Arrange — one alive slot, one dead slot.
    slots, secrets = _two_provider_slots()
    # Act
    doc = sweep(slots, secrets, opener=_probe_opener, now=1000.0)
    # Assert
    assert doc["slots"]["COMMANDCODE_API_KEY_01"]["verdict"] == "dead"


def test_sweep_marks_200_slot_alive():
    # Arrange — the same two slots; the opencode probe answers 200.
    slots, secrets = _two_provider_slots()
    # Act
    doc = sweep(slots, secrets, opener=_probe_opener, now=1000.0)
    # Assert
    assert doc["slots"]["OPENCODE_GO_API_KEY_01"]["verdict"] == "alive"
