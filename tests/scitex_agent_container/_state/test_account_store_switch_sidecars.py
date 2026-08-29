"""A switch establishes a LIVE LOGIN; the store's sidecars stay in the store.

``switch_account`` copies an account directory's files into
``~/.claude`` so the next ``claude`` invocation uses that credential. It
did so by copying EVERYTHING except ``account.json``, which was fine
while the only other file was the credential and became less fine with
each sidecar the store learned to keep beside it.

Reviewed 2026-08-26, when the pause change added a fifth. Measured:
after seeding an account with a credential, an entitlement verdict and
an ACTIVE PAUSE and calling ``switch_account``, ``~/.claude`` held
``['.credentials.json', 'entitlement.json', 'pause.json']``. The store's
pause survived intact — nothing was corrupted and nothing was
resurrected — so this is litter rather than a defect in the pause
itself. It is worth removing anyway, and the pause is what made it
worth naming: ``~/.claude`` is a directory that gets copied, backed up
and bind-mounted, and a decision-shaped file sitting in it invites a
future reader to treat it as authority over the live session. It is not
one; nothing reads it there (checked fleet-wide: zero references).

Each of these files answers a question about the STORED ACCOUNT — who
it is, whether the API still accepts it, how much quota is gone,
whether the operator has rested it — and none of them says anything
about the login being established. The credential does, and the
credential still travels; the control below is what pins that.

NO MOCKS (PA-306): a real store and a real ``$HOME`` on ``tmp_path``,
driven through the ``store_dir`` / ``home`` parameters the function
already takes.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._creds._entitlement import (
    FORBIDDEN,
    Entitlement,
    write_entitlement,
)
from scitex_agent_container._creds._pause import Pause, read_pause, write_pause
from scitex_agent_container._state.account_store import switch_account

NAME = "alpha-example-com"
REASON = "quota rest — restarting the subscription later"


@pytest.fixture
def switched(tmp_path: Path) -> Path:
    """Seed one account with every sidecar, switch onto it, return ``~/.claude``."""
    home = tmp_path / "home"
    home.mkdir()
    store = tmp_path / "accounts"
    account_dir = store / NAME
    account_dir.mkdir(parents=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "access-not-a-real-token",
                    "refreshToken": "refresh-not-a-real-token",
                    "expiresAt": int((time.time() + 8 * 3600) * 1000),
                }
            }
        )
    )
    (account_dir / "account.json").write_text(json.dumps({"name": NAME}))
    (account_dir / "usage.json").write_text(json.dumps({"used_pct_5h": 12.0}))
    (account_dir / "identity.json").write_text(json.dumps({"state": "verified"}))
    write_entitlement(
        account_dir,
        Entitlement(
            name=NAME,
            state=FORBIDDEN,
            checked_at=time.time(),
            http_status=403,
            detail="Your organization has disabled Claude Code",
        ),
    )
    write_pause(
        account_dir,
        Pause(
            name=NAME,
            active=True,
            reason=REASON,
            since=time.time() - 3600,
            by="operator@test-host",
        ),
    )
    result = switch_account(NAME, store_dir=store, home=home)
    assert result["success"] is True, result
    return home / ".claude"


@pytest.mark.parametrize(
    "sidecar",
    ["pause.json", "entitlement.json", "identity.json", "usage.json", "account.json"],
)
def test_a_store_sidecar_does_not_follow_the_switch(switched: Path, sidecar: str):
    """``pause.json`` is the one that made this worth fixing; the rest ride along."""
    # Arrange
    landed = switched / sidecar
    # Act
    exists = landed.exists()
    # Assert
    assert exists is False


def test_the_credential_itself_still_follows_the_switch(switched: Path):
    """THE REVERSING CONTROL. Copying nothing would satisfy every test above.

    The credential is the entire purpose of the copy loop, so this is
    the assertion that stops the fix from becoming a bigger bug than
    the litter it removes.
    """
    # Arrange
    landed = switched / ".credentials.json"
    # Act
    payload = json.loads(landed.read_text())
    # Assert
    assert payload["claudeAiOauth"]["accessToken"] == "access-not-a-real-token"


def test_the_stores_own_pause_is_untouched_by_a_switch(switched: Path, tmp_path: Path):
    """A switch reads the account dir; it must not disturb the decision in it."""
    # Arrange
    account_dir = tmp_path / "accounts" / NAME
    # Act
    stored = read_pause(NAME, account_dir)
    # Assert
    assert stored.active is True and stored.reason == REASON
