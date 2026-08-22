"""Both CCT start-time observations run, and neither can break a start.

Covers ``runtimes/_cct_start_observers.observe_cct_at_start`` — the single
seam :mod:`.._lifecycle._start` calls, holding the rail verdict and the
ownership-ledger write.

WHY A SEAM AND NOT TWO CALLS AT THE CALL SITE: ``_start.py`` sits against a
512-line cap, and more importantly the two observations share a PRECONDITION
— the agent's ``$HOME/.env`` has just been materialised, and that file is
precedence #1 of the token resolution. Naming that precondition once, in one
module, is what stops the second observation drifting to a place where it
reports every agent as token-less.

The tests here assert the pair RUNS and that a failing half does not take the
start down. The halves' own behaviour is measured in
``test__cct_rail_alarm.py`` and ``test__cct_token_ledger.py``.

The store the ledger writes to is genuinely unreachable in this suite
(``tests/_store_isolation.py`` points every test at ``127.0.0.1:1``), so the
"a broken half does not raise" property is exercised against a real failure
rather than a simulated one (PA-306). STX-TQ002 AAA markers, STX-TQ007 one
assert per test.

Named ``test__cct_start_observers.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_start_observers.py``.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_start_observers import observe_cct_at_start
from scitex_agent_container.runtimes._cct_token_ledger import (
    CLAIM_FAILED,
    CLAIM_SKIPPED,
)

_CHANNEL = "server:claude-code-telegrammer"
_SECRET = "zz-not-a-real-bot-token-0000000000"


def _cfg(name: str, *, channel: bool = True) -> AgentConfig:
    cfg = AgentConfig(name=name)
    if channel:
        cfg.claude.channels = [_CHANNEL]
    return cfg


def _home(tmp_path: Path, env_body: str | None = None) -> Path:
    dest = tmp_path / "home"
    dest.mkdir(exist_ok=True)
    if env_body is not None:
        (dest / ".env").write_text(env_body, encoding="utf-8")
    return dest


def test_a_bot_less_agent_skips_both_observations(tmp_path: Path) -> None:
    # Arrange — most of the fleet: no channel, nothing to say about it.
    cfg = _cfg("zz-quiet", channel=False)
    # Act
    rail, claim = observe_cct_at_start(cfg, dest=_home(tmp_path))
    # Assert
    assert (rail, claim) == ("skipped", CLAIM_SKIPPED)


def test_the_ledger_half_runs_for_an_agent_that_holds_a_bot(tmp_path: Path) -> None:
    # Arrange — a token folded into .env is precedence #1, and an agent whose
    # bot arrives that way can collide exactly like one that resolved a slot.
    cfg = _cfg("zz-owner")
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    _rail, claim = observe_cct_at_start(cfg, dest=dest)
    # Assert — the store is unreachable here, so FAILED is the proof it ran.
    assert claim == CLAIM_FAILED


def test_a_failing_ledger_does_not_stop_the_rail_verdict(tmp_path: Path) -> None:
    # Arrange — ORDER IS THE POINT: the rail verdict can page a human about an
    # outage, so a slow or unreachable PostgreSQL must never sit in front of
    # it, and must never swallow it either.
    cfg = _cfg("zz-owner")
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    rail, _claim = observe_cct_at_start(cfg, dest=dest)
    # Assert
    assert rail == "clear"


def test_a_broken_config_does_not_raise(tmp_path: Path) -> None:
    # Arrange — this seam is attached to every start in the fleet; whatever it
    # is handed, the start must survive it.
    # Act
    rail, claim = observe_cct_at_start(object(), dest=_home(tmp_path))
    # Assert
    assert (rail, claim) == ("skipped", CLAIM_SKIPPED)
