"""Tests for ``sac account quota`` (agent self-awareness, #16 PART 4).

Reads ``$CLAUDE_AGENT_ACCOUNT`` + the bound quota-cache.json and prints
THIS agent's own account + live quota numbers — for use inside a Claude
turn so the agent can see "am I about to hit the 5h cap" without going
through the listen daemon.

No mocks. The quota-cache reader is exercised via a tmp fixture file
pointed at by ``$SAC_QUOTA_CACHE_PATH``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.account_group import account

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_cache(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "quota-cache.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


SAMPLE = {
    "written_at": 1.0,
    "accounts": {
        "wyusuuke@gmail.com": {
            "short": "wyusuuke",
            "h5": 17.0,
            "d7": 3.0,
            "ttl_h": 7.74,
        }
    },
}


@pytest.fixture(autouse=True)
def _clean_env(env_save_restore) -> None:
    env_save_restore.delete("CLAUDE_AGENT_ACCOUNT")
    env_save_restore.delete("SAC_QUOTA_CACHE_PATH")


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_account_quota_human_emits_compact_line(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(_write_cache(tmp_path, SAMPLE)))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "wyusuuke-gmail-com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota"])
    # Assert — exact format documented in the docstring (compact, eye-parseable).
    assert (
        result.output.strip() == "account=wyusuuke 5h=17 percent 7d=3 percent ttl=7.74h"
    )


def test_account_quota_human_exits_zero_on_success(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(_write_cache(tmp_path, SAMPLE)))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "wyusuuke-gmail-com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota"])
    # Assert
    assert result.exit_code == 0


def test_account_quota_json_emits_deterministic_shape(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(_write_cache(tmp_path, SAMPLE)))
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "wyusuuke-gmail-com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota", "--json"])
    payload = json.loads(result.output)
    # Assert — exact keys the in-agent consumer can rely on.
    assert payload == {
        "account": "wyusuuke",
        "used_pct_5h": 17.0,
        "used_pct_7d": 3.0,
        "token_ttl_hours": 7.74,
    }


_ROUNDING_PAYLOAD = {
    "accounts": {
        "x@y.z": {
            "short": "wyusuuke",
            "h5": 8.6,
            "d7": 2.4,
            "ttl_h": 1.0,
        }
    }
}


@pytest.mark.parametrize(
    "expected_fragment",
    ["5h=9 percent", "7d=2 percent"],
)
def test_account_quota_human_rounds_percentage_field(
    tmp_path: Path,
    env_save_restore,
    expected_fragment: str,
) -> None:
    # Arrange — non-integer percentages must round (matches the
    # telegrammer signature shape so an agent and its bridge tell the
    # same story). 8.6 → 9, 2.4 → 2.
    env_save_restore.set(
        "SAC_QUOTA_CACHE_PATH", str(_write_cache(tmp_path, _ROUNDING_PAYLOAD))
    )
    env_save_restore.set("CLAUDE_AGENT_ACCOUNT", "wyusuuke-gmail-com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota"])
    # Assert
    assert expected_fragment in result.output


# ---------------------------------------------------------------------------
# Unavailable + --strict behaviour
# ---------------------------------------------------------------------------


def test_account_quota_human_prints_unavailable_without_account_env(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — cache present, but no account env.
    env_save_restore.set("SAC_QUOTA_CACHE_PATH", str(_write_cache(tmp_path, SAMPLE)))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota"])
    # Assert
    assert result.output.strip() == "unavailable"


def test_account_quota_human_exits_zero_by_default_when_unavailable() -> None:
    # Arrange — operator contract: a missing cache must NOT abort a
    # Claude turn pipelining `sac account quota` for soft routing.
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota"])
    # Assert
    assert result.exit_code == 0


def test_account_quota_strict_exits_nonzero_when_unavailable() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota", "--strict"])
    # Assert
    assert result.exit_code == 1


def test_account_quota_json_emits_null_when_unavailable() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota", "--json"])
    # Assert — JSON consumers can branch on `if quota is None:`.
    assert result.output.strip() == "null"


def test_account_quota_json_exits_zero_by_default_when_unavailable() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota", "--json"])
    # Assert
    assert result.exit_code == 0


def test_account_quota_json_strict_exits_nonzero_when_unavailable() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["quota", "--json", "--strict"])
    # Assert
    assert result.exit_code == 1
