"""WHICH fault the mint refusal names, and which remedy it offers.

THIS FILE EXISTS BECAUSE THE MESSAGE WAS THE INCIDENT. On 2026-08-26
the operator's credential timer told him, on every pass, that
``wyusuuke-gmail-com`` had "no credential snapshot on disk" — while
that account's ``.credentials.json`` sat there, 1146 bytes, refreshed
hours earlier. The file was never the problem: the API had started
answering 403 ``oauth_not_allowed_for_organization`` because the
subscription was cancelled. The refusal in
:func:`_account.mint_token.mint_access_only_artifact` was a two-way
branch — EXPIRED, else "no snapshot" — written before FORBIDDEN
existed, so a state it had never heard of fell into the ``else`` and
inherited a sentence about a missing file. He was handed the wrong
fault and a remedy (`claude /login`) that could not work.

Reviewed 2026-08-26: the branch was repaired and the repair was
UNTESTED. Deleting both new branches left 896 tests green across
``_account`` and ``_creds``, and ``rg -c "cannot mint" tests/``
returned nothing — no test in the repo asserted on ANY mint refusal
message. A fix nothing can hold in place is a fix that comes back.

So the assertions below are deliberately shaped around the failure
rather than around the feature. The one that matters most is negative:
a FORBIDDEN account's refusal must NOT contain "no credential snapshot
on disk", because that is the exact wrong sentence, and asserting only
that the new words are present would stay green if the old ones came
back beside them.

NO MOCKS (PA-306). Real account directories on a real ``tmp_path``,
real ``entitlement.json`` written by the same
:func:`._creds._entitlement.write_entitlement` the ``*/30`` probe
calls, and a real ``pause.json`` written by the same
:func:`._creds._pause.write_pause` the ``pause`` verb calls. The
function under test already takes ``store_dir`` / ``home`` / ``now``
as real parameters, so nothing needs substituting.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from scitex_agent_container._account.mint_token import (
    MintError,
    mint_access_only_artifact,
)
from scitex_agent_container._creds._entitlement import (
    FORBIDDEN,
    Entitlement,
    write_entitlement,
)
from scitex_agent_container._creds._pause import Pause, write_pause

_LABEL = "alpha-example-com"
_DENIAL = "Your organization has disabled Claude Code"
_REASON = "quota rest — the subscription is stopped for now"
#: The sentence that made this file necessary. It is TRUE only of
#: ABSENT, and every other state that prints it is lying to the reader.
_WRONG_SENTENCE = "no credential snapshot on disk"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    path = tmp_path / "accounts"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_fresh_account(store: Path, label: str = _LABEL) -> Path:
    """A stored account whose token is unambiguously fresh for eight hours."""
    account_dir = store / label
    account_dir.mkdir(parents=True, exist_ok=True)
    (account_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "oat-test-access",
                    "refreshToken": "oat-test-refresh",
                    "expiresAt": int((time.time() + 8 * 3600) * 1000),
                    "scopes": ["user:inference"],
                }
            }
        )
    )
    return account_dir


@pytest.fixture
def paused(store: Path) -> Path:
    """A fresh account the operator has rested. Nothing is broken here."""
    account_dir = _write_fresh_account(store)
    write_pause(
        account_dir,
        Pause(
            name=_LABEL,
            active=True,
            reason=_REASON,
            since=time.time() - 3600,
            by="operator@test-host",
        ),
    )
    return store


@pytest.fixture
def forbidden(store: Path) -> Path:
    """A fresh account the API refuses — the operator's 2026-08-26 case."""
    account_dir = _write_fresh_account(store)
    write_entitlement(
        account_dir,
        Entitlement(
            name=_LABEL,
            state=FORBIDDEN,
            checked_at=time.time(),
            http_status=403,
            detail=_DENIAL,
        ),
    )
    return store


@pytest.fixture
def absent(store: Path) -> Path:
    """The ONE state for which "no credential snapshot on disk" is true."""
    (store / _LABEL).mkdir(parents=True, exist_ok=True)
    return store


def _refusal(store: Path, tmp_path: Path) -> str:
    with pytest.raises(MintError) as excinfo:
        mint_access_only_artifact(
            _LABEL, store_dir=store, home=tmp_path, hostname="test-host"
        )
    return str(excinfo.value)


# ---------------------------------------------------------------------------
# PAUSED — a decision, and the remedy is one command
# ---------------------------------------------------------------------------


def test_a_paused_account_is_refused_as_paused(paused, tmp_path):
    """Without its own branch this falls into the ``else`` and says ABSENT."""
    # Arrange
    store = paused
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert "is PAUSED" in message


def test_a_paused_refusal_names_the_command_that_lifts_it(paused, tmp_path):
    """A pause is lifted by exactly one verb; the refusal has to say which."""
    # Arrange
    store = paused
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert f"sac accounts resume {_LABEL}" in message


def test_a_paused_refusal_quotes_the_operators_own_reason(paused, tmp_path):
    """He wrote the reason precisely so it would come back to him here."""
    # Arrange
    store = paused
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert _REASON in message


def test_a_paused_refusal_does_not_claim_the_snapshot_is_missing(paused, tmp_path):
    """PAUSED would have inherited the same wrong sentence FORBIDDEN did."""
    # Arrange
    store = paused
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert _WRONG_SENTENCE not in message


# ---------------------------------------------------------------------------
# FORBIDDEN — the incident itself
# ---------------------------------------------------------------------------


def test_a_forbidden_account_is_refused_as_forbidden(forbidden, tmp_path):
    # Arrange
    store = forbidden
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert "is FORBIDDEN" in message


def test_a_forbidden_refusal_does_not_claim_the_snapshot_is_missing(
    forbidden, tmp_path
):
    """THE ASSERTION THAT PINS THE 2026-08-26 BUG. Negative on purpose.

    Asserting only that the new words appear would stay green if the
    old sentence came back beside them, and the old sentence is what
    sent the operator looking for a file that was never missing.
    """
    # Arrange
    store = forbidden
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert _WRONG_SENTENCE not in message


def test_a_forbidden_refusal_quotes_the_apis_own_words(forbidden, tmp_path):
    """"Your token is fine but you cannot use it" needs the API's reason."""
    # Arrange
    store = forbidden
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert _DENIAL in message


def test_a_forbidden_refusal_offers_the_pause_as_a_way_to_stop_the_noise(
    forbidden, tmp_path
):
    """This is the account the operator asked to be able to rest."""
    # Arrange
    store = forbidden
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert f"sac accounts pause {_LABEL}" in message


# ---------------------------------------------------------------------------
# ABSENT — the control. The old sentence is CORRECT here and must survive.
# ---------------------------------------------------------------------------


def test_an_absent_snapshot_still_says_the_snapshot_is_missing(absent, tmp_path):
    """The reversing control for both negative assertions above.

    Without it, deleting the sentence everywhere would satisfy them —
    and then the one state it truthfully describes would lose it.
    """
    # Arrange
    store = absent
    # Act
    message = _refusal(store, tmp_path)
    # Assert
    assert _WRONG_SENTENCE in message
