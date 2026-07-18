"""Tests for ``sac accounts refresh --push-to <peer>``.

The gap: the host-side ``sac.accounts-refresh`` timer is the SOLE refresher
under the single-refresher model, but that only reaches agents sharing this
machine's filesystem. A peer (Spartan) is a different box, so the copy of
the snapshot its agents bind is refreshed by NOTHING and silently 401s
within one access-token lifetime. ``--push-to`` copies each freshly-rotated
snapshot to the peer's identical absolute path.

No-mocks (PA-306 / STX-NM002). Real on-disk credentials under a real
(redirected) ``$HOME``; a real ``config.yaml`` pinned with
``$SCITEX_AGENT_CONTAINER_CONFIG`` (the same file ``sac host list`` reads);
the OAuth HTTP call injected at the ``urllib.request.urlopen`` production
boundary; and the push driven through the REAL ssh transport, with only the
network hop replaced by the ``ssh_exec_shim`` helper (a real ``ssh`` on
``$PATH`` that runs the remote command locally). An UNREACHABLE peer is
modelled with the repo's ``subprocess_shim`` — a real ``ssh`` binary that
exits 255, exactly as a real one does when the host is unreachable.

AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
import stat as stat_mod
from pathlib import Path
from typing import Any, Iterator

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_refresh_push import refreshed_accounts
from scitex_agent_container.cli_pkg.account_group import account

_FUTURE_MS = 9_999_999_999_000  # far-future expiry (skipped: plenty of TTL)
_PAST_MS = 1_000_000_000_000  # 2001 — long expired (refreshed)

# Distinctive stand-ins for token material; the no-leak test greps for these.
_NEW_ACCESS = "NEW-ACCESS-SECRET-XYZ"
_NEW_REFRESH = "NEW-REFRESH-SECRET-XYZ"


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so ``Path.home()`` lands inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


@pytest.fixture
def peer_config(tmp_path, env_save_restore) -> Path:
    """Write a real config.yaml and pin sac's peer lookup at it."""
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers:\n  spartan:\n    ssh: ywatanabe@spartan-login1\n")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
    return cfg


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
    """Swap ``urllib.request.urlopen`` at the production boundary."""
    import urllib.request

    state: dict[str, Any] = {
        "response": {
            "access_token": _NEW_ACCESS,
            "refresh_token": _NEW_REFRESH,
            "expires_in": 3600,
        }
    }
    saved = urllib.request.urlopen

    def fake_urlopen(req, timeout=None):
        return _FakeResp(json.dumps(state["response"]).encode())

    urllib.request.urlopen = fake_urlopen  # type: ignore[assignment]
    try:
        yield state
    finally:
        urllib.request.urlopen = saved  # type: ignore[assignment]


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _seed_account(home: Path, name: str, *, expires_ms: int) -> Path:
    """Create a stored account with a real credentials snapshot on disk."""
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    creds = _store_dir(home) / name / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "OLD-ACCESS",
                    "refreshToken": "OLD-REFRESH",
                    "clientId": "cid",
                    "expiresAt": expires_ms,
                }
            }
        )
    )
    return creds


def _mode_of(path: Path) -> str:
    return oct(stat_mod.S_IMODE(path.stat().st_mode))[2:]


def _access_token_at(path: Path) -> str:
    return json.loads(path.read_text())["claudeAiOauth"]["accessToken"]


# ---------------------------------------------------------------------------
# Only the accounts that ACTUALLY rotated are pushed
# ---------------------------------------------------------------------------


def test_refreshed_accounts_includes_a_rotated_account() -> None:
    # Arrange
    results = [{"name": "work", "success": True, "credentials_path": "/x"}]
    # Act
    selected = refreshed_accounts(results)
    # Assert
    assert [r["name"] for r in selected] == ["work"]


def test_refreshed_accounts_excludes_a_skipped_fresh_account() -> None:
    # Arrange — the refresher runs on a short cadence and mostly skips.
    # Pushing a skipped account would turn that cadence into a copy storm.
    results = [
        {"name": "fresh", "success": None, "skipped": True, "credentials_path": "/x"}
    ]
    # Act
    selected = refreshed_accounts(results)
    # Assert
    assert selected == []


def test_refreshed_accounts_excludes_a_failed_account() -> None:
    # Arrange — a failed refresh must never overwrite the peer's copy.
    results = [
        {"name": "dead", "success": False, "skipped": False, "credentials_path": "/x"}
    ]
    # Act
    selected = refreshed_accounts(results)
    # Assert
    assert selected == []


# ---------------------------------------------------------------------------
# End-to-end: refresh, then push to the peer
# ---------------------------------------------------------------------------


def test_push_to_exits_zero_on_success(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert
    assert result.exit_code == 0


def test_push_to_lands_the_snapshot_at_mode_0600(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — the REAL mode of the REAL file the push landed on the peer.
    assert _mode_of(creds) == "600"


def test_push_to_delivers_the_rotated_token(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — the peer received the FRESH token, not the pre-refresh one.
    assert _access_token_at(creds) == _NEW_ACCESS


def test_push_to_reports_the_peer_and_the_remote_path(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert
    assert f"pushed -> spartan:{creds}" in result.output


def test_push_to_records_the_peer_in_json_output(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(
        account, ["refresh", "--all", "--push-to", "spartan", "--json"]
    )
    # Assert
    assert json.loads(result.stdout)[0]["pushed_to"] == "spartan"


def test_skipped_fresh_account_is_not_pushed(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange — nothing rotates, so nothing should be copied to the peer.
    _seed_account(sandbox_home, "fresh", expires_ms=_FUTURE_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — the push never even reached ssh.
    assert ssh_exec_shim.invocations() == []


def test_skipped_fresh_account_says_nothing_to_push(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "fresh", expires_ms=_FUTURE_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert
    assert "no account rotated this run" in result.output


def test_push_to_does_not_leak_token_values(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — paths and account names only; never token material.
    assert _NEW_ACCESS not in result.output and _NEW_REFRESH not in result.output


# ---------------------------------------------------------------------------
# Unknown peer — rejected BEFORE a single-use refresh_token is spent
# ---------------------------------------------------------------------------


def test_unknown_peer_exits_two(sandbox_home, peer_config, opener_swap) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "nope"])
    # Assert
    assert result.exit_code == 2


def test_unknown_peer_does_not_spend_a_refresh_token(
    sandbox_home, peer_config, opener_swap
) -> None:
    # Arrange — the refresh_token is SINGLE-USE; a typo must not burn it.
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all", "--push-to", "nope"])
    # Assert — the peer was validated first, so nothing rotated.
    assert _access_token_at(creds) == "OLD-ACCESS"


# ---------------------------------------------------------------------------
# Fail loud — an unreachable peer fails the RUN, never a silent success
# ---------------------------------------------------------------------------


def _unreachable_peer(subprocess_shim) -> None:
    """Install a real ``ssh`` that fails exactly as an unreachable host does."""
    subprocess_shim.install(
        "ssh",
        exit=255,
        stderr="ssh: connect to host spartan-login1 port 22: No route to host",
    )


def test_unreachable_peer_fails_the_run(
    sandbox_home, peer_config, opener_swap, subprocess_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    _unreachable_peer(subprocess_shim)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — a silent success here would recreate the invisible staleness.
    assert result.exit_code != 0


def test_unreachable_peer_names_the_peer(
    sandbox_home, peer_config, opener_swap, subprocess_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    _unreachable_peer(subprocess_shim)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert
    assert "PUSH FAILED -> spartan" in result.output


def test_unreachable_peer_names_the_path(
    sandbox_home, peer_config, opener_swap, subprocess_shim
) -> None:
    # Arrange
    creds = _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    _unreachable_peer(subprocess_shim)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert
    assert str(creds.parent) in result.output


def test_unreachable_peer_does_not_leak_token_values(
    sandbox_home, peer_config, opener_swap, subprocess_shim
) -> None:
    # Arrange
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    _unreachable_peer(subprocess_shim)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["refresh", "--all", "--push-to", "spartan"])
    # Assert — even the failure path renders paths only.
    assert _NEW_ACCESS not in result.output and _NEW_REFRESH not in result.output


def test_refresh_without_push_to_never_touches_a_peer(
    sandbox_home, peer_config, opener_swap, ssh_exec_shim
) -> None:
    # Arrange — the flag is OPT-IN; the default must behave exactly as before.
    _seed_account(sandbox_home, "stale", expires_ms=_PAST_MS)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["refresh", "--all"])
    # Assert
    assert ssh_exec_shim.invocations() == []
