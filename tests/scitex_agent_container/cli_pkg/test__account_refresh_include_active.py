"""Tests for ``--include-active`` and the still-fresh (skipped) code path.

Regression coverage for the 2026-07-09/10 total-fleet stall.

Two defects, one outage:

1. The federated timer ran ``--all --skip-active``. That flag was the
   race guard for the pre-2026-07-08 TWO-refresher model (host timer +
   in-container CLI redeeming the same single-use refresh_token). Once
   agents began binding the credential ``:ro`` and never refreshing, the
   timer became the SOLE refresher — and ``--skip-active`` starved the
   one account every agent is pinned to, whose ~8h access_token expired
   and 401'd the whole fleet.

2. ``_iso_ms`` passed the ``datetime.timezone`` CLASS as ``tz=`` instead
   of ``timezone.utc``, so the rotate-only-when-stale branch ("account is
   still fresh, leave it untouched") raised ``TypeError``. Under
   ``--skip-active`` the only targets were always stale, so that branch
   never executed and the crash stayed invisible. The moment the active
   (freshly-logged-in) account entered the target list, every run died —
   and because the crash is uncaught, it aborts the whole ``--all`` loop,
   so a stale account ordered AFTER a fresh one never gets refreshed.

No-mocks (PA-306): real on-disk account store; HTTP is injected at
``urllib.request.urlopen`` (the production seam), matching the sibling
``test__account_refresh_pinned_running`` shape.

AAA marker comments; one assertion per test; >=3-word names.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account

_HOUR_MS = 3600 * 1000


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_REGISTRY_DIR")
    return home


def _seed_account(home: Path, name: str, *, expires_in_hours: float) -> Path:
    """Seed a stored account whose access_token expires in N hours."""
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "refreshToken": f"{name}-refresh",
                    "clientId": "cid",
                    "expiresAt": int(time.time() * 1000)
                    + int(expires_in_hours * _HOUR_MS),
                }
            }
        )
    )
    return creds


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return self._body


@pytest.fixture
def opener_swap() -> Iterator[dict]:
    """Swap ``urllib.request.urlopen`` at the production HTTP boundary."""
    import urllib.request

    state: dict[str, Any] = {"response": {"access_token": "NEW", "expires_in": 3600}}
    saved = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        resp = state["response"]
        if isinstance(resp, Exception):
            raise resp
        return _FakeResp(json.dumps(resp).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        yield state
    finally:
        urllib.request.urlopen = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Defect 2: the still-fresh (skipped) branch must not crash
# ---------------------------------------------------------------------------


def test_still_fresh_account_does_not_raise(sandbox_home, opener_swap) -> None:
    # Arrange — one account with plenty of TTL left, so --all takes the
    # rotate-only-when-stale "skip" branch that formats expires_at.
    _seed_account(sandbox_home, "alpha", expires_in_hours=24)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all"])
    # Assert — pre-fix this was TypeError from _iso_ms(tz=timezone).
    assert result.exception is None


def test_still_fresh_account_token_left_untouched(sandbox_home, opener_swap) -> None:
    # Arrange — a fresh token must never be needlessly rotated (its
    # refresh_token is single-use).
    creds = _seed_account(sandbox_home, "alpha", expires_in_hours=24)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert
    written = json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "OLD-ACCESS"


def test_fresh_account_does_not_abort_later_stale_refresh(
    sandbox_home, opener_swap
) -> None:
    # Arrange — "alpha" is fresh and sorts BEFORE the stale "beta". The
    # uncaught _iso_ms TypeError on alpha aborted the whole --all loop,
    # so beta was never refreshed. This is the outage's real mechanism.
    _seed_account(sandbox_home, "alpha", expires_in_hours=24)
    creds_beta = _seed_account(sandbox_home, "beta", expires_in_hours=0.1)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert — beta got rotated despite the fresh account ahead of it.
    written = json.loads(creds_beta.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


# ---------------------------------------------------------------------------
# Defect 1: --include-active must refresh the host-active account
# ---------------------------------------------------------------------------


def test_include_active_refreshes_the_active_account(
    sandbox_home, opener_swap
) -> None:
    # Arrange — "alpha" is the host's ~/.claude active login and is stale.
    # --skip-active would exclude it; --include-active must refresh it.
    creds = _seed_account(sandbox_home, "alpha", expires_in_hours=0.1)
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "alpha@x"}})
    )
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — the account the whole fleet is pinned to gets a fresh token.
    written = json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "NEW"


def test_skip_active_still_excludes_the_active_account(
    sandbox_home, opener_swap
) -> None:
    # Arrange — the opposite intent must keep working (the flag itself is
    # not being removed, only demoted from the timer's default).
    creds = _seed_account(sandbox_home, "alpha", expires_in_hours=0.1)
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "alpha@x"}})
    )
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--skip-active"])
    # Assert
    written = json.loads(creds.read_text())["claudeAiOauth"]["accessToken"]
    assert written == "OLD-ACCESS"


def test_include_active_conflicts_with_skip_active(sandbox_home, opener_swap) -> None:
    # Arrange — opposite intents; the CLI must fail loud, not silently
    # pick one.
    _seed_account(sandbox_home, "alpha", expires_in_hours=0.1)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        account, ["refresh", "--all", "--include-active", "--skip-active"]
    )
    # Assert
    assert result.exit_code == 2


def test_include_active_announces_intent_on_stderr(sandbox_home, opener_swap) -> None:
    # Arrange — the timer's intent must be legible in the journal.
    _seed_account(sandbox_home, "alpha", expires_in_hours=0.1)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--include-active"])
    # Assert — click >=8.2 separates stderr; <8.2 merges it. Tolerate both.
    stderr_text = getattr(result, "stderr", "") or ""
    all_out = (result.output or "") + stderr_text
    assert "include-active" in all_out
