"""Every boot must leave a record of WHICH account it chose, and on what evidence.

MEASURED 2026-08-21, ywata-note-win. Agent ``business`` booted onto
``scitex-01-scitex-ai`` at ``d7=100%`` and answered "You've hit your weekly
limit" on every turn. sac printed ``SUCC``, tmux was alive, and all three
startup prompts reported ``idle-gated submit verified``.

Reconstructing that boot afterwards was IMPOSSIBLE. The only trace anywhere
under ``~/.scitex/agent-container/runtime`` was auth-heal noting the agent was
"no longer login-expired"; ``auth-events.jsonl`` had no entry for the boot at
all. Nothing named the account, its quota, or which gate had been armed — so
"did the guard run and choose this, or was it never consulted?" had no answer,
and the investigation went hunting a picker bug the code does not have
(``_quota_rank.EXPIRING_MIN_HEADROOM_PCT`` already excludes a capped account
from the expiring-capacity preference, by construction).

What is locked here
-------------------
1. The selection record names the account, its cached 5h/7d, and WHICH branch
   of the gate produced it.
2. An account with no weekly budget warns — loudly, naming the consequence.
3. An account with headroom does NOT warn, so the warning keeps its meaning in
   a fleet where near-cap is routine (measured the same night: four of four
   stored accounts sat at 90-100%).
4. A quota the cache does not carry renders as ``?``, never as a number. Every
   bug this module documents begins with an unknown quota rendered as if known.
5. The recorder is a pass-through: it reports, it never changes the pick.

PA-306: no mocks. A real cache file in ``tmp_path``, and a real ``pick``
callable — which is a genuine stand-in rather than a mock, because ``pick`` is
a ``Callable`` PARAMETER of the function under test ("a callable rather than a
kwargs bundle because the two preflight call sites pass different candidate
universes"). AAA markers (TQ002); descriptive names; one assertion each
(TQ007).
"""

from __future__ import annotations

import io
import json
import logging
import time
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._quota_evidence import (
    SELECTION_MARKER,
    pick_with_quota_evidence,
)

_LOGGER = "scitex_agent_container._lifecycle._quota_evidence"

# The account from the incident. Its first dash-segment is "scitex", which is
# the quota cache's per-account match key (``short``) — spelled out here
# because a cache entry keyed on the wrong segment reads as "no entry" and
# every percentage would silently render "?", passing a weaker assertion.
_CAPPED = "scitex-01-scitex-ai"
_CAPPED_SHORT = "scitex"

# The only stored account that still had weekly budget that night.
_HEALTHY = "ywata1989-gmail-com"
_HEALTHY_SHORT = "ywata1989"


def _write_cache(path: Path, entries: dict[str, dict]) -> Path:
    """A real, FRESH quota cache, so the armed-and-already-fresh branch runs.

    ``written_at`` is stamped now on purpose: an undated or hour-old cache
    routes the caller into the staleness re-measure instead, which is a
    different branch than the one these tests are about.
    """
    path.write_text(json.dumps({"written_at": time.time(), "accounts": entries}))
    return path


def _capped_cache(tmp_path: Path) -> Path:
    return _write_cache(
        tmp_path / "quota-cache.json",
        {
            _CAPPED: {"short": _CAPPED_SHORT, "h5": 0.0, "d7": 100.0, "ttl_h": 4.9},
            _HEALTHY: {"short": _HEALTHY_SHORT, "h5": 17.0, "d7": 90.0, "ttl_h": 4.9},
        },
    )


def _pick(account: str):
    """A real callable returning a fixed account, mirroring ``pick``'s contract."""

    def _picker(require_quota_evidence: bool) -> str:
        return account

    return _picker


def _selection_line(caplog: pytest.LogCaptureFixture) -> str:
    """The selection record only.

    Scoped to the marker rather than searching the whole log, because the
    module's other lines also name the agent and would let an unscoped
    substring assertion pass against a build that records nothing.
    """
    lines = [
        r.getMessage() for r in caplog.records if SELECTION_MARKER in r.getMessage()
    ]
    assert len(lines) == 1, f"expected exactly one selection record, got {lines}"
    return lines[0]


def test_the_selection_record_names_the_account_the_boot_actually_chose(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        pick_with_quota_evidence(
            _pick(_CAPPED),
            agent_name="business",
            quota_cache_path=cache,
            log_stream=io.StringIO(),
        )
    # Assert
    assert _CAPPED in _selection_line(caplog)


def test_the_selection_record_carries_the_accounts_cached_7d(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        pick_with_quota_evidence(
            _pick(_CAPPED),
            agent_name="business",
            quota_cache_path=cache,
            log_stream=io.StringIO(),
        )
    # Assert
    assert "7d=100%" in _selection_line(caplog)


def test_the_selection_record_names_which_gate_produced_the_account(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        pick_with_quota_evidence(
            _pick(_CAPPED),
            agent_name="business",
            quota_cache_path=cache,
            log_stream=io.StringIO(),
        )
    # Assert
    assert "gate armed" in _selection_line(caplog)


def test_an_account_with_no_weekly_budget_warns_that_every_turn_will_be_refused(
    tmp_path: Path,
) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    stream = io.StringIO()
    # Act
    pick_with_quota_evidence(
        _pick(_CAPPED),
        agent_name="business",
        quota_cache_path=cache,
        log_stream=stream,
    )
    # Assert
    assert "hit your weekly limit" in stream.getvalue()


def test_an_account_that_still_has_headroom_does_not_warn(tmp_path: Path) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    stream = io.StringIO()
    # Act
    pick_with_quota_evidence(
        _pick(_HEALTHY),
        agent_name="business",
        quota_cache_path=cache,
        log_stream=stream,
    )
    # Assert
    assert stream.getvalue() == ""


def test_a_quota_the_cache_does_not_carry_renders_as_a_question_mark(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Arrange — a cache that is present and fresh but knows nothing about the
    # account the picker returns. This is the shape that produced "5h=? 7d=?"
    # in the 2026-07-20 incident, and it must never render as a number.
    cache = _write_cache(
        tmp_path / "quota-cache.json",
        {_HEALTHY: {"short": _HEALTHY_SHORT, "h5": 17.0, "d7": 90.0, "ttl_h": 4.9}},
    )
    # Act
    with caplog.at_level(logging.INFO, logger=_LOGGER):
        pick_with_quota_evidence(
            _pick("stranger-example-com"),
            agent_name="business",
            quota_cache_path=cache,
            log_stream=io.StringIO(),
        )
    # Assert
    assert "5h=? 7d=?" in _selection_line(caplog)


def test_recording_the_selection_does_not_change_the_picked_account(
    tmp_path: Path,
) -> None:
    # Arrange
    cache = _capped_cache(tmp_path)
    # Act
    picked = pick_with_quota_evidence(
        _pick(_CAPPED),
        agent_name="business",
        quota_cache_path=cache,
        log_stream=io.StringIO(),
    )
    # Assert
    assert picked == _CAPPED
