"""Tests for context_manager module."""

from __future__ import annotations

import logging

import pytest

from scitex_agent_container.config import ContextManagementConfig
from scitex_agent_container.context_manager import (
    ContextManager,
    parse_context_percent,
)


SAMPLE_STATUSLINE = """\
some prior output line
another line
[claude-opus-4-6] ctx 73% | tokens 110k/200k | 2m idle
$
"""


def test_parse_context_percent_finds_statusline():
    assert parse_context_percent(SAMPLE_STATUSLINE) == 73.0


def test_parse_context_percent_none_when_missing():
    assert parse_context_percent("no percent sign here\njust text\n") is None


def test_parse_context_percent_empty():
    assert parse_context_percent("") is None


def _make_cm(percent_text: str, **cfg_overrides) -> tuple[ContextManager, list]:
    calls: list[tuple[str, object]] = []

    def dispatcher(strategy, agent_config):
        calls.append((strategy, agent_config))

    def capture(_session: str) -> str:
        return percent_text

    kwargs = dict(
        trigger_at_percent=70.0,
        strategy="compact",
        warn_before_n_checks=0,
        check_interval_seconds=1,
    )
    kwargs.update(cfg_overrides)
    cfg = ContextManagementConfig(**kwargs)
    cm = ContextManager(
        agent_name="test-agent",
        session_name="test-session",
        config=cfg,
        dispatcher=dispatcher,
        capture=capture,
    )
    return cm, calls


def test_tick_below_threshold_does_not_dispatch():
    cm, calls = _make_cm("[model] ctx 42% | foo\n")
    percent = cm.tick()
    assert percent == 42.0
    assert calls == []


def test_tick_above_threshold_dispatches_compact():
    cm, calls = _make_cm("[model] ctx 85% | foo\n")
    percent = cm.tick()
    assert percent == 85.0
    assert len(calls) == 1
    assert calls[0][0] == "compact"


def test_tick_at_threshold_dispatches():
    cm, calls = _make_cm("[model] ctx 70% | foo\n")
    cm.tick()
    assert len(calls) == 1


def test_tick_latches_until_drop():
    cm, calls = _make_cm("[model] ctx 85% | foo\n")
    cm.tick()
    cm.tick()
    cm.tick()
    # Latched: only one dispatch despite repeated high readings
    assert len(calls) == 1


def test_warn_window_emits_warning(caplog):
    cm, calls = _make_cm(
        "[model] ctx 65% | foo\n",
        warn_before_n_checks=2,
    )
    with caplog.at_level(logging.WARNING, logger="scitex_agent_container.context_manager"):
        cm.tick()
    # Below threshold, inside the 10% headroom window → warn, no dispatch
    assert calls == []
    assert any(
        "approaching threshold" in rec.message for rec in caplog.records
    ), f"expected warn log, got: {[r.message for r in caplog.records]}"


def test_tick_no_percent_returns_none():
    cm, calls = _make_cm("no statusline here\n")
    assert cm.tick() is None
    assert calls == []
