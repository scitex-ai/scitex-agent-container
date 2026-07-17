"""Tests for the 7d spend-policy layer (operator ruling 2026-07-17).

PA-306 no-mocks: every case drives the real functions with injected
quota maps / an injected ``now`` (the documented seams) — no real quota
cache, no network, and NEVER a token. Covers:

* policy resolution (default spread; burn opt-in; fail-loud on unknown);
* the POLICY_BURN ordering — highest 7d usage first (spend the
  perishable weekly bucket), soonest-reset tie-break, 5h-blocked tier
  still supreme, deterministic (spread_key ignored);
* the preferred-pin inversion — under burn a near-capped pin is a
  reason to STAY, not to rotate off;
* the auditable ranking-input record (the fix for "the notice named
  criteria but not inputs, so a reasoned pick was indistinguishable
  from a lucky one").
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds._pick_audit import (
    audit_candidates,
    format_pick_audit,
)
from scitex_agent_container._creds._pick_healthy import pick_healthy_account
from scitex_agent_container._creds._quota_rank import pick_ranked
from scitex_agent_container._creds._spend_policy import (
    POLICY_BURN,
    POLICY_SPREAD,
    resolve_7d_policy,
    validate_7d_policy,
)

_NOW = 1_752_700_000.0  # fixed instant for reset arithmetic


# ---------------------------------------------------------------------------
# resolve_7d_policy — default, opt-in, fail-loud
# ---------------------------------------------------------------------------


def test_resolve_policy_empty_defaults_to_spread():
    # Arrange — burn-to-zero stays GATED on the fleet reconciler.
    raw = ""
    # Act
    policy = resolve_7d_policy(raw)
    # Assert
    assert policy == POLICY_SPREAD


def test_resolve_policy_accepts_burn():
    # Arrange
    raw = "burn"
    # Act
    policy = resolve_7d_policy(raw)
    # Assert
    assert policy == POLICY_BURN


def test_resolve_policy_normalises_case_and_whitespace():
    # Arrange
    raw = "  BURN "
    # Act
    policy = resolve_7d_policy(raw)
    # Assert
    assert policy == POLICY_BURN


def test_resolve_policy_rejects_unknown_value():
    # Arrange — an operator who asked for a policy must never silently
    # get another (no silent fallback).
    raw = "hoard"
    # Act
    ctx = pytest.raises(ValueError, match="hoard")
    # Assert
    with ctx:
        resolve_7d_policy(raw)


@pytest.fixture
def _burn_env() -> "Iterator[None]":
    """Set the REAL SAC_CREDS_7D_POLICY env var; restore on teardown."""
    saved = os.environ.get("SAC_CREDS_7D_POLICY")
    saved_long = os.environ.pop("SCITEX_AGENT_CONTAINER_CREDS_7D_POLICY", None)
    os.environ["SAC_CREDS_7D_POLICY"] = "burn"
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("SAC_CREDS_7D_POLICY", None)
        else:
            os.environ["SAC_CREDS_7D_POLICY"] = saved
        if saved_long is not None:
            os.environ["SCITEX_AGENT_CONTAINER_CREDS_7D_POLICY"] = saved_long


def test_resolve_policy_reads_sac_env(_burn_env: None):
    # Arrange — the real env rail (SAC_ prefix; long form unset) is set
    # by the fixture.
    # Act
    policy = resolve_7d_policy()
    # Assert
    assert policy == POLICY_BURN


def test_validate_policy_rejects_unknown_value():
    # Arrange
    policy = "greedy"
    # Act
    ctx = pytest.raises(ValueError, match="greedy")
    # Assert
    with ctx:
        validate_7d_policy(policy)


def test_pick_ranked_rejects_unknown_policy():
    # Arrange
    names = ["a"]
    # Act
    ctx = pytest.raises(ValueError, match="bogus")
    # Assert
    with ctx:
        pick_ranked(names, {"a": 1.0}, {"a": 1.0}, policy="bogus")


# ---------------------------------------------------------------------------
# POLICY_BURN ordering — spend the perishable weekly bucket first
# ---------------------------------------------------------------------------


def test_burn_prefers_highest_7d_usage():
    # Arrange — nobody blocked; b holds the fullest (most perishable) bucket.
    names = ["a", "b", "c"]
    u5 = {"a": 10.0, "b": 10.0, "c": 10.0}
    u7 = {"a": 10.0, "b": 50.0, "c": 30.0}
    # Act
    picked = pick_ranked(names, u5, u7, now=_NOW, policy=POLICY_BURN)
    # Assert — highest 7d usage wins (spread would prefer a).
    assert picked == "b"


def test_burn_never_picks_a_5h_blocked_account_over_an_unblocked_one():
    # Arrange — a holds the fullest 7d bucket but cannot serve NOW.
    names = ["a", "b"]
    u5 = {"a": 100.0, "b": 10.0}
    u7 = {"a": 90.0, "b": 50.0}
    # Act
    picked = pick_ranked(names, u5, u7, now=_NOW, policy=POLICY_BURN)
    # Assert — the 5h gate stays supreme under burn.
    assert picked == "b"


def test_burn_tie_breaks_by_soonest_7d_reset():
    # Arrange — equal 7d usage; b's window resets sooner (its remainder
    # is destroyed sooner — the operator's original insight).
    names = ["a", "b"]
    u5 = {"a": 10.0, "b": 10.0}
    u7 = {"a": 50.0, "b": 50.0}
    resets = {"a": _NOW + 4 * 86_400.0, "b": _NOW + 2 * 86_400.0}
    # Act
    picked = pick_ranked(names, u5, u7, reset_7d=resets, now=_NOW, policy=POLICY_BURN)
    # Assert
    assert picked == "b"


def test_burn_is_deterministic_across_spread_keys():
    # Arrange — burn deliberately concentrates: spread_key must not
    # change the winner (「落ちたら再起動」 — draining is the point).
    names = ["a", "b", "c"]
    u5 = {n: 10.0 for n in names}
    u7 = {"a": 20.0, "b": 60.0, "c": 40.0}
    # Act
    picks = {
        pick_ranked(names, u5, u7, now=_NOW, spread_key=key, policy=POLICY_BURN)
        for key in (None, "agent-one", "agent-two")
    }
    # Assert — one winner regardless of the key.
    assert picks == {"b"}


def test_burn_known_7d_usage_beats_unknown():
    # Arrange — a's 7d usage is unknown; known usage beats a guess.
    names = ["a", "b"]
    u5 = {"a": 10.0, "b": 10.0}
    u7 = {"a": None, "b": 5.0}
    # Act
    picked = pick_ranked(names, u5, u7, now=_NOW, policy=POLICY_BURN)
    # Assert
    assert picked == "b"


def test_burn_all_blocked_still_boots_on_highest_7d_usage():
    # Arrange — quota is a preference, never a gate: an all-blocked
    # fleet still boots, on the fullest perishable bucket.
    names = ["a", "b"]
    u5 = {"a": 100.0, "b": 96.0}
    u7 = {"a": 80.0, "b": 10.0}
    # Act
    picked = pick_ranked(names, u5, u7, now=_NOW, policy=POLICY_BURN)
    # Assert
    assert picked == "a"


def test_burn_near_cap_is_a_reason_to_pick_not_to_avoid():
    # Arrange — a sits at 96% of its 7d window (spread demotes it to the
    # near-capped tier; burn inverts: unspent quota is destroyed at the
    # boundary, so drain it to zero).
    names = ["a", "b"]
    u5 = {"a": 10.0, "b": 10.0}
    u7 = {"a": 96.0, "b": 50.0}
    # Act
    picked = pick_ranked(names, u5, u7, now=_NOW, policy=POLICY_BURN)
    # Assert
    assert picked == "a"


def test_default_policy_matches_explicit_spread():
    # Arrange — omitting ``policy`` must be EXACTLY the historical pick.
    names = ["a", "b", "c"]
    u5 = {n: 10.0 for n in names}
    u7 = {"a": 20.0, "b": 60.0, "c": 40.0}
    # Act
    default_pick = pick_ranked(names, u5, u7, now=_NOW, spread_key="alpha")
    explicit_pick = pick_ranked(
        names, u5, u7, now=_NOW, spread_key="alpha", policy=POLICY_SPREAD
    )
    # Assert
    assert default_pick == explicit_pick


def test_burn_on_the_observed_20260717_pool_state():
    # Arrange — the exact pool state from the operator's report
    # (5h 10/3/25, 7d 11/3/25, resets +2d8h/+4d3h/+3d18h). Under the
    # corrected rule usage DOMINATES and reset only tie-breaks, so the
    # winner is the FULLEST 7d bucket — ywatanabe-scitex-ai at 25% —
    # not the soonest-resetting wyusuuke (that was the pre-correction
    # ordering).
    names = ["wyusuuke-gmail-com", "ywata1989-gmail-com", "ywatanabe-scitex-ai"]
    u5 = {names[0]: 10.0, names[1]: 3.0, names[2]: 25.0}
    u7 = {names[0]: 11.0, names[1]: 3.0, names[2]: 25.0}
    resets = {
        names[0]: _NOW + (2 * 24 + 8) * 3_600.0,
        names[1]: _NOW + (4 * 24 + 3) * 3_600.0,
        names[2]: _NOW + (3 * 24 + 18) * 3_600.0,
    }
    # Act
    picked = pick_ranked(names, u5, u7, reset_7d=resets, now=_NOW, policy=POLICY_BURN)
    # Assert
    assert picked == "ywatanabe-scitex-ai"


# ---------------------------------------------------------------------------
# pick_healthy_account — the preferred-pin inversion under burn
# ---------------------------------------------------------------------------


def _write_snapshot(home: Path, name: str, expires_at_ms: int) -> Path:
    path = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}}))
    return path


def _fresh_ms() -> int:
    return int((time.time() + 7_200.0) * 1_000)


def test_spread_rotates_off_a_near_capped_pin(tmp_path: Path):
    # Arrange — the pinned account is near-capped (95%, not expiring).
    _write_snapshot(tmp_path, "pin", _fresh_ms())
    _write_snapshot(tmp_path, "alt", _fresh_ms())
    quota = {"pin": 95.0, "alt": 10.0}
    # Act
    picked = pick_healthy_account(
        "pin",
        candidates=["pin", "alt"],
        home=tmp_path,
        usage_5h={},
        usage_7d=quota,
        reset_7d={},
        policy=POLICY_SPREAD,
    )
    # Assert — the historical avoidance: rotate off the near-capped pin.
    assert picked == "alt"


def test_burn_keeps_a_near_capped_pin_and_drains_it(tmp_path: Path):
    # Arrange — same state; burn inverts: near-cap is a reason to STAY.
    _write_snapshot(tmp_path, "pin", _fresh_ms())
    _write_snapshot(tmp_path, "alt", _fresh_ms())
    quota = {"pin": 95.0, "alt": 10.0}
    # Act
    picked = pick_healthy_account(
        "pin",
        candidates=["pin", "alt"],
        home=tmp_path,
        usage_5h={},
        usage_7d=quota,
        reset_7d={},
        policy=POLICY_BURN,
    )
    # Assert
    assert picked == "pin"


def test_burn_still_rotates_off_a_5h_blocked_pin(tmp_path: Path):
    # Arrange — burn never overrides the 5h gate: a blocked-now pin
    # cannot serve a request no matter how full its weekly bucket is.
    _write_snapshot(tmp_path, "pin", _fresh_ms())
    _write_snapshot(tmp_path, "alt", _fresh_ms())
    # Act
    picked = pick_healthy_account(
        "pin",
        candidates=["pin", "alt"],
        home=tmp_path,
        usage_5h={"pin": 100.0, "alt": 5.0},
        usage_7d={"pin": 95.0, "alt": 10.0},
        reset_7d={},
        policy=POLICY_BURN,
    )
    # Assert
    assert picked == "alt"


# ---------------------------------------------------------------------------
# The auditable ranking-input record
# ---------------------------------------------------------------------------


def test_audit_records_seconds_to_7d_reset():
    # Arrange
    names = ["a"]
    resets = {"a": _NOW + 3_600.0}
    # Act
    records = audit_candidates(names, {"a": 5.0}, {"a": 7.0}, reset_7d=resets, now=_NOW)
    # Assert — time-to-reset is IN the record (the missing input).
    assert records[0].reset_7d_in_s == 3_600.0


def test_audit_line_names_every_candidate():
    # Arrange
    names = ["a", "b"]
    u = {"a": 5.0, "b": 7.0}
    # Act
    line = format_pick_audit(audit_candidates(names, u, u, now=_NOW))
    # Assert
    assert "a(" in line and "b(" in line


def test_audit_line_carries_reset_hours():
    # Arrange
    names = ["a"]
    resets = {"a": _NOW + 3_600.0}
    # Act
    line = format_pick_audit(
        audit_candidates(names, {"a": 5.0}, {"a": 7.0}, reset_7d=resets, now=_NOW)
    )
    # Assert
    assert "7d_reset=+1.0h" in line


def test_audit_line_flags_a_5h_blocked_candidate():
    # Arrange
    names = ["a"]
    # Act
    line = format_pick_audit(
        audit_candidates(names, {"a": 100.0}, {"a": 7.0}, now=_NOW)
    )
    # Assert
    assert "5h-blocked" in line


def test_audit_line_renders_unknowns_as_question_marks():
    # Arrange — no cached usage / reset at all.
    names = ["a"]
    # Act
    line = format_pick_audit(audit_candidates(names, {}, {}, now=_NOW))
    # Assert
    assert "5h=? 7d=? 7d_reset=?" in line
