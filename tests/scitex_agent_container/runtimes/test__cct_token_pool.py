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

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
import scitex_logging

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_token_pool import (
    _default_agent_id,
    _slot_candidates,
    ensure_cct_bot_token,
    prune_tokenless_telegrammer_mcp,
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


def test_noop_when_channel_not_requested(tmp_path: Path, secrets_envrc: None) -> None:
    # Arrange — pool has a matching slot, but the spec does not request the channel.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_NOCHAN=tok-zz\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-nochan", channel=False), dest)
    # Assert — no .env is created for an agent that never asked for telegram.
    assert not (dest / ".env").exists()


def test_existing_token_left_untouched(tmp_path: Path, secrets_envrc: None) -> None:
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


def test_existing_token_backfills_the_agents_own_name_as_identity(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — token provided but no identity; the agent works in a project
    # whose name differs from its own (the scitex-cards-in-scitex-todo shape).
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    (dest / ".env").write_text("CCT_BOT_TOKEN=tok-hand\n", encoding="utf-8")
    workdir = tmp_path / "proj" / "zz-proj"
    # Act
    ensure_cct_bot_token(_cfg("zz-agent", workdir=str(workdir)), dest)
    # Assert — identity is the AGENT, never the directory it stands in.
    # This test asserted `CCT_AGENT_ID=zz-proj` until 2026-07-17: it encoded the
    # bug as the contract, and would have blocked the fix for the incident it
    # described.
    assert "CCT_AGENT_ID=zz-agent" in (dest / ".env").read_text()


# ---------------------------------------------------------------------------
# pool resolution
# ---------------------------------------------------------------------------


def test_resolves_from_pool_via_agent_name(tmp_path: Path, secrets_envrc: None) -> None:
    # Arrange — a real temp pool holds the agent-name slot.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_POOL_AGENT=tok-zz\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-pool-agent"), dest)
    # Assert
    assert "CCT_BOT_TOKEN=tok-zz" in (dest / ".env").read_text()


def test_agent_name_slot_wins_over_the_project_it_works_in(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — pool holds BOTH slots. This is the live scitex-cards shape:
    # the project it works in HAS a registered bot; the agent is not its owner.
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
    # Assert — the agent gets ITS OWN bot, not the one belonging to the
    # directory. This asserted `tok-proj` until 2026-07-17, under the comment
    # "the bot is per-PROJECT so workdir wins" — i.e. the test demanded the
    # theft. It was written when a project had one agent and that made it true;
    # it did not stop being green when that stopped being true.
    assert "CCT_BOT_TOKEN=tok-agent" in (dest / ".env").read_text()


def test_strips_scitex_prefix_candidate(tmp_path: Path, secrets_envrc: None) -> None:
    # Arrange — pool names the core package by its short slot (scitex- stripped).
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZEXAMPLE=tok-short\n")
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("scitex-zzexample"), dest)
    # Assert
    assert "CCT_BOT_TOKEN=tok-short" in (dest / ".env").read_text()


def test_explicit_slot_override_wins(tmp_path: Path, secrets_envrc: None) -> None:
    # Arrange — spec.apptainer.env names a slot; the mechanical slot also exists.
    _pool_file(
        tmp_path,
        "export CCT_BOT_TOKEN_ZZ_CUSTOM=tok-custom\n"
        "export CCT_BOT_TOKEN_ZZ_MECH=tok-mech\n",
    )
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    ensure_cct_bot_token(_cfg("zz-mech", env={"CCT_BOT_TOKEN_SLOT": "ZZ_CUSTOM"}), dest)
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
    ensure_cct_bot_token(_cfg("zz-mech", env={"CCT_BOT_TOKEN_SLOT": "ZZ_GHOST"}), dest)
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
# caller-independent pool: the canonical $HOME default (class fix 2026-07-18)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_home(tmp_path: Path) -> Iterator[Path]:
    """Point ``$HOME`` at a real temp dir (save/restore) — no monkeypatch.

    So the canonical-default resolver globs THIS dir, never the operator's real
    ``~/.bash.d/secrets`` — the test stays deterministic on every host.
    """
    home = tmp_path / "home"
    home.mkdir()
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(home)
    try:
        yield home
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def test_pool_resolves_from_canonical_default_when_var_unset(
    tmp_path: Path, secrets_envrc: None, isolated_home: Path
) -> None:
    # Arrange — SAC_SECRETS_ENVRC UNSET (the cron / raw-ssh / federated-timer
    # case that stripped tokens tonight), but the operator's standardized default
    # pool file exists under $HOME. The class fix must resolve the token from
    # there instead of folding it EMPTY — the whole point of the fix.
    os.environ.pop(_SECRETS_VAR, None)
    pooldir = isolated_home / ".bash.d" / "secrets" / "010_scitex"
    pooldir.mkdir(parents=True)
    (pooldir / "01_cct.src").write_text(
        "export CCT_BOT_TOKEN_ZZ_DEFAULT=tok-default\n", encoding="utf-8"
    )
    dest = tmp_path / "workspace-home"
    dest.mkdir()
    # Act — no SAC_SECRETS_ENVRC set anywhere; only the default location has it.
    ensure_cct_bot_token(_cfg("zz-default"), dest)
    # Assert — the token is loaded from the canonical default pool, NOT stripped.
    assert "CCT_BOT_TOKEN=tok-default" in (dest / ".env").read_text()


# ---------------------------------------------------------------------------
# injected .env contents
# ---------------------------------------------------------------------------


def test_injection_sets_default_agent_id_from_the_agents_own_name(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — an agent working in a project it does not own.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_IDAGENT=tok-id\n")
    dest = tmp_path / "home"
    dest.mkdir()
    workdir = tmp_path / "proj" / "zz-idproj"
    # Act
    ensure_cct_bot_token(_cfg("zz-idagent", workdir=str(workdir)), dest)
    # Assert — asserted `zz-idproj` (the DIRECTORY) until 2026-07-17. The
    # identity half is the silent one: a stolen slot 409s until someone
    # notices, a stolen identity just writes under the wrong name.
    assert "CCT_AGENT_ID=zz-idagent" in (dest / ".env").read_text()


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


def test_injected_env_file_is_owner_only(tmp_path: Path, secrets_envrc: None) -> None:
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


def test_missing_token_logs_warning(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — channel requested, no pool anywhere.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.WARNING):
        ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert — LOUD warning names the agent (never silent absence).
    assert "zz-missing-fixture" in caplog.text


def test_missing_token_never_logs_at_error_level(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — a brand-new agent has no bot yet BY DEFINITION. Logging that
    # at ERROR made every fresh agent's boot log read like a startup failure
    # next to the genuinely fatal lines. It is a degraded comms rail, not a
    # dead boot: WARNING, never ERROR.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert
    assert not [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]


def test_missing_token_warning_says_the_agent_still_starts(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange — the message must SAY it is non-fatal, not merely be non-fatal.
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.WARNING):
        ensure_cct_bot_token(_cfg("zz-missing-fixture"), dest)
    # Assert
    assert "NOT a startup failure" in caplog.text


def test_missing_token_warning_names_pool_source(
    tmp_path: Path,
    secrets_envrc: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    os.environ.pop(_SECRETS_VAR, None)
    dest = tmp_path / "home"
    dest.mkdir()
    # Act
    with caplog.at_level(scitex_logging.WARNING):
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
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_REDACT=tok-NEVER-LOG-9x7\n")
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


def test_slot_candidates_come_from_the_agent_name_and_ignore_the_workdir() -> None:
    # Arrange
    # Act
    candidates = _slot_candidates("zz-agent", "/tmp/proj/zz-proj")
    # Assert — the workdir contributes NOTHING. This asserted
    # `["ZZ_PROJ", "ZZ_AGENT"]` until 2026-07-17: the project's slot first, the
    # agent's own second. A directory names a PROJECT, never an agent.
    assert candidates == ["ZZ_AGENT"]


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


# ---------------------------------------------------------------------------
# Identity is agent-scoped, never directory-scoped (INCIDENT 2026-07-17)
#
# A directory names a PROJECT, never an agent; the second agent in a repo must
# be a second AGENT, not the first one twice. Identity used to be derived from
# the workdir basename, so one repo = one identity = one agent, structurally.
# Three scitex-cards UI agents working in ~/proj/scitex-todo took the
# scitex-todo steward's identity and its bot.
# ---------------------------------------------------------------------------
_SHARED_REPO = "/home/ywatanabe/proj/scitex-todo"


def test_two_agents_in_one_repo_get_different_slots():
    # Arrange: the exact shape of the incident -- siblings sharing a workdir.
    # Act
    chat = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    gui = _slot_candidates("scitex-cards-gui", _SHARED_REPO)
    # Assert: the whole point. Under the old workdir-first rule both returned
    # TODO first and the second agent became the first.
    assert chat[0] != gui[0]


def test_two_agents_in_one_repo_get_different_identities():
    # Arrange
    # Act
    chat = _default_agent_id("scitex-cards-chat", _SHARED_REPO)
    gui = _default_agent_id("scitex-cards-gui", _SHARED_REPO)
    # Assert: the silent half. A stolen slot 409s loudly; a stolen identity just
    # writes under someone else's name.
    assert chat != gui


def test_an_agents_identity_is_its_own_name_even_inside_another_agents_repo():
    # Arrange: the steward of this repo is 'scitex-todo'; the worker is not.
    # Act
    identity = _default_agent_id("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert identity == "scitex-cards-chat"


def test_slot_never_includes_the_short_slot_of_the_project_worked_in():
    # Arrange: CCT_BOT_TOKEN_TODO exists in the live pool and CARDS does not, so
    # a workdir-first rule hands this agent the steward's REGISTERED bot.
    # Act
    candidates = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert "TODO" not in candidates


def test_slot_never_includes_the_long_slot_of_the_project_worked_in():
    # Arrange
    # Act
    candidates = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert "SCITEX_TODO" not in candidates


def test_agent_whose_name_matches_its_project_is_unaffected():
    # Arrange: 9 of 12 live agents look like this -- the rules agree, and the
    # fix must be a no-op for them.
    # Act
    candidates = _slot_candidates("scitex-dev", "/home/ywatanabe/proj/scitex-dev")
    # Assert
    assert candidates == ["SCITEX_DEV", "DEV"]


def test_scitex_prefix_still_yields_the_short_pool_slot():
    # Arrange: the pool names core packages by short slot (TODO, DEV, ...).
    # Act
    candidates = _slot_candidates("scitex-storage", "/anywhere/at/all")
    # Assert: no regression in the stripping behaviour the pool depends on.
    assert candidates == ["SCITEX_STORAGE", "STORAGE"]


def test_workdir_cannot_influence_the_slot_at_all():
    # Arrange: same agent, wildly different locations.
    # Act
    a = _slot_candidates("grant", "/home/ywatanabe/proj/grant")
    b = _slot_candidates("grant", "/home/ywatanabe/proj/scitex-todo")
    c = _slot_candidates("grant", "")
    # Assert: location is not an input to identity. Full stop.
    assert a == b == c


# ---------------------------------------------------------------------------
# prune_tokenless_telegrammer_mcp — card
# sac-omit-telegram-mcp-when-no-cct-bot-token-20260702.
#
# The shared baseline .mcp.json declares claude-code-telegrammer for EVERY
# agent. An agent with no bot therefore launches it with an empty token, cct
# refuses to start, and the operator's MCP panel carries a permanent "failed"
# row. No token -> no entry -> nothing to fail. Real files on tmp_path.
# ---------------------------------------------------------------------------

_TELEGRAMMER = "claude-code-telegrammer"


def _write_mcp_json(dest: Path, servers: dict) -> Path:
    mcp = dest / ".mcp.json"
    mcp.write_text(json.dumps({"mcpServers": servers}, indent=2) + "\n")
    return mcp


def _telegrammer_entry() -> dict:
    return {
        _TELEGRAMMER: {"command": "cct", "env": {"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"}}
    }


def _servers_in(mcp: Path) -> dict:
    return json.loads(mcp.read_text())["mcpServers"]


def test_tokenless_agent_loses_the_telegrammer_entry(tmp_path: Path) -> None:
    # Arrange — a materialised home with the entry and an env with NO token.
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("SOME_OTHER=1\n")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert _TELEGRAMMER not in _servers_in(mcp)


def test_tokened_agent_keeps_the_telegrammer_entry(tmp_path: Path) -> None:
    # Arrange — a real (non-empty) token in the materialised env.
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("CCT_BOT_TOKEN=123:abc\n")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert _TELEGRAMMER in _servers_in(mcp)


def test_empty_token_value_counts_as_no_token(tmp_path: Path) -> None:
    """An empty assignment is exactly the case cct fails loudly on."""
    # Arrange
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("CCT_BOT_TOKEN=\n")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert _TELEGRAMMER not in _servers_in(mcp)


def test_pruning_leaves_other_servers_untouched(tmp_path: Path) -> None:
    """Only the telegrammer entry may be removed."""
    # Arrange
    servers = {**_telegrammer_entry(), "scitex-cards": {"command": "scitex-cards"}}
    mcp = _write_mcp_json(tmp_path, servers)
    (tmp_path / ".env").write_text("")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert "scitex-cards" in _servers_in(mcp)


def test_prune_reports_whether_it_removed_the_entry(tmp_path: Path) -> None:
    # Arrange
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    removed = prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert removed is True


def test_missing_mcp_json_is_a_noop(tmp_path: Path) -> None:
    # Arrange — nothing materialised yet.
    (tmp_path / ".env").write_text("")
    # Act
    removed = prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert removed is False


def test_malformed_mcp_json_is_left_untouched(tmp_path: Path) -> None:
    """The .mcp.json deploy owns JSON fail-loud; pruning must not mask it."""
    # Arrange
    mcp = tmp_path / ".mcp.json"
    mcp.write_text("{not json")
    (tmp_path / ".env").write_text("")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert mcp.read_text() == "{not json"


def test_absent_telegrammer_entry_is_a_noop(tmp_path: Path) -> None:
    # Arrange — an agent whose config never declared it.
    _write_mcp_json(tmp_path, {"scitex-cards": {"command": "scitex-cards"}})
    (tmp_path / ".env").write_text("")
    # Act
    removed = prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert removed is False


# ---------------------------------------------------------------------------
# The prune's LEVEL splits on what the spec DECLARED — card
# sac-cct-prune-hides-misconfigured-telegram-agent-20260810.
#
# Removing the entry is right in every tokenless case (a server that cannot
# start is worse than an absent one), but removing it SILENTLY is how a
# misconfigured agent hides: four agents went mute AND deaf on a new host
# behind one INFO line each, and the operator concluded they were ignoring him.
#
# The trigger is the DECLARED slot, NOT the channel request. Measured on
# compute-04: 80 specs request the channel, 14 resolve a token, 66 do not — and
# the 66 include _template_generalist / _template_python_developer /
# _template_researcher. The request is inherited from the templates, so an
# ERROR keyed on it would print 66 red lines into the panel this prune exists
# to keep clean.
# ---------------------------------------------------------------------------


def _declaring_cfg(name: str, slot: str) -> AgentConfig:
    """A spec that DECLARES a pool slot — the statement of intent."""
    return _cfg(name, env={"CCT_BOT_TOKEN_SLOT": slot})


def test_declared_slot_that_does_not_resolve_logs_at_error(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — the spec names a slot; the pool does not have it.
    _pool_file(tmp_path, "export CCT_BOT_TOKEN_ZZ_OTHER=tok-other\n")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
        )
    # Assert — a stated mapping that does not work is an ERROR, not an INFO.
    assert [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]


def test_declared_slot_miss_still_removes_the_entry(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — loud does NOT mean "ship a server that cannot start".
    _pool_file(tmp_path, "")
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    prune_tokenless_telegrammer_mcp(
        tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
    )
    # Assert
    assert _TELEGRAMMER not in _servers_in(mcp)


def test_declared_slot_miss_error_names_the_agent(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-mute-and-deaf", "ZZ_GHOST")
        )
    # Assert — the operator must learn WHICH agent went quiet.
    assert "zz-mute-and-deaf" in caplog.text


def test_declared_slot_miss_error_names_the_declared_slot(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
        )
    # Assert — slot NAMES are loggable; token values never are.
    assert "CCT_BOT_TOKEN_ZZ_GHOST" in caplog.text


def test_declared_slot_miss_error_says_mute_and_deaf(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — deafness is the half that surprises: the absent entry kills
    # INBOUND too, which is why the operator read silence as being ignored.
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
        )
    # Assert
    assert "DEAF" in caplog.text


def test_declared_slot_miss_error_says_the_agent_cannot_self_diagnose(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — `health` is itself a tool on the server that was just removed,
    # so the agent cannot check its own rail. Say so, or the next reader will
    # tell it to "run health".
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
        )
    # Assert
    assert "health" in caplog.text


def test_declared_slot_miss_error_names_the_pool_source(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.ERROR):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-declared", "ZZ_GHOST")
        )
    # Assert — WHERE sac looked, so the fix can be applied to the right file.
    assert _SECRETS_VAR in caplog.text


def test_no_declared_slot_keeps_the_quiet_intentional_no_bot_path(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — the channel IS requested (every template requests it) but no
    # slot was ever declared. This is 66 of the 80 live specs: silence here is
    # the whole point, or the fix becomes the noise it was written to remove.
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        prune_tokenless_telegrammer_mcp(tmp_path, config=_cfg("zz-template-default"))
    # Assert
    assert not [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]


def test_no_declared_slot_still_says_intentional_no_bot_path(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.INFO):
        prune_tokenless_telegrammer_mcp(tmp_path, config=_cfg("zz-template-default"))
    # Assert — unchanged wording for the designed case.
    assert "intentional no-bot path" in caplog.text


def test_no_declared_slot_still_removes_the_entry(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange
    _pool_file(tmp_path, "")
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    prune_tokenless_telegrammer_mcp(tmp_path, config=_cfg("zz-template-default"))
    # Assert
    assert _TELEGRAMMER not in _servers_in(mcp)


def test_declared_slot_without_the_channel_is_not_an_error(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — an inert override on a spec that never asked for the rail.
    # Resolution was never attempted, so the slot cannot be blamed for missing.
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    cfg = _cfg("zz-no-channel", channel=False, env={"CCT_BOT_TOKEN_SLOT": "ZZ_GHOST"})
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        prune_tokenless_telegrammer_mcp(tmp_path, config=cfg)
    # Assert
    assert not [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]


def test_declared_slot_that_resolves_leaves_the_entry_alone(
    tmp_path: Path, secrets_envrc: None
) -> None:
    # Arrange — the declared mapping WORKS: ensure_cct_bot_token wrote the
    # token, so there is nothing to prune and nothing to report.
    _pool_file(tmp_path, "")
    mcp = _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("CCT_BOT_TOKEN=123:abc\n")
    # Act
    prune_tokenless_telegrammer_mcp(
        tmp_path, config=_declaring_cfg("zz-working", "ZZ_REAL")
    )
    # Assert
    assert _TELEGRAMMER in _servers_in(mcp)


def test_token_present_never_logs_an_error(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — a working agent must stay entirely quiet.
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("CCT_BOT_TOKEN=123:abc\n")
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        prune_tokenless_telegrammer_mcp(
            tmp_path, config=_declaring_cfg("zz-working", "ZZ_REAL")
        )
    # Assert
    assert not [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]


def test_prune_without_a_config_keeps_the_pre_spec_aware_behaviour(
    tmp_path: Path, secrets_envrc: None, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — no spec in hand means the two cases cannot be told apart, so
    # the blind INFO is the only honest answer.
    _pool_file(tmp_path, "")
    _write_mcp_json(tmp_path, _telegrammer_entry())
    (tmp_path / ".env").write_text("")
    # Act
    with caplog.at_level(scitex_logging.DEBUG):
        prune_tokenless_telegrammer_mcp(tmp_path)
    # Assert
    assert not [r for r in caplog.records if r.levelno >= scitex_logging.ERROR]
