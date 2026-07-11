"""Deterministic CCT bot-token injection from the fleet pool.

Covers ``_cct_token_pool.ensure_cct_bot_token`` (card
``sac-fleet-ux-misc-2026-06-24``, last item): when a spec requests
``server:claude-code-telegrammer``, sac resolves the agent/project →
``CCT_BOT_TOKEN_<SLOT>`` from the pool (launching env + ``SAC_SECRETS_ENVRC``
secret files) and appends the token to the agent's materialised ``.env`` —
never leaving it to per-project ``.envrc`` goodwill. Real temp-dir pools +
real bash sourcing — no mocks (PA-306). STX-TQ002 AAA markers, STX-TQ007
one assert per test.

Named ``test__cct_token_pool.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_token_pool.py``. Slot names use a
``ZZ_``-prefixed namespace so an operator shell's real pool vars can never
collide with the fixtures.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import scitex_logging

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_token_pool import (
    _slot_candidates,
    ensure_cct_bot_token,
)

_SECRETS_VAR = "SAC_SECRETS_ENVRC"
_CHANNEL = "server:claude-code-telegrammer"


@pytest.fixture
def secrets_envrc() -> Iterator[None]:
    """Save/restore ``SAC_SECRETS_ENVRC`` so a test may set it freely."""
    saved = os.environ.get(_SECRETS_VAR)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(_SECRETS_VAR, None)
        else:
            os.environ[_SECRETS_VAR] = saved


def _cfg(
    name: str,
    *,
    workdir: str = "",
    channel: bool = True,
    env: dict[str, str] | None = None,
) -> AgentConfig:
    """Real AgentConfig with the telegrammer channel requested by default."""
    cfg = AgentConfig(name=name)
    cfg.workdir = workdir
    if channel:
        cfg.claude.channels = [_CHANNEL]
    if env:
        cfg.env = dict(env)
    return cfg


def _pool_file(tmp_path: Path, lines: str) -> None:
    """Write a REAL temp secrets pool and point ``SAC_SECRETS_ENVRC`` at it."""
    pool = tmp_path / "pool.src"
    pool.write_text(lines, encoding="utf-8")
    os.environ[_SECRETS_VAR] = str(pool)


# ---------------------------------------------------------------------------
# gating + explicit-mapping precedence
# ---------------------------------------------------------------------------


def test_noop_when_channel_not_requested(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — pool has a matching slot, but the spec does not request the channel.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_NOCHAN=tok-zz\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-nochan", channel=False), dest)
    # Assert — no .env is created for an agent that never asked for telegram.
    assert not (dest / ".env").exists()


def test_existing_token_left_untouched(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the .envrc cascade already provided a token; pool has another.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_KEEP=tok-pool\n")
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / ".env").write_text(
        "CCT_AGENT_ID=hand\nCCT_BOT_TOKEN=tok-hand\n", encoding="utf-8"
    )
    # Act
    ensure_cct_bot_token(_cfg("zz-keep"), dest)
    # Assert — the hand-authored mapping stays authoritative.
    assert "CCT_BOT_TOKEN=tok-hand" in (dest / ".env").read_text()


def test_existing_token_backfills_agent_id(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — token provided but no identity; workdir names the project.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / ".env").write_text("CCT_BOT_TOKEN=tok-hand\n", encoding="utf-8")
    workdir = tmp_path / "proj" / "zz-proj"
    # Act
    ensure_cct_bot_token(_cfg("zz-agent", workdir=str(workdir)), dest)
    # Assert — identity defaults to the project (workdir basename).
    assert "CCT_AGENT_ID=zz-proj" in (dest / ".env").read_text()


# ---------------------------------------------------------------------------
# pool resolution
# ---------------------------------------------------------------------------


def test_resolves_from_pool_via_agent_name(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a real temp pool holds the agent-name slot.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_POOL_AGENT=tok-zz\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-pool-agent"), dest)
    # Assert
    assert "CCT_BOT_TOKEN=tok-zz" in (dest / ".env").read_text()


def test_workdir_basename_slot_wins_over_agent_name(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — pool has BOTH slots; the bot is per-PROJECT so workdir wins.
    _pool_file(
        tmp_path,
        "export CCT_BOT_TOKEN_ZZ_PROJ=tok-proj\n"
        "export CCT_BOT_TOKEN_ZZ_AGENT=tok-agent\n",
    )
    dest = tmp_path / "home"
    dest.mkdir()
    workdir = tmp_path / "proj" / "zz-proj"
    # Act
    ensure_cct_bot_token(_cfg("zz-agent", workdir=str(workdir)), dest)
    # Assert
    assert "CCT_BOT_TOKEN=tok-proj" in (dest / ".env").read_text()


def test_strips_scitex_prefix_candidate(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — pool names the core package by its short slot (scitex- stripped).
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZEXAMPLE=tok-short\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("scitex-zzexample"), dest)
    # Assert
    assert "CCT_BOT_TOKEN=tok-short" in (dest / ".env").read_text()


def test_explicit_slot_override_wins(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — spec.apptainer.env names a slot; the mechanical slot also exists.
    _pool_file(
        tmp_path,
        "export CCT_BOT_TOKEN_ZZ_CUSTOM=tok-custom\n"
        "export CCT_BOT_TOKEN_ZZ_MECH=tok-mech\n",
    )
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(
        _cfg("zz-mech", env={"CCT_BOT_TOKEN_SLOT": "ZZ_CUSTOM"}), dest
    )
    # Assert
    assert "CCT_BOT_TOKEN=tok-custom" in (dest / ".env").read_text()


def test_override_miss_does_not_fall_back_mechanically(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the override names a GHOST slot while the mechanical slot exists;
    # a typo must fail loud, not silently bind another project's bot.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_MECH=tok-mech\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(
        _cfg("zz-mech", env={"CCT_BOT_TOKEN_SLOT": "ZZ_GHOST"}), dest
    )
    # Assert — no token written at all.
    assert not (dest / ".env").exists()


def test_empty_pool_value_is_treated_as_missing(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the slot exists but resolves empty (unset upstream secret).
    _pool_file(tmp_path, 'export CCT_BOT_TOKEN_ZZ_EMPTY=""\n')
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-empty"), dest)
    # Assert
    assert not (dest / ".env").exists()


# ---------------------------------------------------------------------------
# injected .env contents
# ---------------------------------------------------------------------------


def test_injection_sets_default_agent_id_from_workdir(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_IDPROJ=tok-id\n")
    dest = tmp_path / "home"
    dest.mkdir()
    workdir = tmp_path / "proj" / "zz-idproj"
    # Act
    ensure_cct_bot_token(_cfg("zz-idagent", workdir=str(workdir)), dest)
    # Assert
    assert "CCT_AGENT_ID=zz-idproj" in (dest / ".env").read_text()


def test_injection_preserves_other_env_lines(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the fold already materialised unrelated vars.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_PRESERVE=tok-p\n")
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / ".env").write_text("OTHER_VAR=1\n", encoding="utf-8")
    # Act
    ensure_cct_bot_token(_cfg("zz-preserve"), dest)
    # Assert
    assert "OTHER_VAR=1" in (dest / ".env").read_text()


def test_injected_env_file_is_owner_only(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_PERMS=tok-perm\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-perms"), dest)
    # Assert — the token-bearing .env is chmod 0600.
    assert (dest / ".env").stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------------------
# fail-loud + redaction discipline
# ---------------------------------------------------------------------------


def test_missing_token_logs_error(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — channel requested, no pool anywhere.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert — LOUD error names the agent (never silent absence).
    assert "zz-missing-fixture" in caplog.text


def test_missing_token_error_names_pool_source(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert — the operator is told WHERE the pool lives.
    assert "SAC_SECRETS_ENVRC" in caplog.text


def test_missing_token_does_not_raise_or_write(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — telegram is a comms rail, not a boot dependency.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act — must not raise.
    ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert — and must not fabricate an .env either.
    assert not (dest / ".env").exists()


def test_token_value_never_logged(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — a distinctive token value that must never reach any log line.
    _pool_file(
        tmp_path, "export CCT_BOT_TOKEN_ZZ_REDACT=tok-NEVER-LOG-9x7\n"
    )
    dest = tmp_path / "home"
    dest.mkdir()
    # Act — capture EVERYTHING down to DEBUG.
    with caplog.at_level(scitex_logging.DEBUG):
        ensure_cct_bot_token(_cfg("zz-redact"), dest)
    # Assert — the value appears only in the 0600 .env, never in logs.
    assert "tok-NEVER-LOG-9x7" not in caplog.text


def test_resolution_logs_slot_name_not_value(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_SLOTLOG=tok-slot\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.INFO):
        ensure_cct_bot_token(_cfg("zz-slotlog"), dest)
    # Assert — the log names the SLOT so the operator can audit the mapping.
    assert "CCT_BOT_TOKEN_ZZ_SLOTLOG" in caplog.text


# ---------------------------------------------------------------------------
# slot-candidate derivation (pure helper)
# ---------------------------------------------------------------------------


def test_slot_candidates_workdir_first_then_name() -> None:
    # Arrange
    # Act
    candidates = _slot_candidates("zz-agent", "/tmp/proj/zz-proj")
    # Assert
    assert candidates == ["ZZ_PROJ", "ZZ_AGENT"]


def test_slot_candidates_add_scitex_stripped_form() -> None:
    # Arrange
    # Act
    candidates = _slot_candidates("scitex-todo", "")
    # Assert
    assert candidates == ["SCITEX_TODO", "TODO"]


def test_slot_candidates_dedupe_same_project_and_name() -> None:
    # Arrange — agent named after its project dir must not double-try.
    # Act
    candidates = _slot_candidates("zz-same", "/tmp/proj/zz-same")
    # Assert
    assert candidates == ["ZZ_SAME"]


# ---------------------------------------------------------------------------
# deploy_to_home integration — the wiring point actually runs
# ---------------------------------------------------------------------------


def test_deploy_to_home_injects_pool_token_end_to_end(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — a real spec dir with a to_home layer, a real temp pool, and a
    # spec requesting the telegrammer channel but shipping NO .envrc at all
    # (the exact gap this feature closes).
    from scitex_agent_container.runtimes._to_home import deploy_to_home

    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_E2E=tok-e2e\n")
    agent_dir = tmp_path / "agents" / "zz-e2e"
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True)
    (to_home / "marker.txt").write_text("hi\n", encoding="utf-8")
    dest = tmp_path / "workspace-home"
    cfg = _cfg("zz-e2e", workdir=str(tmp_path / "proj" / "zz-e2e"))
    cfg.config_path = str(agent_dir / "spec.yaml")
    cfg.to_home = str(to_home)
    # Act
    deploy_to_home(cfg, str(dest))
    # Assert — the materialised home carries the pool-resolved token.
    assert "CCT_BOT_TOKEN=tok-e2e" in (dest / ".env").read_text()
