"""``instances.screen`` is populated with the tmux session the start created.

ROOT CAUSE (P0, 2026-08-14, scitex-compute-04). ``record_instance_start``
has always accepted ``screen=`` and NO caller ever passed it, so the column
was NULL on every locally-started row. ``screen`` is the only column in
``instances`` naming something the OS can be asked about INDEPENDENTLY of
sac's own bookkeeping — with it NULL, every "did this agent cycle?"
question could only be answered by re-reading the rows sac itself had just
written. That is an echo, not evidence.

What it cost: three ``instances`` rows were minted for one agent that
night, all with ``screen`` NULL and pids matching no live process, while
the ONE real tmux session (``tui-scitex-agent-container``) had been alive
and untouched since the previous day. ``sac agents stop`` and ``sac agents
restart`` reported success over it FOUR times without touching the
process — and the maintenance path then refused the overlay-venv repair
with ``agent_not_running``, so the corruption it was there to fix could
not be fixed by sac at all.

THE NAIVE FIX IS WRONG: re-deriving ``tui-<name>`` at record time is free
to drift from what the runtime actually launched, and a drifted name
probes an empty answer that reads exactly like a dead agent. The value
must come from the runtime that just started the session
(:meth:`runtimes.base.RuntimeBase.session_name`), so the recorded string
is THE SAME one passed to ``tmux new-session -s``.

Real on-disk SQLite state.db (env-overridden per test) and the REAL
``TuiSessionRuntime``. No mocks.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env (explicit save/restore)."""
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


class _SessionRuntime:
    """Honest runtime collaborator implementing the ``RuntimeBase`` seam.

    Mirrors the real runtimes' shape: a ``_state_dir`` resolver plus the
    ``session_name`` accessor. ``start`` re-reads the attribute so a restart
    can hand back a DIFFERENT session, exactly as a respawned tmux session
    would.
    """

    def __init__(self, root: Path, session: object) -> None:
        self._root = root
        self.session = session

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name

    def session_name(self, config: AgentConfig):
        del config
        return self.session

    def start(self, config: AgentConfig) -> bool:
        del config
        return True


class _LegacyRuntime:
    """A runtime predating the ``session_name`` seam (back-compat guard)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _state_dir(self, config: AgentConfig) -> Path:
        return self._root / config.name


class _ExplodingRuntime(_SessionRuntime):
    """A runtime whose session probe raises — a start must never be blocked."""

    def session_name(self, config: AgentConfig):
        del config
        raise RuntimeError("tmux probe blew up")


def _row_for(name: str) -> dict:
    from scitex_agent_container._state.state_db import list_active_instances

    return [r for r in list_active_instances() if r["name"] == name][0]


# ---------------------------------------------------------------------------
# record_local_instance now persists the runtime's session name
# ---------------------------------------------------------------------------


def test_record_local_instance_persists_runtime_session_name(pg_schema, db_path, tmp_path) -> None:
    # Arrange — the runtime reports the session it just launched into.
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="screen-1", runtime="tui")
    # Act
    record_local_instance(cfg, _SessionRuntime(tmp_path, "tui-screen-1"))
    # Assert — THE regression guard. Pre-fix this column was NULL on every row.
    assert _row_for("screen-1")["screen"] == "tui-screen-1"


def test_record_local_instance_leaves_screen_null_for_legacy_runtime(
    pg_schema: str,
    db_path, tmp_path
) -> None:
    # Arrange — a runtime without the seam must not fabricate a session name.
    from scitex_agent_container._lifecycle._instances import record_local_instance

    cfg = AgentConfig(name="screen-legacy", runtime="apptainer")
    # Act
    record_local_instance(cfg, _LegacyRuntime(tmp_path))
    # Assert — NULL is the honest "this runtime has no session to name".
    assert _row_for("screen-legacy")["screen"] is None


def test_restart_and_record_refreshes_the_session_name(pg_schema, db_path, tmp_path) -> None:
    # Arrange — the supervisor restarts the agent into a NEW tmux session; a
    # STALE session name is worse than none, because the verifier would then
    # compare a live session against a name that no longer refers to it.
    from scitex_agent_container._lifecycle._instances import (
        record_local_instance,
        restart_and_record,
    )

    cfg = AgentConfig(name="screen-restart", runtime="tui")
    rt = _SessionRuntime(tmp_path, "tui-screen-restart-old")
    record_local_instance(cfg, rt)
    rt.session = "tui-screen-restart-new"
    # Act
    restart_and_record(cfg, lambda _c: rt)
    # Assert
    assert _row_for("screen-restart")["screen"] == "tui-screen-restart-new"


# ---------------------------------------------------------------------------
# _runtime_session_name: only a real, non-empty string is a session name
# ---------------------------------------------------------------------------


def test_runtime_session_name_rejects_a_blank_name(tmp_path) -> None:
    # Arrange — a whitespace-only name would be probed against tmux and always
    # miss, which reads exactly like a dead agent.
    from scitex_agent_container._lifecycle._instances import _runtime_session_name

    cfg = AgentConfig(name="screen-blank", runtime="tui")
    # Act
    resolved = _runtime_session_name(cfg, _SessionRuntime(tmp_path, "   "))
    # Assert
    assert resolved is None


def test_runtime_session_name_rejects_a_non_string(tmp_path) -> None:
    # Arrange — the column is TEXT; a non-string would stringify into a name
    # no tmux server has ever heard of.
    from scitex_agent_container._lifecycle._instances import _runtime_session_name

    cfg = AgentConfig(name="screen-nonstr", runtime="tui")
    # Act
    resolved = _runtime_session_name(cfg, _SessionRuntime(tmp_path, 12345))
    # Assert
    assert resolved is None


def test_runtime_session_name_survives_a_raising_probe(tmp_path) -> None:
    # Arrange — a session-name hiccup must degrade to "unknown", never abort
    # the agent start it is only annotating.
    from scitex_agent_container._lifecycle._instances import _runtime_session_name

    cfg = AgentConfig(name="screen-boom", runtime="tui")
    # Act
    resolved = _runtime_session_name(cfg, _ExplodingRuntime(tmp_path, "tui-x"))
    # Assert
    assert resolved is None


# ---------------------------------------------------------------------------
# The REAL runtime implements the seam, and returns the SAME string it hands
# to ``tmux new-session -s`` — the drift guard the whole fix rests on.
# ---------------------------------------------------------------------------


def test_base_runtime_session_name_defaults_to_none() -> None:
    # Arrange — a runtime with no multiplexer at all (SDK / apptainer / docker,
    # or SSHRemote whose session lives on another host) inherits the base
    # default rather than fabricating a name.
    from scitex_agent_container.runtimes.base import RuntimeBase

    cfg = AgentConfig(name="base-default", runtime="apptainer")
    # Act
    session = RuntimeBase.session_name(object(), cfg)  # type: ignore[arg-type]
    # Assert
    assert session is None


def test_real_tui_runtime_names_its_own_session() -> None:
    # Arrange — the production runtime, not a stand-in.
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = AgentConfig(name="real-tui", runtime="tui")
    # Act
    session = TuiSessionRuntime().session_name(cfg)
    # Assert
    assert session == "tui-real-tui"


def test_real_tui_runtime_session_name_matches_the_launch_convention() -> None:
    # Arrange — the recorded name must be THE SAME call ``start`` passes to
    # ``tmux new-session -s``. Asserting against ``session_name_for`` (rather
    # than against a literal) is what makes this a DRIFT guard: if the launch
    # convention ever moves, this fails instead of silently recording a name
    # that no longer names anything.
    from scitex_agent_container.runtimes.tui_session import (
        TuiSessionRuntime,
        session_name_for,
    )

    cfg = AgentConfig(name="drift-guard", runtime="tui")
    # Act
    session = TuiSessionRuntime().session_name(cfg)
    # Assert
    assert session == session_name_for(cfg)


def test_real_tui_runtime_session_name_reaches_the_recorder(tmp_path) -> None:
    # Arrange — the seam is only worth anything if the RECORDER accepts what
    # the REAL runtime returns; the two stand-in tests above would both pass
    # over a runtime the recorder rejects.
    from scitex_agent_container._lifecycle._instances import _runtime_session_name
    from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

    cfg = AgentConfig(name="real-recorded", runtime="tui")
    # Act
    resolved = _runtime_session_name(cfg, TuiSessionRuntime())
    # Assert
    assert resolved == "tui-real-recorded"
