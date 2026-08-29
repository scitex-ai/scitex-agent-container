"""The picker must actually ROTATE OFF a de-entitled account.

:mod:`test__entitlement` proves the verdict module in isolation. That
is not the thing that failed on 2026-08-25 — the thing that failed is
that a de-entitled account stayed SELECTABLE. So these tests drive the
real :func:`account_health` and :func:`pick_healthy_account` over a
real on-disk store, and assert on the choice they make.

Without this file the entitlement module could be perfect and entirely
unwired, and every other test would still pass. That is precisely the
shape of the original bug: a check that was correct about the question
it asked, while nothing asked the question that mattered.

No mocks. Real dirs, real JSON, real functions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._creds._account_health import (
    NoHealthyAccountError,
    account_health,
)
from scitex_agent_container._creds._entitlement import (
    ENTITLED,
    FORBIDDEN,
    UNKNOWN,
    Entitlement,
    write_entitlement,
)
from scitex_agent_container._creds._pick_healthy import pick_healthy_account

# A REALISTIC epoch, and that matters. `_oauth_expiry_to_seconds`
# treats a raw value as milliseconds only when it exceeds 1e12; below
# that it is read as seconds. An earlier draft of this file used
# `_NOW = 1_000_000`, which put every "milliseconds" constant near 1e9
# — under the threshold — so the values were silently interpreted as
# SECONDS and landed in the far future. The expired-token test caught
# it (EXPIRED came back FORBIDDEN), but the fresh-token tests had been
# passing for the wrong reason: they never exercised the millisecond
# branch that production always takes. Anchoring at a real 2026 epoch
# puts these above 1e12 so the tests normalise exactly as production
# does.
_NOW = 1_787_000_000.0  # 2026-08-17T...Z, unix seconds
_FRESH_MS = (_NOW + 6 * 3600) * 1000.0
_STALE_MS = (_NOW - 6 * 3600) * 1000.0


def _store(tmp_path: Path) -> Path:
    d = tmp_path / ".scitex" / "agent-container" / "accounts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _account(
    store: Path,
    name: str,
    *,
    expires_ms: float = _FRESH_MS,
    entitlement: str | None = None,
) -> Path:
    """A real account dir: a real snapshot, optionally a real verdict."""
    d = store / name
    d.mkdir(parents=True, exist_ok=True)
    (d / ".credentials.json").write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "tok", "expiresAt": expires_ms}}
        )
    )
    if entitlement is not None:
        write_entitlement(
            d, Entitlement(name, entitlement, checked_at=_NOW - 60)
        )
    return d


# ---------------------------------------------------------------------------
# account_health now answers the second question
# ---------------------------------------------------------------------------


@pytest.fixture
def forbidden_store(tmp_path: Path) -> Path:
    """One account: token FRESH, subscription CANCELLED.

    This is wyusuuke on the morning of 2026-08-25 — refreshed at 09:17
    with a new expiry of 17:17, and a 403 on every real turn.
    """
    store = _store(tmp_path)
    _account(store, "cancelled", entitlement=FORBIDDEN)
    return store


def test_a_fresh_but_cancelled_account_is_not_valid(forbidden_store):
    # Arrange: see the fixture. Before this change the state was VALID.
    # Act
    health = account_health("cancelled", store_dir=forbidden_store, now=_NOW)
    # Assert
    assert health.state == "FORBIDDEN"


def test_a_fresh_but_cancelled_account_is_not_healthy(forbidden_store):
    # Arrange: is_healthy is what every caller actually gates on.
    # Act
    health = account_health("cancelled", store_dir=forbidden_store, now=_NOW)
    # Assert
    assert health.is_healthy is False


def test_the_forbidden_health_record_carries_the_reason(forbidden_store):
    # Arrange: "your token is fine but you cannot use it" reads as OUR
    # bug unless the API's own words travel with it.
    write_entitlement(
        forbidden_store / "cancelled",
        Entitlement(
            "cancelled",
            FORBIDDEN,
            checked_at=_NOW - 60,
            detail="not allowed for this organization",
        ),
    )
    # Act
    health = account_health("cancelled", store_dir=forbidden_store, now=_NOW)
    # Assert
    assert "organization" in health.entitlement_detail


def test_an_entitled_account_stays_valid(tmp_path):
    # Arrange
    store = _store(tmp_path)
    _account(store, "good", entitlement=ENTITLED)
    # Act
    health = account_health("good", store_dir=store, now=_NOW)
    # Assert
    assert health.state == "VALID"


def test_unknown_entitlement_does_not_downgrade_a_fresh_account(tmp_path):
    # Arrange: the constitution's rule at the integration level. An
    # account we have never probed -- or could not reach -- must keep
    # working. Collapsing UNKNOWN into FORBIDDEN here would black out
    # the whole fleet the first time this host lost its uplink.
    store = _store(tmp_path)
    _account(store, "unprobed", entitlement=UNKNOWN)
    # Act
    health = account_health("unprobed", store_dir=store, now=_NOW)
    # Assert
    assert health.state == "VALID"


def test_a_never_probed_account_stays_valid(tmp_path):
    # Arrange: no verdict file at all -- every account, the moment this
    # ships, before the timer has run even once. Must be a no-op.
    store = _store(tmp_path)
    _account(store, "virgin")
    # Act
    health = account_health("virgin", store_dir=store, now=_NOW)
    # Assert
    assert health.state == "VALID"


def test_an_expired_token_is_not_relabelled_forbidden(tmp_path):
    # Arrange: an EXPIRED snapshot that is ALSO de-entitled. Expiry is
    # the actionable fault (log in again); hiding it behind FORBIDDEN
    # would send the operator to fix the wrong thing.
    store = _store(tmp_path)
    _account(store, "both", expires_ms=_STALE_MS, entitlement=FORBIDDEN)
    # Act
    health = account_health("both", store_dir=store, now=_NOW)
    # Assert
    assert health.state == "EXPIRED"


# ---------------------------------------------------------------------------
# the picker: the behaviour that actually failed
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_store(tmp_path: Path) -> Path:
    """A cancelled account and a working one, both token-fresh."""
    store = _store(tmp_path)
    _account(store, "cancelled", entitlement=FORBIDDEN)
    _account(store, "working", entitlement=ENTITLED)
    return store


def test_the_picker_rotates_off_a_cancelled_preferred(mixed_store):
    # Arrange: THE INCIDENT. The agent prefers the cancelled account and
    # its token is fresh, so before this change it was handed straight
    # back and the turn died on a 403.
    # Act
    chosen = pick_healthy_account(
        "cancelled", store_dir=mixed_store, home=mixed_store, now=_NOW
    )
    # Assert
    assert chosen == "working"


def test_the_picker_never_returns_a_cancelled_account(mixed_store):
    # Arrange: with no preference at all it must still avoid it.
    # Act
    chosen = pick_healthy_account(
        None, store_dir=mixed_store, home=mixed_store, now=_NOW
    )
    # Assert
    assert chosen != "cancelled"


def test_the_picker_keeps_an_entitled_preferred(mixed_store):
    # Arrange: churn control -- a healthy preference must be honoured.
    # Act
    chosen = pick_healthy_account(
        "working", store_dir=mixed_store, home=mixed_store, now=_NOW
    )
    # Assert
    assert chosen == "working"


def test_the_picker_fails_loudly_when_every_account_is_cancelled(tmp_path):
    # Arrange: the genuine "nothing usable" case. It must RAISE, not
    # silently hand back a dead account -- the same fail-loud contract
    # the freshness gate already has.
    store = _store(tmp_path)
    _account(store, "dead-a", entitlement=FORBIDDEN)
    _account(store, "dead-b", entitlement=FORBIDDEN)
    # Act
    pick = lambda: pick_healthy_account(  # noqa: E731
        None, store_dir=store, home=store, now=_NOW
    )
    # Assert
    with pytest.raises(NoHealthyAccountError):
        pick()


def test_the_failure_message_names_the_cancelled_state(tmp_path):
    # Arrange: the operator must learn it is a SUBSCRIPTION problem, not
    # a login problem. Those have different fixes.
    store = _store(tmp_path)
    _account(store, "dead-a", entitlement=FORBIDDEN)
    # Act
    try:
        pick_healthy_account(None, store_dir=store, home=store, now=_NOW)
        message = ""
    except NoHealthyAccountError as exc:
        message = str(exc)
    # Assert
    assert "FORBIDDEN" in message


def test_a_restored_subscription_returns_the_account_to_the_pool(mixed_store):
    # Arrange: the operator's workflow end to end. The timer re-probes
    # and overwrites the verdict; nothing else changes -- no spec edit,
    # no symlink rename, no restart of anything.
    write_entitlement(
        mixed_store / "cancelled",
        Entitlement("cancelled", ENTITLED, checked_at=_NOW - 60),
    )
    # Act
    chosen = pick_healthy_account(
        "cancelled", store_dir=mixed_store, home=mixed_store, now=_NOW
    )
    # Assert
    assert chosen == "cancelled"
