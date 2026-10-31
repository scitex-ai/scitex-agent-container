#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Start-progress heartbeat: long silent launches stay narrated.

Operator report 2026-10-19: ``sac agents start`` sat silent for minutes
(boot drain / image / hooks) behind a single ``starting <name>`` line.
:func:`starting_progress` wraps the blocking launch and emits a
``still starting`` line on a cadence until the launch returns.

PA-306: no ``unittest.mock`` / ``monkeypatch`` — the ``system_msg``
sink is swapped at the module namespace with save/restore.
PA-307 §3: one assertion per test function; Arrange/Act/Assert each
on its own line.
"""

from __future__ import annotations

import re
import time
from contextlib import contextmanager
from typing import Iterator

import pytest

import scitex_agent_container.cli_pkg.lifecycle._start_progress as progress_mod
from scitex_agent_container.cli_pkg.lifecycle._start_progress import (
    starting_progress,
)


@contextmanager
def _swap_sink(recorder: list) -> Iterator[None]:
    saved = progress_mod.system_msg
    progress_mod.system_msg = lambda text, style="info": recorder.append(  # type: ignore[method-assign]
        (text, style)
    )
    try:
        yield
    finally:
        progress_mod.system_msg = saved


@pytest.fixture
def slow_launch_lines() -> list:
    # Arrange
    lines: list = []
    # Act — a launch slower than two ticks must be narrated twice.
    with _swap_sink(lines):
        with starting_progress("pytest-fixture-slow", interval_s=0.05):
            time.sleep(0.22)
    # Assert
    return lines


def test_heartbeat_narrates_a_slow_launch(slow_launch_lines):
    # Arrange
    lines = slow_launch_lines
    # Act
    count = len(lines)
    # Assert
    assert count >= 2


def test_heartbeat_names_the_agent(slow_launch_lines):
    # Arrange
    lines = slow_launch_lines
    # Act
    named = all("still starting 'pytest-fixture-slow'" in text for text, _ in lines)
    # Assert
    assert named


def test_heartbeat_is_info_level(slow_launch_lines):
    # Arrange
    lines = slow_launch_lines
    # Act
    levelled = all(style == "info" for _, style in lines)
    # Assert
    assert levelled


def test_heartbeat_quiet_for_a_fast_launch():
    # Arrange
    lines: list = []
    # Act
    # A launch quicker than one tick emits nothing.
    with _swap_sink(lines):
        with starting_progress("pytest-fixture-fast", interval_s=60.0):
            pass
    # Assert
    assert lines == []


def test_heartbeat_disabled_emits_nothing():
    # Arrange
    lines: list = []
    # Act
    # --json mode disables the heartbeat (stdout stays a pure envelope).
    with _swap_sink(lines):
        with starting_progress("pytest-fixture-json", enabled=False, interval_s=0.01):
            time.sleep(0.05)
    # Assert
    assert lines == []


@pytest.fixture
def elapsed_counters() -> list:
    # Arrange
    lines: list = []
    # Act
    with _swap_sink(lines):
        with starting_progress("pytest-fixture-elapsed", interval_s=0.05):
            time.sleep(0.16)
    # Assert
    return [
        int(m.group(1))
        for text, _ in lines
        for m in [re.search(r"(\d+)s elapsed", text)]
        if m is not None
    ]


def test_heartbeat_elapsed_counters_present(elapsed_counters):
    # Arrange
    elapsed = elapsed_counters
    # Act
    count = len(elapsed)
    # Assert
    assert count >= 2


def test_heartbeat_elapsed_counters_grow(elapsed_counters):
    # Arrange
    elapsed = elapsed_counters
    # Act
    ordered = elapsed == sorted(elapsed)
    # Assert
    assert ordered
