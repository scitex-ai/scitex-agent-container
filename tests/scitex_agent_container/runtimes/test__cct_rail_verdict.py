"""The three-valued Telegram-rail verdict.

Covers ``runtimes/_cct_rail_verdict.assess_cct_rail`` (card
``sac-cct-rail-loud-when-no-slot-resolves-20260812``): an agent that declares
``server:claude-code-telegrammer`` and resolves no ``CCT_BOT_TOKEN_<SLOT>`` has
its MCP server removed and starts MUTE and DEAF with no signal anywhere.

The load-bearing tests here are the UNKNOWN ones. "No slot resolves" and "sac
could not tell whether a slot resolves" are DIFFERENT FACTS, and the second
must never read as the first — that collapse is what made three consecutive
diagnoses of the 2026-08-12 outage wrong ("there is no token on 04"; the token
was there, the LAUNCHING PROCESS could not see it).

Real ``AgentConfig`` objects, real temp ``.env`` files, and a real
``PoolRead`` value object injected through the documented ``pool=`` seam — no
mocks stand in for the behaviour under test (PA-306). STX-TQ002 AAA markers,
STX-TQ007 one assert per test. Slot names use a ``ZZ_``-prefixed namespace so
an operator shell's real pool vars can never collide with the fixtures.

Named ``test__cct_rail_verdict.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_rail_verdict.py``.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_rail_verdict import (
    RAIL_DOWN,
    RAIL_NOT_REQUESTED,
    RAIL_UNKNOWN,
    RAIL_UP,
    assess_cct_rail,
    materialised_home,
    near_miss_slots,
)
from scitex_agent_container.runtimes._secret_pool import PoolRead

_CHANNEL = "server:claude-code-telegrammer"
# A value-shaped string. No test may find it in a verdict, message or remedy.
_SECRET = "zz-secret-value-must-never-be-echoed"


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
    """A real PoolRead — the documented injection seam, not a mock.

    The default carries ANOTHER agent's bot, so "the pool does not have this
    agent's slot" is expressible without accidentally also saying "this is not
    the bot pool" — which is a different verdict (UNKNOWN, see below) and would
    make most of these tests assert the wrong one.
    """
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
# gating
# ---------------------------------------------------------------------------


def test_not_requested_when_the_spec_omits_the_channel(tmp_path: Path) -> None:
    # Arrange — pool holds a matching slot, but the spec never asks for a rail.
    cfg = _cfg("zz-norail", channel=False)
    # Act
    verdict = assess_cct_rail(
        cfg, dest=_home(tmp_path), pool=_pool({"CCT_BOT_TOKEN_ZZ_NORAIL": _SECRET})
    )
    # Assert — bot-less BY DECLARATION is not a fault.
    assert verdict.state == RAIL_NOT_REQUESTED


def test_unrequested_rail_is_not_alarming(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-quiet", channel=False)
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert
    assert verdict.is_alarming is False


# ---------------------------------------------------------------------------
# UP — the rail resolves
# ---------------------------------------------------------------------------


def test_up_when_the_env_file_already_carries_a_token(tmp_path: Path) -> None:
    # Arrange — precedence #1: the .envrc cascade already folded a token in.
    # This is the route that carried the rail on ywata-note-win and vanished
    # on relocation; reporting it as absent is the false negative.
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    verdict = assess_cct_rail(_cfg("zz-folded"), dest=dest, pool=_pool())
    # Assert
    assert verdict.state == RAIL_UP


def test_env_file_route_is_reported_as_env_file_not_as_a_slot(tmp_path: Path) -> None:
    # Arrange
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    verdict = assess_cct_rail(_cfg("zz-folded2"), dest=dest, pool=_pool())
    # Assert — the .envrc route never names a slot; claiming one would be a lie.
    assert verdict.source == "env-file"


def test_up_when_the_declared_slot_resolves(tmp_path: Path) -> None:
    # Arrange — precedence #2, the documented per-spec override.
    cfg = _cfg("zz-mismatch", env={"CCT_BOT_TOKEN_SLOT": "ZZ_DECLARED"})
    # Act
    verdict = assess_cct_rail(
        cfg,
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_ZZ_DECLARED": _SECRET}),
    )
    # Assert
    assert verdict.resolved_slot == "ZZ_DECLARED"


def test_up_when_a_mechanically_derived_candidate_resolves(tmp_path: Path) -> None:
    # Arrange — precedence #3, upper-snake of the agent name.
    # Act
    verdict = assess_cct_rail(
        _cfg("zz-derived"),
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_ZZ_DERIVED": _SECRET}),
    )
    # Assert
    assert verdict.resolved_slot == "ZZ_DERIVED"


def test_a_declared_slot_suppresses_the_mechanical_fallback(tmp_path: Path) -> None:
    # Arrange — a DECLARED slot must fail loud on a typo rather than silently
    # falling back to a derived name that might belong to another agent.
    cfg = _cfg("zz-typo", env={"CCT_BOT_TOKEN_SLOT": "ZZ_WRONG"})
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert
    assert verdict.candidates == ("ZZ_WRONG",)


# ---------------------------------------------------------------------------
# DOWN — conclusively no rail
# ---------------------------------------------------------------------------


def test_down_when_a_conclusive_pool_holds_no_matching_slot(tmp_path: Path) -> None:
    # Arrange — sac read the pool it meant to read, and the slot is not in it.
    # Act
    verdict = assess_cct_rail(
        _cfg("zz-mute"),
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_ZZ_SOMEONE_ELSE": _SECRET}),
    )
    # Assert
    assert verdict.state == RAIL_DOWN


def test_a_down_rail_says_mute_and_deaf(tmp_path: Path) -> None:
    # Arrange — the operator-visible symptom is silence in BOTH directions;
    # a message that only says "no token" does not describe what he will see.
    # Act
    verdict = assess_cct_rail(_cfg("zz-mute2"), dest=_home(tmp_path), pool=_pool())
    # Assert
    assert "DEAF" in verdict.detail


def test_down_is_alarming(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-mute3")
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert
    assert verdict.is_alarming is True


# ---------------------------------------------------------------------------
# UNKNOWN — the third value, and the reason this module exists
# ---------------------------------------------------------------------------


def test_unknown_when_the_pool_read_was_not_conclusive(tmp_path: Path) -> None:
    # Arrange — the pool file exists on the host; the LAUNCHING PROCESS could
    # not see it (no SAC_SECRETS_ENVRC on a systemd unit / non-interactive
    # ssh). Exactly the 2026-08-12 shape.
    # Act
    verdict = assess_cct_rail(
        _cfg("zz-blind"), dest=_home(tmp_path), pool=_pool(trusted=False)
    )
    # Assert — sac did not learn that this agent has no bot. It learned nothing.
    assert verdict.state == RAIL_UNKNOWN


def test_an_inconclusive_read_is_never_reported_as_down(tmp_path: Path) -> None:
    # Arrange — the same inputs that would be DOWN under a trusted read.
    # Act
    verdict = assess_cct_rail(
        _cfg("zz-blind2"),
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_ZZ_OTHER": _SECRET}, trusted=False),
    )
    # Assert
    assert verdict.state != RAIL_DOWN


def test_an_inconclusive_read_refuses_to_read_as_an_all_clear(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-blind3")
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool(trusted=False))
    # Assert — the detail must say what was NOT established.
    assert "cannot distinguish" in verdict.detail


def test_a_pool_with_no_bot_slots_at_all_is_unknown(tmp_path: Path) -> None:
    # Arrange — the read SUCCEEDED and holds no CCT_BOT_TOKEN_* at all, so it
    # is not the bot pool. Measured shape: the CCT secrets file never reached
    # the launching process. Calling this DOWN would report ~80 confident
    # false alarms, which is indistinguishable from a broken fleet.
    pool = _pool({"SOME_OTHER_SECRET": _SECRET})
    # Act
    verdict = assess_cct_rail(_cfg("zz-wrongpool"), dest=_home(tmp_path), pool=pool)
    # Assert
    assert verdict.state == RAIL_UNKNOWN


def test_a_pool_holding_other_agents_bots_is_still_conclusive(tmp_path: Path) -> None:
    # Arrange — the guard above must not swallow the real DOWN case: a pool
    # WITH bot slots, none of which are this agent's, is a genuine answer.
    pool = _pool({"CCT_BOT_TOKEN_ZZ_SOMEONE_ELSE": _SECRET})
    # Act
    verdict = assess_cct_rail(_cfg("zz-realmute"), dest=_home(tmp_path), pool=pool)
    # Assert
    assert verdict.state == RAIL_DOWN


def test_unknown_when_the_env_file_exists_but_cannot_be_read(tmp_path: Path) -> None:
    # Arrange — a .env sac cannot DECODE is as unread as one it cannot open,
    # and precedence #1 is exactly where a token would have been.
    dest = _home(tmp_path)
    (dest / ".env").write_bytes(b"CCT_BOT_TOKEN=\xff\xfe\x00bad\n")
    # Act
    verdict = assess_cct_rail(_cfg("zz-unreadable"), dest=dest, pool=_pool())
    # Assert
    assert verdict.state == RAIL_UNKNOWN


def test_a_never_materialised_home_is_a_conclusive_absence_not_unknown() -> None:
    # Arrange — an agent that has never been started has no $HOME/.env, so
    # precedence #1 has not HAPPENED yet. That is an observation, not a blind
    # spot: the pool answer stands on its own.
    #
    # The tempting alternative — "the file is not there, so I did not look" —
    # was rejected deliberately. It would mark all ~89 never-started specs
    # UNKNOWN in the fleet audit and drown the handful that are really broken,
    # which is the same "loud everywhere = loud nowhere" failure this work
    # exists to avoid. UNKNOWN is reserved for a file that EXISTS and could not
    # be read (see the test above), where something really was unobserved.
    cfg = _cfg("zz-never-started")
    # Act
    verdict = assess_cct_rail(cfg, dest=None, pool=_pool())
    # Assert
    assert verdict.state == RAIL_DOWN


def test_the_materialised_home_is_none_when_it_cannot_be_resolved() -> None:
    # Arrange — a config with none of the AgentConfig surface.
    broken = object()
    # Act
    resolved = materialised_home(broken)
    # Assert — a real answer ("I do not know where to look"), which the
    # assessment turns into UNKNOWN rather than into a missing token.
    assert resolved is None


def test_unknown_is_alarming(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-blind4")
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool(trusted=False))
    # Assert — an unread instrument must reach a human, not be filed as fine.
    assert verdict.is_alarming is True


# ---------------------------------------------------------------------------
# near misses — a hint for a human, never a resolution
# ---------------------------------------------------------------------------


def test_near_miss_finds_a_word_order_mismatch() -> None:
    # Arrange — the live neurovista-paper-writer shape. No stripping rule can
    # bridge a word-order swap, which is why this is a REPORT and not a rule.
    pool = {"CCT_BOT_TOKEN_PAPER_NEUROVISTA_WRITER": _SECRET}
    # Act
    hits = near_miss_slots("neurovista-paper-writer", pool)
    # Assert
    assert hits == ("PAPER_NEUROVISTA_WRITER",)


def test_near_miss_finds_a_prefixed_slot() -> None:
    # Arrange — the live neurovista shape.
    # Act
    hits = near_miss_slots("neurovista", {"CCT_BOT_TOKEN_PAPER_NEUROVISTA": _SECRET})
    # Assert
    assert hits == ("PAPER_NEUROVISTA",)


def test_near_miss_ignores_generic_words() -> None:
    # Arrange — "SCITEX" and "AGENT" must not make every scitex slot a match,
    # or the hint becomes the pool listing and stops being a hint.
    pool = {"CCT_BOT_TOKEN_SCITEX_SCHOLAR": _SECRET, "CCT_BOT_TOKEN_SAC": _SECRET}
    # Act
    hits = near_miss_slots("scitex-agent-container", pool)
    # Assert
    assert hits == ()


def test_near_miss_never_resolves_the_rail(tmp_path: Path) -> None:
    # Arrange — a near miss is present and is NOT this agent's declared slot.
    # Act
    verdict = assess_cct_rail(
        _cfg("neurovista"),
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_PAPER_NEUROVISTA": _SECRET}),
    )
    # Assert — reported, never taken. Taking it is how an agent steals a bot.
    assert verdict.state == RAIL_DOWN


def test_near_miss_is_reported_on_the_verdict(tmp_path: Path) -> None:
    # Arrange
    pool = _pool({"CCT_BOT_TOKEN_PAPER_NEUROVISTA": _SECRET})
    # Act
    verdict = assess_cct_rail(_cfg("neurovista"), dest=_home(tmp_path), pool=pool)
    # Assert
    assert verdict.near_misses == ("PAPER_NEUROVISTA",)


# ---------------------------------------------------------------------------
# the remedy must name the fix, and nothing must name the token
# ---------------------------------------------------------------------------


def test_remedy_names_the_per_spec_slot_override(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-fixme")
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert — an error that says what to DO.
    assert "CCT_BOT_TOKEN_SLOT" in verdict.remedy()


def test_remedy_names_where_the_override_goes(tmp_path: Path) -> None:
    # Arrange
    cfg = _cfg("zz-fixme2")
    # Act
    verdict = assess_cct_rail(cfg, dest=_home(tmp_path), pool=_pool())
    # Assert
    assert "spec.apptainer.env" in verdict.remedy()


def test_remedy_offers_dropping_the_channel(tmp_path: Path) -> None:
    # Arrange — an agent that genuinely needs no rail should stop being
    # reported, and the way to do that is to stop declaring one.
    # Act
    verdict = assess_cct_rail(_cfg("zz-fixme3"), dest=_home(tmp_path), pool=_pool())
    # Assert
    assert "spec.claude.channels" in verdict.remedy()


def test_the_verdict_never_carries_a_token_value(tmp_path: Path) -> None:
    # Arrange — a resolving pool, so a careless implementation would have the
    # value in hand at exactly the moment it builds the message.
    # Act
    verdict = assess_cct_rail(
        _cfg("zz-quiet-value"),
        dest=_home(tmp_path),
        pool=_pool({"CCT_BOT_TOKEN_ZZ_QUIET_VALUE": _SECRET}),
    )
    # Assert
    assert _SECRET not in repr(verdict)


def test_the_env_file_route_never_carries_a_token_value(tmp_path: Path) -> None:
    # Arrange — the other route that holds a real value while building text.
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    verdict = assess_cct_rail(_cfg("zz-quiet-env"), dest=dest, pool=_pool())
    # Assert
    assert _SECRET not in repr(verdict)
