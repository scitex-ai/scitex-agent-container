"""Tests for the CREDS-PHASE1 account picker wired into ``agent_start``.

PA-306: no mocks. Real config dataclasses, real store snapshots,
real :func:`_rotate_to_healthy_account` mutation against an isolated
``$HOME``. AAA markers (TQ002), descriptive names, one assertion each.

The picker wiring lives in :mod:`scitex_agent_container._lifecycle.
_start`; this file exercises the helper directly because the full
``agent_start`` flow already has dedicated coverage in
``test_lifecycle.py`` / ``test__start_drift.py`` — the new behaviour
is "rotate ``config.claude.account`` to a healthy stored account, or
fail loud" and that's what we pin here.
"""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._creds import NoHealthyAccountError
from scitex_agent_container._lifecycle._start import _rotate_to_healthy_account
from scitex_agent_container.config import AgentConfig


@pytest.fixture
def _isolate_home(tmp_path: Path) -> Iterator[Path]:
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _write_snapshot(home: Path, name: str, expires_at_ms: int) -> None:
    path = (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}}))


def _future_ms(seconds: float = 7200.0) -> int:
    return int((time.time() + seconds) * 1_000)


def _past_ms(seconds: float = 600.0) -> int:
    return int((time.time() - seconds) * 1_000)


def _make_config(name: str, account: str) -> AgentConfig:
    cfg = AgentConfig(name=name)
    cfg.claude.account = account
    return cfg


# ---------------------------------------------------------------------------
# Unpinned agent — picker is a no-op.
# ---------------------------------------------------------------------------


def test_unpinned_agent_leaves_account_unchanged(_isolate_home: Path) -> None:
    # Arrange — no stored accounts, no pin: must NOT touch host live OAuth.
    cfg = _make_config("alpha", account="")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert
    assert cfg.claude.account == ""


def test_unpinned_agent_does_not_print_rotation_line(_isolate_home: Path) -> None:
    # Arrange
    cfg = _make_config("alpha", account="")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert — quiet path; no log noise for the common unpinned case.
    assert log.getvalue() == ""


# ---------------------------------------------------------------------------
# Pinned + healthy — picker is a no-op.
# ---------------------------------------------------------------------------


def test_pinned_healthy_agent_keeps_pinned_account(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert
    assert cfg.claude.account == "ywatanabe-scitex-ai"


def test_pinned_healthy_agent_emits_no_rotation_line(_isolate_home: Path) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _future_ms())
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert — quiet when no rotation happened.
    assert log.getvalue() == ""


# ---------------------------------------------------------------------------
# Pinned + unhealthy + a healthy alternative — rotate.
# ---------------------------------------------------------------------------


def test_pinned_expired_agent_rotates_to_healthy_account(_isolate_home: Path) -> None:
    # Arrange — pinned is EXPIRED, sibling is fresh.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(60))
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert
    assert cfg.claude.account == "wyusuuke-gmail-com"


def test_pinned_rotation_emits_a_one_line_notice_to_log_stream(
    _isolate_home: Path,
) -> None:
    # Arrange
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(60))
    _write_snapshot(home, "wyusuuke-gmail-com", _future_ms())
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    log = io.StringIO()
    # Act
    _rotate_to_healthy_account(cfg, log_stream=log)
    # Assert — the operator must see WHICH agent and HOW the account moved.
    msg = log.getvalue()
    assert (
        "alpha" in msg
        and "ywatanabe-scitex-ai" in msg
        and "wyusuuke-gmail-com" in msg
    )


# ---------------------------------------------------------------------------
# Pinned + nothing healthy — fail loud.
# ---------------------------------------------------------------------------


def test_pinned_all_expired_raises_no_healthy_account_error(
    _isolate_home: Path,
) -> None:
    # Arrange — every stored snapshot expired.
    home = _isolate_home
    _write_snapshot(home, "ywatanabe-scitex-ai", _past_ms(60))
    _write_snapshot(home, "wyusuuke-gmail-com", _past_ms(60))
    _write_snapshot(home, "ywata1989-gmail-com", _past_ms(60))
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    # Act
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert
    with ctx:
        _rotate_to_healthy_account(cfg)


def test_pinned_agent_with_absent_store_raises(_isolate_home: Path) -> None:
    # Arrange — store dir doesn't exist at all and the pin can't resolve.
    cfg = _make_config("alpha", account="ywatanabe-scitex-ai")
    # Act — must NOT silently fall through to "use pinned anyway".
    ctx = pytest.raises(NoHealthyAccountError)
    # Assert
    with ctx:
        _rotate_to_healthy_account(cfg)
