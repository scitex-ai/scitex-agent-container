"""Tests for the ``sac accounts refresh`` rotate-only-when-stale gate.

INCIDENT 2026-08-09: the gate was skipped for a single NAMED account
(``if force or not do_all: return True``), so a diagnostic
``sac accounts refresh <name>`` rotated a single-use refresh_token and
stranded every running agent holding that account's access token with
401s — while the timer's ``--all`` path had been correctly skipping the
same account as still fresh every ten minutes.

The regression guard is :func:`test_named_path_refuses_a_fresh_token`
together with :func:`test_fresh_token_is_not_refreshed`: restore the
``not do_all`` short-circuit and they fail.

No-mocks (PA-306): the gate is pure, so ``now`` is injected as a plain
float and the CLI test drives real on-disk credentials under a real
(redirected) HOME. The refusal happens BEFORE any network call, so the
CLI test needs no HTTP boundary injection at all — which is itself part
of the contract being asserted.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_refresh_gate import (
    hours_left,
    iso_ms,
    needs_refresh,
    refusal_message,
)
from scitex_agent_container.cli_pkg.account_group import account

# A fixed clock so no gate test depends on the wall clock.
_NOW_S = 1_786_000_000.0
_FRESH_MS = int((_NOW_S + 8 * 3600) * 1000)  # 8h of life left
_STALE_MS = int((_NOW_S + 0.5 * 3600) * 1000)  # 30min left
_EXPIRED_MS = int((_NOW_S - 3600) * 1000)  # expired an hour ago

# The CLI path reads the REAL clock (there is no `now` seam through
# click), so a fixture-clock value would drift into the past and be read
# as EXPIRED — which is how the first version of the CLI test below
# failed. Seed CLI credentials with a genuinely far-future expiry.
_FUTURE_MS = 9_999_999_999_000


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _seed_account(home: Path, name: str, *, expires_ms: int) -> Path:
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    oauth: dict[str, Any] = {
        "accessToken": "OLD-ACCESS",
        "refreshToken": "the-refresh",
        "clientId": "cid",
        "expiresAt": expires_ms,
    }
    creds.write_text(json.dumps({"claudeAiOauth": oauth}))
    return creds


# ---------------------------------------------------------------- the gate


def test_fresh_token_is_not_refreshed():
    # Arrange
    expires_ms = _FRESH_MS
    # Act
    result = needs_refresh(expires_ms, force=False, min_ttl_hours=2.0, now=_NOW_S)
    # Assert
    assert result is False


def test_stale_token_is_refreshed():
    # Arrange
    expires_ms = _STALE_MS
    # Act
    result = needs_refresh(expires_ms, force=False, min_ttl_hours=2.0, now=_NOW_S)
    # Assert
    assert result is True


def test_expired_token_is_refreshed():
    # Arrange
    expires_ms = _EXPIRED_MS
    # Act
    result = needs_refresh(expires_ms, force=False, min_ttl_hours=2.0, now=_NOW_S)
    # Assert
    assert result is True


def test_unknown_expiry_is_refreshed():
    # Arrange: absence must not be read as "fresh"
    expires_ms = None
    # Act
    result = needs_refresh(expires_ms, force=False, min_ttl_hours=2.0, now=_NOW_S)
    # Assert
    assert result is True


def test_force_refreshes_a_fresh_token():
    # Arrange
    expires_ms = _FRESH_MS
    # Act
    result = needs_refresh(expires_ms, force=True, min_ttl_hours=2.0, now=_NOW_S)
    # Assert
    assert result is True


def test_gate_takes_no_do_all_parameter():
    # Arrange: the asymmetry WAS the bug, so the signature must not offer
    # a way to express "skip the gate because the account was named".
    gate = needs_refresh
    # Act
    params = inspect.signature(gate).parameters
    # Assert
    assert "do_all" not in params


# ------------------------------------------------------------ hours_left


def test_hours_left_reads_milliseconds():
    # Arrange
    expires_ms = _FRESH_MS
    # Act
    result = hours_left(expires_ms, _NOW_S)
    # Assert
    assert result == pytest.approx(8.0)


def test_hours_left_reads_the_boundary_value_as_milliseconds():
    # Arrange: 1e12 is the exact value a seconds-vs-milliseconds
    # auto-detect (`> 1e12` -> ms) misreads as the year 33658. It is
    # milliseconds — 2001 — and reading it as "fresh" would silently stop
    # the gate ever refreshing that account.
    boundary_ms = 1_000_000_000_000
    # Act
    result = hours_left(boundary_ms, _NOW_S)
    # Assert
    assert result < 0


def test_hours_left_is_none_for_absent_expiry():
    # Arrange
    expires_ms = None
    # Act
    result = hours_left(expires_ms, _NOW_S)
    # Assert
    assert result is None


def test_hours_left_rejects_bool():
    # Arrange: bool is an int subclass, so True must not read as 1970
    expires_ms = True
    # Act
    result = hours_left(expires_ms, _NOW_S)
    # Assert
    assert result is None


def test_iso_ms_is_none_for_absent_expiry():
    # Arrange
    expires_ms = None
    # Act
    result = iso_ms(expires_ms)
    # Assert
    assert result is None


# ------------------------------------------------------- refusal message


def test_refusal_names_the_override_flag():
    # Arrange
    expiry = "2026-08-09T17:38:54+00:00"
    # Act
    message = refusal_message("acct", expiry, 2.0, is_pinned=True)
    # Assert
    assert "--force" in message


def test_refusal_names_the_account():
    # Arrange
    name = "acct"
    # Act
    message = refusal_message(name, None, 2.0, is_pinned=True)
    # Assert
    assert name in message


def test_refusal_warns_even_when_no_local_agent_is_pinned():
    # Arrange: other hosts bind their own copy of the snapshot, so
    # "nothing pinned locally" must never be reported as safe.
    is_pinned = False
    # Act
    message = refusal_message("acct", None, 2.0, is_pinned=is_pinned)
    # Assert
    assert "OTHER" in message


# ------------------------------------------------------------- CLI wiring


def test_named_path_refuses_a_fresh_token(sandbox_home):
    # Arrange
    _seed_account(sandbox_home, "fresh-acct", expires_ms=_FUTURE_MS)
    # Act: no HTTP boundary is injected — refusing BEFORE any network call
    # is part of the contract, so a request reaching the wire would fail
    # this test by erroring differently.
    result = CliRunner().invoke(account, ["refresh", "fresh-acct"])
    # Assert
    assert result.exit_code == 2


def test_named_refusal_leaves_the_snapshot_untouched(sandbox_home):
    # Arrange
    creds = _seed_account(sandbox_home, "fresh-acct", expires_ms=_FUTURE_MS)
    before = creds.read_text()
    # Act
    CliRunner().invoke(account, ["refresh", "fresh-acct"])
    # Assert: a refused rotation must not have consumed the refresh_token
    assert creds.read_text() == before
