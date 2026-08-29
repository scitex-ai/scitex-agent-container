"""The start-time write to the bot-token ownership ledger. Never fatal.

Covers ``runtimes/_cct_token_ledger.record_token_claim_at_start``, the hook
:mod:`.._lifecycle._start` runs beside the rail verdict.

THE PROPERTY THAT MATTERS IS THE NEGATIVE ONE. The store underneath is
PostgreSQL-only and RAISES when the database is unreachable — correct for the
store, unacceptable on a path attached to every start in the fleet. So the
tests that carry the design are the ones proving an unreachable store yields a
printed line and a return value, not an exception: a ledger that can refuse a
boot is strictly worse than a missing ledger.

The unreachable store is NOT mocked. ``tests/_store_isolation.py`` already
points every test at a DSN that cannot exist (``127.0.0.1:1``), so the failure
these tests exercise is the real one production would hit, produced by the
real client against a real closed port (PA-306).

STX-TQ002 AAA markers, STX-TQ007 one assert per test. Slot names use a
``ZZ_``-prefixed namespace so an operator shell's real pool vars can never
collide with the fixtures.

Named ``test__cct_token_ledger.py`` for the PS-202/PS-204 mirror against
``src/scitex_agent_container/runtimes/_cct_token_ledger.py``.
"""

from __future__ import annotations

import io
from pathlib import Path

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._cct_token_ledger import (
    CLAIM_FAILED,
    CLAIM_SKIPPED,
    record_token_claim_at_start,
)

_CHANNEL = "server:claude-code-telegrammer"
# A value-shaped string. No stderr line may contain it.
_SECRET = "zz-not-a-real-bot-token-0000000000"


def _cfg(name: str, *, channel: bool = True, env: dict | None = None) -> AgentConfig:
    cfg = AgentConfig(name=name)
    if channel:
        cfg.claude.channels = [_CHANNEL]
    if env:
        cfg.env = dict(env)
    return cfg


def _home(tmp_path: Path, env_body: str | None = None) -> Path:
    dest = tmp_path / "home"
    dest.mkdir(exist_ok=True)
    if env_body is not None:
        (dest / ".env").write_text(env_body, encoding="utf-8")
    return dest


# ---------------------------------------------------------------------------
# nothing to record
# ---------------------------------------------------------------------------


def test_an_agent_without_the_channel_records_nothing(tmp_path: Path) -> None:
    # Arrange
    stream = io.StringIO()
    # Act
    outcome = record_token_claim_at_start(
        _cfg("zz-quiet", channel=False), dest=_home(tmp_path), err_stream=stream
    )
    # Assert
    assert outcome == CLAIM_SKIPPED


def test_a_deliberately_tokenless_agent_records_nothing(tmp_path: Path) -> None:
    # Arrange — the handyman pattern holds no token, so it owns none.
    stream = io.StringIO()
    # Act
    outcome = record_token_claim_at_start(
        _cfg("zz-hand", env={"CCT_BOT_TOKEN": ""}),
        dest=_home(tmp_path),
        err_stream=stream,
    )
    # Assert
    assert outcome == CLAIM_SKIPPED


def test_an_agent_that_resolves_nothing_records_nothing(tmp_path: Path) -> None:
    # Arrange — mute and deaf is a real fault and it is not an ownership
    # claim; recording it would put a nameless row in the ledger.
    stream = io.StringIO()
    # Act
    outcome = record_token_claim_at_start(
        _cfg("zz-mute", env={"CCT_BOT_TOKEN_SLOT": "ZZ_ABSENT"}),
        dest=_home(tmp_path),
        err_stream=stream,
    )
    # Assert
    assert outcome == CLAIM_SKIPPED


def test_a_skipped_claim_says_nothing_on_stderr(tmp_path: Path) -> None:
    # Arrange — most of the fleet has no bot; a line each would be noise in
    # the one panel the operator actually reads.
    stream = io.StringIO()
    # Act
    record_token_claim_at_start(
        _cfg("zz-quiet", channel=False), dest=_home(tmp_path), err_stream=stream
    )
    # Assert
    assert stream.getvalue() == ""


# ---------------------------------------------------------------------------
# an unreachable store must not break a start
# ---------------------------------------------------------------------------


def test_an_unreachable_store_does_not_raise(tmp_path: Path) -> None:
    # Arrange — the suite's store isolation points at a DSN that cannot exist,
    # which is exactly the production failure: PostgreSQL down.
    stream = io.StringIO()
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    outcome = record_token_claim_at_start(
        _cfg("zz-owner"), dest=dest, err_stream=stream
    )
    # Assert
    assert outcome == CLAIM_FAILED


def test_an_unreachable_store_says_the_agent_starts_normally(tmp_path: Path) -> None:
    # Arrange — a warning that does not say what it did NOT break is read as
    # a boot failure; that misreading is what demoted this subsystem's
    # missing-token log from ERROR to WARNING in the first place.
    stream = io.StringIO()
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    record_token_claim_at_start(_cfg("zz-owner"), dest=dest, err_stream=stream)
    # Assert
    assert "THE AGENT STARTS NORMALLY" in stream.getvalue()


def test_the_failure_line_names_the_agent(tmp_path: Path) -> None:
    # Arrange — one line per start across a 122-agent fleet is unreadable
    # unless it says WHOSE claim went unrecorded.
    stream = io.StringIO()
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    record_token_claim_at_start(_cfg("zz-owner"), dest=dest, err_stream=stream)
    # Assert
    assert "zz-owner" in stream.getvalue()


def test_no_token_value_reaches_stderr(tmp_path: Path) -> None:
    # Arrange — the failure path is the one that prints, so it is the one that
    # could leak. Only a fingerprint may travel, and not even that is printed.
    stream = io.StringIO()
    dest = _home(tmp_path, f"CCT_BOT_TOKEN={_SECRET}\n")
    # Act
    record_token_claim_at_start(_cfg("zz-owner"), dest=dest, err_stream=stream)
    # Assert
    assert _SECRET not in stream.getvalue()


def test_a_broken_config_does_not_raise(tmp_path: Path) -> None:
    # Arrange — an object that is not an AgentConfig at all: whatever this
    # function is handed, a start must survive it.
    stream = io.StringIO()
    # Act
    outcome = record_token_claim_at_start(object(), dest=None, err_stream=stream)
    # Assert
    assert outcome in (CLAIM_SKIPPED, CLAIM_FAILED)
