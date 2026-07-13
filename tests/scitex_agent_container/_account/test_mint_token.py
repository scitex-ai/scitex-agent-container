"""Tests for ``sac accounts mint-token`` — access-only credential minting.

Security-critical invariant under test: the ``refreshToken`` NEVER appears
in the minted artifact (that is the whole point of the access-only
scheme). Fixtures use synthetic token strings — ``oat-test-access`` (the
distributable, allowed to leave) and ``oat-test-refresh`` (the sentinel
that must NEVER leak). No network, no real tokens, no monkeypatching:
the pure function takes ``store_dir`` / ``home`` / ``now`` / ``hostname``
as real injectable parameters, and the CLI is driven against a real
temp store routed via ``$SCITEX_DIR``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._account.mint_token import (
    MintError,
    mint_access_only_artifact,
)

_ACCESS = "oat-test-access"
_REFRESH_SENTINEL = "oat-test-refresh"
_SCOPES = ["user:inference", "user:profile"]
_LABEL = "wyusuuke-gmail-com"


def _write_account(
    store: Path,
    label: str,
    *,
    expires_at_ms: int,
) -> None:
    """Materialise a synthetic stored account under ``store/<label>/``."""
    acct = store / label
    acct.mkdir(parents=True, exist_ok=True)
    creds = {
        "claudeAiOauth": {
            "accessToken": _ACCESS,
            "refreshToken": _REFRESH_SENTINEL,
            "expiresAt": expires_at_ms,
            "scopes": list(_SCOPES),
        }
    }
    (acct / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")
    (acct / "account.json").write_text(
        json.dumps({"name": label, "email_address": f"{label}@example.com"}),
        encoding="utf-8",
    )


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "accounts"
    d.mkdir()
    return d


@pytest.fixture
def healthy_env(store, tmp_path):
    """A minted envelope for a healthy account, plus the expiry it used."""
    now_s = time.time()
    future_ms = int((now_s + 3600) * 1000)
    _write_account(store, _LABEL, expires_at_ms=future_ms)
    env = mint_access_only_artifact(
        _LABEL,
        store_dir=store,
        home=tmp_path,
        hostname="master-host-1",
        now=now_s,
    )
    return env, future_ms


# ---------------------------------------------------------------------------
# Core function — healthy account emits the correct envelope shape
# ---------------------------------------------------------------------------


def test_mint_healthy_carries_access_token(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    oauth = env["artifact"]["claudeAiOauth"]
    # Assert
    assert oauth["accessToken"] == _ACCESS


def test_mint_healthy_carries_expires_at(healthy_env):
    # Arrange
    env, future_ms = healthy_env
    # Act
    oauth = env["artifact"]["claudeAiOauth"]
    # Assert
    assert oauth["expiresAt"] == future_ms


def test_mint_healthy_carries_scopes(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    oauth = env["artifact"]["claudeAiOauth"]
    # Assert
    assert oauth["scopes"] == _SCOPES


def test_mint_healthy_meta_account(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert meta["account"] == _LABEL


def test_mint_healthy_meta_artifact_kind(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert meta["artifact"] == "access-only"


def test_mint_healthy_meta_artifact_version(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert meta["artifact_version"] == 1


def test_mint_healthy_meta_master_host(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert meta["master_host"] == "master-host-1"


def test_mint_healthy_meta_expires_at(healthy_env):
    # Arrange
    env, future_ms = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert meta["expires_at"] == future_ms


def test_mint_healthy_meta_minted_at_is_int(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    meta = env["meta"]
    # Assert
    assert isinstance(meta["minted_at"], int)


# ---------------------------------------------------------------------------
# Core function — SECURITY: refreshToken never leaves
# ---------------------------------------------------------------------------


def test_mint_healthy_omits_refresh_token_key(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    oauth = env["artifact"]["claudeAiOauth"]
    # Assert
    assert "refreshToken" not in oauth


def test_mint_healthy_never_leaks_refresh_sentinel(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    blob = json.dumps(env)
    # Assert
    assert _REFRESH_SENTINEL not in blob


def test_mint_healthy_access_token_present_in_blob(healthy_env):
    # Arrange
    env, _future = healthy_env
    # Act
    blob = json.dumps(env)
    # Assert
    assert _ACCESS in blob


# ---------------------------------------------------------------------------
# Core function — health gate + unknown label fail loudly
# ---------------------------------------------------------------------------


def test_mint_expired_account_raises(store, tmp_path):
    # Arrange
    now_s = time.time()
    _write_account(store, "stale", expires_at_ms=int((now_s - 3600) * 1000))

    def _call():
        mint_access_only_artifact(
            "stale", store_dir=store, home=tmp_path, now=now_s
        )

    # Act
    action = _call
    # Assert
    with pytest.raises(MintError, match="unhealthy"):
        action()


def test_mint_unknown_label_raises(store, tmp_path):
    # Arrange
    now_s = time.time()
    _write_account(store, "known-one", expires_at_ms=int((now_s + 3600) * 1000))

    def _call():
        mint_access_only_artifact(
            "does-not-exist", store_dir=store, home=tmp_path, now=now_s
        )

    # Act
    action = _call
    # Assert
    with pytest.raises(MintError, match="unknown account"):
        action()


def test_mint_unknown_label_lists_available(store, tmp_path):
    # Arrange
    now_s = time.time()
    _write_account(store, "known-one", expires_at_ms=int((now_s + 3600) * 1000))

    def _call():
        mint_access_only_artifact(
            "does-not-exist", store_dir=store, home=tmp_path, now=now_s
        )

    # Act
    action = _call
    # Assert
    with pytest.raises(MintError, match="known-one"):
        action()


# ---------------------------------------------------------------------------
# CLI wrapper — driven against a real temp store via $SCITEX_DIR
# ---------------------------------------------------------------------------


@pytest.fixture
def sac_store(tmp_path):
    """Route the store to a temp dir via $SCITEX_DIR (yield-based, restores)."""
    prev = os.environ.get("SCITEX_DIR")
    os.environ["SCITEX_DIR"] = str(tmp_path / "sac")
    accounts = tmp_path / "sac" / "agent-container" / "accounts"
    accounts.mkdir(parents=True)
    yield accounts
    if prev is None:
        os.environ.pop("SCITEX_DIR", None)
    else:
        os.environ["SCITEX_DIR"] = prev


def _invoke_mint():
    from scitex_agent_container.cli_pkg.account_group import account

    runner = CliRunner()
    # isolated_filesystem gives a git-less cwd so the local-state cascade
    # falls back to $SCITEX_DIR instead of any project-scope store.
    with runner.isolated_filesystem():
        return runner.invoke(account, ["mint-token", "--account", _LABEL])


def test_cli_healthy_exit_zero(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    result = _invoke_mint()
    # Assert
    assert result.exit_code == 0, result.output


def test_cli_healthy_stdout_is_access_only(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    payload = json.loads(_invoke_mint().output)
    # Assert
    assert payload["meta"]["artifact"] == "access-only"


def test_cli_healthy_stdout_has_access_token(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    payload = json.loads(_invoke_mint().output)
    # Assert
    assert payload["artifact"]["claudeAiOauth"]["accessToken"] == _ACCESS


def test_cli_healthy_master_host_present(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    payload = json.loads(_invoke_mint().output)
    # Assert
    assert isinstance(payload["meta"]["master_host"], str)


def test_cli_healthy_stdout_omits_refresh_sentinel(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    output = _invoke_mint().output
    # Assert
    assert _REFRESH_SENTINEL not in output


def test_cli_expired_exit_nonzero(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s - 60) * 1000))
    # Act
    result = _invoke_mint()
    # Assert
    assert result.exit_code != 0


def test_cli_expired_prints_no_artifact(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, _LABEL, expires_at_ms=int((now_s - 60) * 1000))
    # Act
    output = _invoke_mint().output
    # Assert
    assert "claudeAiOauth" not in output


def test_cli_unknown_label_exit_nonzero(sac_store):
    # Arrange
    now_s = time.time()
    _write_account(sac_store, "other", expires_at_ms=int((now_s + 3600) * 1000))
    # Act
    result = _invoke_mint()
    # Assert
    assert result.exit_code != 0
