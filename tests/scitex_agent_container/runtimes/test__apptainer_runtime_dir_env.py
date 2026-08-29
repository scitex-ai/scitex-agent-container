#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The container must be told where beats live, or liveness is undetectable.

THE DEFECT THIS PINS, measured 2026-08-27 from inside a live agent container:

    beat_is_recent('scitex-agent-container')  -> None   # me, beat file 30s old
    beat_is_recent('paper-scitex-clew')       -> None   # a real peer
    beat_is_recent('definitely-not-an-agent') -> None   # does not exist

Live, dead and nonexistent were indistinguishable — the whole of
`sac-agent-liveness-undetectable-and-no-autoheal-20260823`, in three lines.

The cause is not the reader. `beat_is_recent` resolves
`runtime_base_dir() / name / heartbeat.json`, and `runtime_base_dir()` falls
back to `~/.scitex/agent-container/runtime` when its env var is unset. Inside
a container `~` is /home/agent, which is ephemeral and where no agent ever
writes a beat, so the lookup finds nothing and answers None HONESTLY. The
container was simply never told where the beats are.

WHY THE OBVIOUS TEST WOULD NOT HAVE CAUGHT IT. A test asserting the flag
appears in the argv passes whether or not the value is usable, and a test run
on the HOST passes either way — there `~` already IS the runtime root, so the
fallback is correct and the bug is invisible. That is how this survived long
enough to blind 107 of 123 agents.

So these drive the REAL reader against a REAL beat file on disk, with HOME
pointed at an empty directory the way a container has it. No mocks and no
monkeypatch (PA-306 / STX-NM002): the `environment` fixture sets os.environ
and restores it on teardown, which is the real variable the real resolver
reads.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._runtime_paths import runtime_base_dir
from scitex_agent_container.cli_pkg._helpers._agent_list_beat import beat_is_recent

_AGENT = "an-agent-that-is-beating"
_RUNTIME_ENV = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"


@pytest.fixture
def environment():
    """Set/unset real env vars, restoring exactly what was there before.

    A yield fixture rather than monkeypatch: the resolver reads os.environ, so
    the test should write os.environ. Teardown restores the prior value —
    including its ABSENCE, which a naive save/restore that stores "" would get
    wrong, and the resolver treats "" and unset differently.
    """
    saved: dict[str, str | None] = {}

    def apply(**pairs: str | None) -> None:
        for key, value in pairs.items():
            if key not in saved:
                saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    try:
        yield apply
    finally:
        for key, prior in saved.items():
            if prior is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prior


@pytest.fixture
def beating_agent(tmp_path):
    """A real runtime root holding one agent whose beat was written just now."""
    agent_dir = tmp_path / "runtime" / _AGENT
    agent_dir.mkdir(parents=True)
    (agent_dir / "heartbeat.json").write_text("{}")
    return tmp_path / "runtime"


def test_the_reader_is_blind_when_home_is_the_containers_own(
    beating_agent, tmp_path, environment
):
    """THE DEFECT. Container HOME holds no beats, so the fallback finds none.

    This assertion fails on a fixed container and passes on a broken one — it
    pins the SYMPTOM, so a fix has to change something real rather than move a
    string around in the argv.
    """
    # Arrange
    empty_home = tmp_path / "container-home"
    (empty_home / ".scitex" / "agent-container" / "runtime").mkdir(parents=True)
    # Act
    environment(HOME=str(empty_home), **{_RUNTIME_ENV: None})
    # Assert — None, not False: no evidence either way (three-valued contract).
    assert beat_is_recent(_AGENT) is None


def test_the_env_var_makes_the_same_reader_see_the_beat(
    beating_agent, tmp_path, environment
):
    """THE FIX. Same reader, same beat file, told where to look."""
    # Arrange
    empty_home = tmp_path / "container-home-2"
    empty_home.mkdir()
    # Act
    environment(HOME=str(empty_home), **{_RUNTIME_ENV: str(beating_agent)})
    # Assert
    assert beat_is_recent(_AGENT) is True


def test_a_name_with_no_beat_file_stays_unknown(beating_agent, environment):
    """CONTROL. Without this, the test above could pass by always saying True."""
    # Arrange
    environment(**{_RUNTIME_ENV: str(beating_agent)})
    # Act
    verdict = beat_is_recent("a-name-that-was-never-an-agent")
    # Assert
    assert verdict is None


def test_a_stale_beat_reads_false_rather_than_unknown(beating_agent, environment):
    """The other pole, so `None` and `False` are not being conflated.

    `False` means "the file is old"; `None` means "there is no file". A reader
    returning None for both would satisfy every other test in this file.
    """
    # Arrange
    beat = beating_agent / _AGENT / "heartbeat.json"
    a_day_ago = time.time() - 86_400
    os.utime(beat, (a_day_ago, a_day_ago))
    # Act
    environment(**{_RUNTIME_ENV: str(beating_agent)})
    # Assert
    assert beat_is_recent(_AGENT) is False


def test_the_resolver_honours_the_variable_the_launcher_sets(
    beating_agent, environment
):
    """The launcher writes this variable; the resolver must read THAT one.

    Named separately because the two halves live in different modules, and a
    launcher setting a variable nothing reads is this card's own failure one
    level up.
    """
    # Arrange
    environment(**{_RUNTIME_ENV: str(beating_agent)})
    # Act
    resolved = runtime_base_dir()
    # Assert
    assert resolved == Path(str(beating_agent))


# EOF
