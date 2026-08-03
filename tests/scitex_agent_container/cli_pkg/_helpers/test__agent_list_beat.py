"""A LIVE agent with no registry row must not render as ``defined``.

Measured on the live fleet 2026-08-03: five agents were beating while
``sac agents list`` called them ``defined`` — one of them the host agent
running the query. ``defined`` is synthesized for "spec on disk, absent
from the registry", and the source asserted "These agents are never LIVE".
An absent registry row is not a stopped agent.

The heartbeat FILE MTIME is the signal, not the ``ts`` field inside it:
``write_heartbeat`` takes a ``ts`` override and the TUI runner passes the
tmux pane-activity epoch, so ``ts`` read 3.4h stale on a file written that
same second. See ``_agent_list_beat`` for the measurements.

The beat files here go into the ALREADY-SANDBOXED runtime root that the
conftest points every worker at, rather than relocating the root: moving
it trips the state-floor teardown guard, whose whole purpose is to catch a
test writing into the operator's real state.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._runtime_paths import runtime_base_dir
from scitex_agent_container.cli_pkg._helpers._agent_list_beat import (
    RECENT_BEAT_MAX_AGE_S,
    beat_is_recent,
)
from scitex_agent_container.cli_pkg._helpers._agent_list_discover import (
    defined_agent_rows,
)

AGENT = "beat-probe-agent"


def _write_beat(state_dir: Path, *, age_s: float = 0.0) -> Path:
    """A real heartbeat.json whose MTIME is ``age_s`` seconds old.

    The ``ts`` field is deliberately stamped in 1970 on every beat written
    here — that mirrors the real-world shape (a stale pane-activity epoch)
    and keeps any implementation that reads ``ts`` instead of the mtime
    from passing these tests by accident.
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    beat = state_dir / "heartbeat.json"
    beat.write_text('{"state": "running", "ts": 1000000.0, "pid": 0}')
    if age_s:
        old = time.time() - age_s
        os.utime(beat, (old, old))
    return beat


@pytest.fixture
def beating_state_dir():
    """This agent's REAL resolved state dir, beating, removed on teardown."""
    state_dir = runtime_base_dir() / AGENT
    _write_beat(state_dir)
    yield state_dir
    (state_dir / "heartbeat.json").unlink(missing_ok=True)
    # stx-allow: fallback (reason: teardown must not fail the run if the
    # sandbox dir was already reaped by another fixture)
    try:
        state_dir.rmdir()
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        pass


@pytest.fixture
def silent_state_dir():
    """The same agent with NO beat file — the ordinary defined case."""
    state_dir = runtime_base_dir() / AGENT
    beat = state_dir / "heartbeat.json"
    beat.unlink(missing_ok=True)
    yield state_dir


def _spec_for(tmp_path: Path) -> Path:
    """A real, fully-explicit v3 spec (red-start ruling 2026-07-21)."""
    from tests.scitex_agent_container._helpers.explicit_spec import (
        explicitize_yaml,
    )

    spec = tmp_path / "agents" / AGENT / "spec.yaml"
    spec.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(
        [
            "apiVersion: scitex-agent-container/v3",
            "kind: Agent",
            "metadata: {}",
            "spec:",
            "  runtime: apptainer",
            "  host: ${HOSTNAME}",
            "  workdir: /home/agent/work",
            "  apptainer:",
            "    image: /x.sif",
            "    binds: []",
            "  claude:",
            "    model: sonnet",
            "  health:",
            "    enabled: true",
            "    interval: 60",
            "  restart:",
            "    policy: on-failure",
            "    max_retries: 3",
        ]
    )
    spec.write_text(explicitize_yaml(body + "\n"))
    return spec


def _rows(tmp_path: Path) -> list[dict]:
    """Rows for one on-disk agent the registry has never heard of."""
    spec = _spec_for(tmp_path)
    return defined_agent_rows(
        registered=set(),  # the whole point: NO registry row
        port_claims={},
        display_host="test-host",
        discover=lambda: [(AGENT, spec)],
    )


# ---------------------------------------------------------------------------
# 1. The signal itself — three-valued, keyed on the mtime.
# ---------------------------------------------------------------------------


def test_beat_written_now_is_evidence_of_life(tmp_path):
    # Arrange — a beat file written this instant.
    _write_beat(tmp_path)
    # Act
    verdict = beat_is_recent("anything", state_dir=tmp_path)
    # Assert
    assert verdict is True


def test_stale_beat_is_not_evidence_of_life(tmp_path):
    # Arrange — the fossil shape: last write long ago, still says "running".
    _write_beat(tmp_path, age_s=RECENT_BEAT_MAX_AGE_S * 100)
    # Act
    verdict = beat_is_recent("anything", state_dir=tmp_path)
    # Assert
    assert verdict is False


def test_absent_beat_file_is_unknown_not_false(tmp_path):
    # Arrange — a state dir that exists but was never beaten into.
    (tmp_path / "empty").mkdir()
    # Act
    verdict = beat_is_recent("anything", state_dir=tmp_path / "empty")
    # Assert — None, never False: "no file" is no evidence, not a verdict.
    assert verdict is None


def test_beat_stamped_slightly_in_the_future_still_counts(tmp_path):
    # Arrange — clock skew between the writing host and this reader.
    _write_beat(tmp_path, age_s=-30.0)
    # Act
    verdict = beat_is_recent("anything", state_dir=tmp_path)
    # Assert — a beat that just happened is a beat, skew notwithstanding.
    assert verdict is True


def test_stale_ts_field_does_not_defeat_a_fresh_write(tmp_path):
    # Arrange — the exact live-fleet shape: ts ancient, file written now.
    _write_beat(tmp_path)
    # Act
    verdict = beat_is_recent("anything", state_dir=tmp_path)
    # Assert — an implementation reading ts (epoch 1970) answers False here.
    assert verdict is True


def test_resolver_finds_the_beat_without_an_explicit_state_dir(
    beating_state_dir,
):
    # Arrange — the beat sits in the REAL resolved runtime root.
    del beating_state_dir
    # Act — no state_dir override: force the real resolver to run.
    verdict = beat_is_recent(AGENT)
    # Assert
    assert verdict is True


# ---------------------------------------------------------------------------
# 2. THE REGRESSION: the row that made this necessary. On develop a beating
#    agent with no registry row renders status="defined".
# ---------------------------------------------------------------------------


def test_beating_agent_absent_from_registry_is_not_called_defined(
    tmp_path, beating_state_dir
):
    # Arrange — an agent beating right now, unknown to the registry.
    del beating_state_dir
    # Act
    rows = _rows(tmp_path)
    # Assert — "defined" would contradict a file written this second.
    assert rows[0]["status"] != "defined"


def test_beating_agent_is_marked_liveness_unknown(tmp_path, beating_state_dir):
    # Arrange
    del beating_state_dir
    # Act
    rows = _rows(tmp_path)
    # Assert — the flag the remote-probe path uses for "could not tell".
    assert rows[0].get("liveness_unknown") is True


def test_silent_agent_absent_from_registry_still_renders_defined(
    tmp_path, silent_state_dir
):
    # Arrange — no beat file at all: the ordinary defined case.
    del silent_state_dir
    # Act
    rows = _rows(tmp_path)
    # Assert — positive-only. Absence of a beat changes nothing.
    assert rows[0]["status"] == "defined"
