"""The ONE agent-to-bot derivation, and its four outcomes.

Covers ``runtimes/_cct_token_resolution.resolve_cct_token`` — the computation
``ensure_cct_bot_token`` writes with, the fleet collision census counts with,
and the ownership ledger records with. A second derivation drifting from the
writer's is how a census reports collisions that do not exist; the tests that
matter most here are therefore the ones asserting the FOUR outcomes stay
distinct, and the one asserting no token VALUE ever reaches the result.

Real ``AgentConfig`` objects, real temp ``.env`` files, and a real
``PoolRead`` injected through the documented ``pool=`` seam — no mocks
(PA-306). STX-TQ002 AAA markers, STX-TQ007 one assert per test. Slot names use
a ``ZZ_``-prefixed namespace so an operator shell's real pool vars can never
collide with the fixtures.

Named ``test__cct_token_resolution.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_token_resolution.py``.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_token_resolution import (
    SOURCE_ENV_FILE,
    SOURCE_POOL,
    SOURCE_SPEC_ENV,
    TOKEN_DISABLED,
    TOKEN_NO_CHANNEL,
    TOKEN_RESOLVED,
    TOKEN_UNRESOLVED,
    resolve_cct_token,
)
from scitex_agent_container.runtimes._secret_pool import PoolRead

_CHANNEL = "server:claude-code-telegrammer"
# A value-shaped string. No test may find it in a resolution or its projection.
_SECRET = "zz-not-a-real-bot-token-0000000000"


def _cfg(
    name: str,
    *,
    channel: bool = True,
    workdir: str = "",
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


def _pool(slots: dict[str, str] | None = None, *, trusted: bool = True) -> PoolRead:
    """A real PoolRead — the documented injection seam, not a mock."""
    return PoolRead(
        env=dict(slots if slots is not None else {"CCT_BOT_TOKEN_ZZ_FOREIGN": _SECRET}),
        trusted=trusted,
        detail="" if trusted else "no canonical secret file resolved (test)",
    )


def _home(tmp_path: Path, env_body: str | None = None) -> Path:
    dest = tmp_path / "home"
    dest.mkdir(exist_ok=True)
    if env_body is not None:
        (dest / ".env").write_text(env_body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# the four outcomes, kept apart
# ---------------------------------------------------------------------------


def test_a_spec_without_the_channel_is_no_channel(tmp_path: Path) -> None:
    # Arrange — the pool holds this agent's slot; the spec never asks for it.
    cfg = _cfg("zz-quiet", channel=False)
    # Act
    got = resolve_cct_token(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_QUIET": _SECRET})
    )
    # Assert
    assert got.outcome == TOKEN_NO_CHANNEL


def test_an_explicitly_empty_spec_token_is_disabled_not_unresolved(
    tmp_path: Path,
) -> None:
    # Arrange — the handyman pattern: an empty spec value overrides the pool.
    cfg = _cfg("zz-hand", env={"CCT_BOT_TOKEN": ""})
    # Act
    got = resolve_cct_token(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_HAND": _SECRET})
    )
    # Assert — DELIBERATELY tokenless is not the same fact as broken.
    assert got.outcome == TOKEN_DISABLED


def test_the_empty_spec_token_beats_a_matching_pool_slot(tmp_path: Path) -> None:
    # Arrange — same shape; this asserts the ORDER, not just the label.
    cfg = _cfg("zz-hand", env={"CCT_BOT_TOKEN": ""})
    # Act
    got = resolve_cct_token(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_HAND": _SECRET})
    )
    # Assert — seven of eight handymen depend on this: they must claim nothing.
    assert not got.claims_a_token


def test_an_empty_spec_token_without_the_channel_is_still_disabled(
    tmp_path: Path,
) -> None:
    # Arrange — the LIVE handyman shape: empty token AND no channel request.
    cfg = _cfg("zz-hand", channel=False, env={"CCT_BOT_TOKEN": ""})
    # Act
    got = resolve_cct_token(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert — the explicit statement is reported over the mere absence.
    assert got.outcome == TOKEN_DISABLED


def test_a_requested_rail_with_no_slot_is_unresolved(tmp_path: Path) -> None:
    # Arrange — the channel is requested; the pool holds somebody else's bot.
    cfg = _cfg("zz-mute")
    # Act
    got = resolve_cct_token(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert
    assert got.outcome == TOKEN_UNRESOLVED


def test_unresolved_claims_no_token_so_it_cannot_collide(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-mute")
    # Act
    got = resolve_cct_token(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert — mute and deaf is a real fault, and it is not THIS one.
    assert not got.claims_a_token


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------


def test_a_pool_slot_resolves_from_the_agent_name(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-named")
    # Act
    got = resolve_cct_token(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_NAMED": _SECRET})
    )
    # Assert
    assert (got.outcome, got.source, got.slot) == (
        TOKEN_RESOLVED,
        SOURCE_POOL,
        "ZZ_NAMED",
    )


def test_a_declared_slot_is_exclusive(tmp_path: Path) -> None:
    # Arrange — the declared slot is absent; the mechanical one is present.
    cfg = _cfg("zz-named", env={"CCT_BOT_TOKEN_SLOT": "ZZ_TYPO"})
    # Act
    got = resolve_cct_token(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_NAMED": _SECRET})
    )
    # Assert — a typo fails loud; it never silently falls back to another bot.
    assert got.outcome == TOKEN_UNRESOLVED


def test_the_env_file_fold_wins_over_the_pool(tmp_path: Path) -> None:
    # Arrange — precedence #1: a project .envrc already folded a token in.
    cfg = _cfg("zz-named")
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}-folded\n")
    # Act
    got = resolve_cct_token(
        cfg, dest=dest, pool=_pool({"CCT_BOT_TOKEN_ZZ_NAMED": _SECRET})
    )
    # Assert
    assert got.source == SOURCE_ENV_FILE


def test_a_token_pinned_in_the_spec_wins_over_the_env_file(tmp_path: Path) -> None:
    # Arrange — an apptainer --env flag overrides --env-file at runtime, so a
    # pinned value is what the agent actually holds.
    cfg = _cfg("zz-pinned", env={"CCT_BOT_TOKEN": f"{_SECRET}-pinned"})
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}-folded\n")
    # Act
    got = resolve_cct_token(cfg, dest=dest, pool=_pool())
    # Assert
    assert got.source == SOURCE_SPEC_ENV


def test_dest_none_means_do_not_consult_an_env_file(tmp_path: Path) -> None:
    # Arrange — a .env exists but the caller did not offer it (a peer's spec
    # tree, whose materialised home lives on another host).
    cfg = _cfg("zz-named")
    _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}-folded\n")
    # Act
    got = resolve_cct_token(cfg, dest=None, pool=_pool())
    # Assert
    assert got.outcome == TOKEN_UNRESOLVED


# ---------------------------------------------------------------------------
# the fingerprint contract
# ---------------------------------------------------------------------------


def test_two_agents_on_one_slot_share_a_fingerprint(tmp_path: Path) -> None:
    # Arrange — the collision condition, at the level that detects it.
    pool = _pool({"CCT_BOT_TOKEN_ZZ_SHARED": _SECRET})
    one = _cfg("zz-one", env={"CCT_BOT_TOKEN_SLOT": "ZZ_SHARED"})
    two = _cfg("zz-two", env={"CCT_BOT_TOKEN_SLOT": "ZZ_SHARED"})
    # Act
    a = resolve_cct_token(one, dest=_home(tmp_path), pool=pool)
    b = resolve_cct_token(two, dest=None, pool=pool)
    # Assert
    assert a.token_fp == b.token_fp


def test_two_agents_on_different_slots_do_not(tmp_path: Path) -> None:
    # Arrange — the negative control: the fingerprint must DISCRIMINATE, or
    # every agent would "collide" with every other and the check would be
    # unfalsifiable.
    pool = _pool(
        {
            "CCT_BOT_TOKEN_ZZ_ONE": f"{_SECRET}-a",
            "CCT_BOT_TOKEN_ZZ_TWO": f"{_SECRET}-b",
        }
    )
    # Act
    a = resolve_cct_token(_cfg("zz-one"), dest=None, pool=pool)
    b = resolve_cct_token(_cfg("zz-two"), dest=None, pool=pool)
    # Assert
    assert a.token_fp != b.token_fp


def test_the_fingerprint_is_opaque(tmp_path: Path) -> None:
    # Arrange
    pool = _pool({"CCT_BOT_TOKEN_ZZ_NAMED": _SECRET})
    # Act
    got = resolve_cct_token(_cfg("zz-named"), dest=None, pool=pool)
    # Assert
    assert got.token_fp.startswith("sha256:")


def test_no_token_value_reaches_the_projection(tmp_path: Path) -> None:
    # Arrange — the whole secret contract, asserted on the surface that
    # travels: to_dict() is what --json prints and what a ledger row is built
    # from.
    pool = _pool({"CCT_BOT_TOKEN_ZZ_NAMED": _SECRET})
    got = resolve_cct_token(_cfg("zz-named"), dest=None, pool=pool)
    # Act
    rendered = repr(got.to_dict()) + got.detail + repr(got)
    # Assert
    assert _SECRET not in rendered


def test_an_untrusted_pool_read_is_carried_into_the_resolution(
    tmp_path: Path,
) -> None:
    # Arrange — a MISS against an untrusted read proves nothing, and the
    # caller cannot know that unless the resolution says so.
    cfg = _cfg("zz-mute")
    # Act
    got = resolve_cct_token(cfg, dest=None, pool=_pool(trusted=False))
    # Assert
    assert got.pool_trusted is False
