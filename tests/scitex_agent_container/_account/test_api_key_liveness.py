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


def test_collect_slots_maps_pools_to_agents():
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
    assert slots["COMMANDCODE_API_KEY_02"]["agents"] == ["agent-b"]
    assert slots["OPENCODE_GO_API_KEY_01"]["provider"] == "opencode-go"


def test_collect_slots_includes_primary_engine_env():
    # Arrange — an engine's own auth_token_env with no pool entry.
    specs = [_spec("agent-a", {}, {"engine-x": "COMMANDCODE_API_KEY_07"})]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots["COMMANDCODE_API_KEY_07"]["engines"] == ["engine-x"]


def test_collect_slots_ignores_non_key_env():
    # Arrange — a pool naming something that is not a key slot.
    specs = [_spec("agent-a", {"engine-x": ["SOME_OTHER_VAR"]})]
    # Act
    slots = collect_slots(specs)
    # Assert
    assert slots == {}


def test_classify_dead_on_401():
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
    # Act
    verdict, _ = classify_status(
        "commandcode", 200, json.dumps({"balances": {"purchased": 12.5}})
    )
    # Assert
    assert verdict == "alive"


def test_classify_unknown_on_no_answer_and_5xx():
    # Act
    assert classify_status("commandcode", None, "")[0] == "unknown"
    assert classify_status("commandcode", 503, "")[0] == "unknown"


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


def test_sweep_reports_unresolved_without_network():
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
    assert calls == []


def test_sweep_probes_each_slot_once():
    # Arrange — one alive slot, one dead slot.
    def opener(request, timeout=None):
        url = request.full_url
        if "opencode" in url:
            return _FakeResponse(200, b'{"ok": true}')
        raise urllib.error.HTTPError(url, 401, "Unauthorized", Message(), None)

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
    # Act
    doc = sweep(
        slots,
        {"COMMANDCODE_API_KEY_01": "dead-token", "OPENCODE_GO_API_KEY_01": "live"},
        opener=opener,
        now=1000.0,
    )
    # Assert
    assert doc["slots"]["COMMANDCODE_API_KEY_01"]["verdict"] == "dead"
    assert doc["slots"]["OPENCODE_GO_API_KEY_01"]["verdict"] == "alive"
