"""Official quota evidence may cause one fresh restart, never an unknown/stale loop."""

from datetime import datetime, timezone
import json

import pytest

from scitex_agent_container._account.api_key_liveness import classify_status, capped_windows
from scitex_agent_container._account.quota_restart import restart_capped_agents


@pytest.mark.parametrize("hours", [5, 24, 168, 720, 744])
def test_official_quota_window_cap_retains_reset_and_does_not_outlive_it(hours):
    # Arrange: Go's official usage payload, with a provider-owned reset.
    now = 1_700_000_000.0
    reset = datetime.fromtimestamp(now + hours * 3600, timezone.utc).isoformat()
    body = json.dumps({"usage": {"rolling": {"percent": 100, "resetsAt": reset}}})
    # Act
    verdict = classify_status("opencode-go", 200, body, now=now)
    windows = capped_windows("opencode-go", body, now=now)
    expired = capped_windows("opencode-go", body, now=now + hours * 3600 + 1)
    # Assert
    assert (verdict[0], windows[0]["resetAt"], expired) == ("exhausted", reset, [])


def test_quota_event_causes_one_fresh_restart_only_for_running_consumers(tmp_path):
    # Arrange
    now = 1_700_000_000.0
    document = {"checked_at": now, "slots": {"KEY": {
        "verdict": "exhausted", "reason": "http-200-quota-weekly",
        "agents": ["running", "stopped"],
    }}}
    calls = []

    def restart(name, **options):
        calls.append((name, options))
        return True

    path = tmp_path / "quota-restarts.json"
    # Act
    first = restart_capped_agents(document, state_path=path, running_names=["running"], restart=restart, now=now)
    second = restart_capped_agents(document, state_path=path, running_names=["running"], restart=restart, now=now + 1)
    # Assert
    assert (first[0]["status"], second[0]["status"], calls) == (
        "restarted-fresh", "already-restarted",
        [("running", {"session_override": "fresh", "drain_timeout_s": 30})],
    )


@pytest.mark.parametrize("verdict,age", [("unknown", 0), ("alive", 0), ("exhausted", 301)])
def test_unknown_healthy_or_stale_evidence_never_restarts(tmp_path, verdict, age):
    # Arrange
    document = {"checked_at": 1000, "slots": {"KEY": {"verdict": verdict, "agents": ["running"]}}}
    calls = []
    # Act
    restart_capped_agents(document, state_path=tmp_path / "state.json", running_names=["running"],
                          restart=lambda *a, **k: calls.append((a, k)), now=1000 + age)
    # Assert
    assert calls == []
