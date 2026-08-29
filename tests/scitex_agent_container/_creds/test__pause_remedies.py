"""When a pause blocks something, the message must offer the lever that lifts it.

A NEW STATE FLOWING INTO A MESSAGE WRITTEN FOR TWO is the defect this
file guards, and it has now happened three times in this codebase. The
original was :func:`._account.mint_token.mint_access_only_artifact`: a
two-way branch (EXPIRED, else "no snapshot on disk") written before
FORBIDDEN existed, so on 2026-08-26 the operator's timer told him a
file was missing while it sat there, refreshed hours earlier.

Reviewed 2026-08-26: that repair was applied at the mint and NOT at its
two siblings, both of which drop a PAUSED account and then explain the
absence with the vocabulary of a fault.

* THE BOOT PICKER. ``_start_single`` prints ``exc.brief`` in red and
  ``str(exc)`` dim, so the brief is the line that is actually read.
  With every account paused it said "no fresh account among … — run
  ``claude /login`` then ``sac accounts sync-live``". Neither command
  lifts a pause; ``sac accounts resume`` does. The dim line named the
  pauses and then closed with the same wrong instruction.
* QUOTA ROTATION. ``check_and_rotate`` reported "no HEALTHY account to
  rotate to (other accounts absent or credential-expired)". In the
  operator's stated workflow — rest three accounts, keep one — that
  names a fault that does not exist while the surviving account keeps
  burning, which is the opposite of what he paused the others for.

Both are asserted here with the UNPAUSED case as the reversing
control, because a fix that made every message say "resume" would be
just as wrong in the other direction: an expired token really is fixed
by ``claude /login``, and losing that would trade one wrong remedy for
another.

NO MOCKS (PA-306): real account dirs on a real ``tmp_path``, real
``pause.json`` files written by :func:`._pause.write_pause`, and the
``store_dir`` / ``home`` / ``now`` parameters both functions already
take.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._creds._pause import Pause, write_pause

ALPHA = "alpha-example-com"
BETA = "beta-example-com"
ALPHA_REASON = "resting alpha while the quota recovers"
BETA_REASON = "beta's subscription is stopped for now"


def _write_account(store: Path, name: str, *, hours_left: float = 8.0) -> Path:
    account_dir = store / name
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-a-real-token",
                    "refreshToken": "refresh-not-a-real-token",
                    "expiresAt": int((time.time() + hours_left * 3600) * 1000),
                }
            }
        )
    )
    (account_dir / "account.json").write_text(
        json.dumps({"name": name, "email_address": f"{name}@example.com"})
    )
    return account_dir


def _pause(account_dir: Path, name: str, reason: str) -> None:
    write_pause(
        account_dir,
        Pause(
            name=name,
            active=True,
            reason=reason,
            since=time.time() - 7200,
            by="operator@test-host",
        ),
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / ".scitex" / "agent-container" / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.fixture
def all_paused(store: Path) -> Path:
    """Both accounts fresh, both rested. Nothing is broken in this store."""
    _pause(_write_account(store, ALPHA), ALPHA, ALPHA_REASON)
    _pause(_write_account(store, BETA), BETA, BETA_REASON)
    return store


@pytest.fixture
def all_expired(store: Path) -> Path:
    """The reversing control: nothing paused, every token stale."""
    _write_account(store, ALPHA, hours_left=-5.0)
    _write_account(store, BETA, hours_left=-5.0)
    return store


# ---------------------------------------------------------------------------
# The boot picker
# ---------------------------------------------------------------------------


def _boot_error(store: Path, tmp_path: Path):
    from scitex_agent_container._creds._pick_healthy import (
        NoHealthyAccountError,
        pick_healthy_account,
    )

    with pytest.raises(NoHealthyAccountError) as excinfo:
        pick_healthy_account(None, store_dir=store, home=tmp_path)
    return excinfo.value


def test_an_all_paused_boot_error_says_paused_in_the_line_that_is_read(
    all_paused, tmp_path
):
    """``brief`` is the RED line; the dim one is not where a reader starts."""
    # Arrange
    store = all_paused
    # Act
    brief = _boot_error(store, tmp_path).brief
    # Assert
    assert "PAUSED" in brief


def test_an_all_paused_boot_error_offers_resume_in_the_line_that_is_read(
    all_paused, tmp_path
):
    """``claude /login`` cannot lift a pause. One command can."""
    # Arrange
    store = all_paused
    # Act
    brief = _boot_error(store, tmp_path).brief
    # Assert
    assert "sac accounts resume" in brief


def test_an_all_paused_boot_error_does_not_offer_login_in_the_line_that_is_read(
    all_paused, tmp_path
):
    """The negative half: a remedy that cannot work must not be the one shown."""
    # Arrange
    store = all_paused
    # Act
    brief = _boot_error(store, tmp_path).brief
    # Assert
    assert "claude /login" not in brief


def test_an_all_paused_boot_error_names_every_reason_in_the_detail(
    all_paused, tmp_path
):
    """The dim line still carries the evidence: WHICH pause, and why."""
    # Arrange
    store = all_paused
    # Act
    detail = str(_boot_error(store, tmp_path))
    # Assert
    assert ALPHA_REASON in detail and BETA_REASON in detail


def test_an_all_expired_boot_error_still_offers_login(all_expired, tmp_path):
    """THE REVERSING CONTROL. An expired token really is fixed by a login.

    Without this, replacing the remedy everywhere would satisfy the
    assertions above and would break the case they were written for.
    """
    # Arrange
    store = all_expired
    # Act
    brief = _boot_error(store, tmp_path).brief
    # Assert
    assert "claude /login" in brief


def test_an_all_expired_boot_error_does_not_say_paused(all_expired, tmp_path):
    """The control for the PAUSED assertion: it must come from the pauses."""
    # Arrange
    store = all_expired
    # Act
    brief = _boot_error(store, tmp_path).brief
    # Assert
    assert "PAUSED" not in brief


# ---------------------------------------------------------------------------
# Quota rotation
# ---------------------------------------------------------------------------


_ACCOUNTS = [
    {"name": ALPHA, "email_address": f"{ALPHA}@example.com"},
    {"name": BETA, "email_address": f"{BETA}@example.com"},
]
_CURRENT = f"{ALPHA}@example.com"


def _rested(store: Path, tmp_path: Path) -> list[str]:
    """The list the "nothing to rotate to" alert builds its diagnosis from.

    HONEST SCOPE, stated rather than implied: this drives the INPUT to
    that message, not ``check_and_rotate`` end to end. Reaching the
    message itself needs a live quota reading from Anthropic, which
    this suite has no offline way to produce. What is measured here is
    the discriminator — "was the candidate refused by the operator's
    decision, or by a fault?" — which is the half that was missing and
    the half a future edit can silently break. The wording that hangs
    off it is one branch away in ``check_and_rotate``.
    """
    from scitex_agent_container._account.quota_watch import _paused_candidates

    return _paused_candidates(
        _ACCOUNTS, _CURRENT, store_dir=store, home=tmp_path
    )


def test_rotation_reports_a_paused_alternative_as_paused(all_paused, tmp_path):
    """An empty list here makes the alert blame a fault that does not exist."""
    # Arrange
    store = all_paused
    # Act
    rested = _rested(store, tmp_path)
    # Assert
    assert BETA in rested


def test_rotation_reports_no_paused_alternative_when_none_is_paused(
    all_expired, tmp_path
):
    """The control: an EXPIRED alternative is a fault and must not read as a rest."""
    # Arrange
    store = all_expired
    # Act
    rested = _rested(store, tmp_path)
    # Assert
    assert rested == []


def test_a_paused_account_is_never_rotated_onto(all_paused, tmp_path):
    """Rotating onto a rested account is exactly the spend he paused it to avoid."""
    # Arrange
    from scitex_agent_container._account.quota_watch import _select_next_account

    store = all_paused
    # Act
    picked = _select_next_account(
        _ACCOUNTS, _CURRENT, store_dir=store, home=tmp_path
    )
    # Assert
    assert picked is None


def test_an_unpaused_healthy_alternative_is_still_rotated_onto(store, tmp_path):
    """The control for the assertion above: rotation must still work."""
    # Arrange
    _write_account(store, ALPHA)
    _write_account(store, BETA)
    from scitex_agent_container._account.quota_watch import _select_next_account

    # Act
    picked = _select_next_account(
        _ACCOUNTS, _CURRENT, store_dir=store, home=tmp_path
    )
    # Assert
    assert picked is not None and picked["name"] == BETA
