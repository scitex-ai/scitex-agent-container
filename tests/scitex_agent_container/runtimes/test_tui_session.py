"""``TuiSessionRuntime`` — hedge runtime over the salvaged tmux modules.

Substantive impl (lead a2a 74193892, hedge for 2026-06-15 SDK cutoff):
wires ``spec.runtime: tui`` to a tmux-detached ``claude`` session sac
owns. Tests inject an in-memory ``MultiplexerProtocol`` fake (a real
class, not a mock — STX-policy bans MagicMock/monkeypatch-as-fixture-
param) so the suite runs in the CI container without requiring tmux
to be installed.

STX-TQ002 AAA-marker + STX-TQ007 one-assert. No mocks.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

import pytest

from scitex_agent_container.runtimes.tui_session import (
    TuiSessionRuntime,
    session_name_for,
)

# ---------------------------------------------------------------------------
# In-memory multiplexer: a real class that satisfies MultiplexerProtocol.
# Backed by a dict instead of subprocess — no mocks, no MagicMock.
# ---------------------------------------------------------------------------


@dataclass
class _MemorySession:
    name: str
    command: str
    workdir: str
    pane: list[str] = field(default_factory=list)
    # Unix-epoch timestamp of last simulated pane activity. The runtime's
    # is_running probe (step 4/4) reads session_activity to gate on a
    # "responsive" window; tests that want to simulate a stale session
    # mutate this field directly to back-date the stamp.
    activity_at: float = 0.0


class _MemoryMultiplexer:
    """Real MultiplexerProtocol impl backed by a class-level dict.

    Each test gets a fresh subclass via the ``mux`` fixture so two
    parallel tests can't contaminate each other's sessions (pytest
    runs tests in the same process via xdist; a global registry would
    couple them).
    """

    _sessions: dict[str, _MemorySession]
    _stop_log: list[str]

    @classmethod
    def reset(cls) -> None:
        cls._sessions = {}
        cls._stop_log = []

    @classmethod
    def exists(cls, session_name: str) -> bool:
        return session_name in cls._sessions

    @classmethod
    def start(
        cls,
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
    ) -> bool:
        cls._sessions[session_name] = _MemorySession(
            name=session_name,
            command=command,
            workdir=workdir,
            activity_at=time.time(),
        )
        return True

    @classmethod
    def stop(cls, session_name: str) -> bool:
        cls._stop_log.append(session_name)
        if session_name not in cls._sessions:
            return False
        del cls._sessions[session_name]
        return True

    @classmethod
    def capture_content(cls, session_name: str) -> str:
        sess = cls._sessions.get(session_name)
        if sess is None:
            return ""
        return "\n".join(sess.pane)

    @classmethod
    def capture_logs(cls, session_name: str, lines: int = 50) -> str:
        sess = cls._sessions.get(session_name)
        if sess is None:
            return ""
        # Seed deterministic pane content so the logs test can assert
        # a single observable: the captured text.
        return f"<pane lines={lines}>{sess.command}@{sess.workdir}"

    @classmethod
    def send_keys(cls, session_name: str, *keys: str) -> None:
        sess = cls._sessions.get(session_name)
        if sess is None:
            return
        sess.pane.extend(keys)

    @classmethod
    def send_text_and_submit(cls, session_name: str, text: str) -> None:
        sess = cls._sessions.get(session_name)
        if sess is None:
            return
        sess.pane.append(text)

    @classmethod
    def send_text_and_submit_verified(
        cls,
        session_name: str,
        text: str,
        **_: object,
    ) -> int:
        """In-memory analogue of the verified send: trivially "delivers"
        on attempt 1, since the memory backend has no Ink renderer race
        to simulate. The runtime's ``send_turn`` calls this primitive
        (lead a2a ``910ff436642948eb85f8b3100204ed9b``); the memory mux
        returns 1 to mirror the real TmuxManager's "delivered on first
        attempt" contract.
        """
        sess = cls._sessions.get(session_name)
        if sess is None:
            return 0
        sess.pane.append(text)
        return 1

    @classmethod
    def attach(cls, session_name: str) -> None:
        return None

    @classmethod
    def session_activity(cls, session_name: str) -> int | None:
        """Tui-alive probe (step 4/4): returns the last pane-activity
        epoch for ``session_name``, or ``None`` when no such session
        exists. Real ``tmux display -p '#{session_activity}'`` exposes
        this; the in-memory fake mirrors the contract so the runtime's
        ``is_running`` step-4 probe is exercised end-to-end without
        requiring tmux in the CI container.
        """
        sess = cls._sessions.get(session_name)
        if sess is None:
            return None
        return int(sess.activity_at)


@dataclass
class _Config:
    """Minimal AgentConfig surface the runtime touches.

    The real AgentConfig is much richer; the runtime only reads
    ``name`` + ``workdir``, so a dataclass with those two fields
    is a complete substitute for unit-level testing.
    """

    name: str
    workdir: str = "/tmp"


@pytest.fixture
def mux() -> Iterator[type[_MemoryMultiplexer]]:
    """Per-test fresh in-memory multiplexer class.

    Subclassing isolates the ``_sessions`` dict so xdist-parallel
    tests can't see each other's sessions.
    """

    class _PerTestMux(_MemoryMultiplexer):
        pass

    _PerTestMux.reset()
    yield _PerTestMux


# ---------------------------------------------------------------------------
# session_name_for — namespacing convention
# ---------------------------------------------------------------------------


def test_session_name_for_prefixes_with_tui_namespace() -> None:
    # Arrange
    config = _Config(name="alpha")
    # Act
    name = session_name_for(config)
    # Assert
    assert name == "tui-alpha"


# ---------------------------------------------------------------------------
# start — creates the session via the injected multiplexer
# ---------------------------------------------------------------------------


def test_tui_runtime_start_creates_named_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="beta", workdir="/tmp/beta")
    # Act
    runtime.start(config)
    # Assert
    assert mux.exists("tui-beta")


def test_tui_runtime_start_returns_true_on_success(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="gamma")
    # Act
    ok = runtime.start(config)
    # Assert
    assert ok is True


def test_tui_runtime_start_invokes_claude_binary_in_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux, claude_bin="/usr/local/bin/claude")
    config = _Config(name="delta")
    # Act
    runtime.start(config)
    # Assert
    assert mux._sessions["tui-delta"].command == "/usr/local/bin/claude"


def test_tui_runtime_start_force_stops_existing_session_first(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="epsilon")
    runtime.start(config)
    pre_stop_calls = sum(1 for _ in mux._stop_log)
    # Act
    runtime.start(config, force=True)
    # Assert — force=True triggered an extra stop before the second start.
    assert sum(1 for _ in mux._stop_log) == pre_stop_calls + 1


def test_tui_runtime_start_dry_run_does_not_create_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="zeta")
    # Act
    runtime.start(config, dry_run=True)
    # Assert
    assert mux.exists("tui-zeta") is False


def test_tui_runtime_start_dry_run_returns_true(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="eta")
    # Act
    ok = runtime.start(config, dry_run=True)
    # Assert
    assert ok is True


# ---------------------------------------------------------------------------
# stop — kills the session
# ---------------------------------------------------------------------------


def test_tui_runtime_stop_kills_named_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="theta")
    runtime.start(config)
    # Act
    runtime.stop(config)
    # Assert
    assert mux.exists("tui-theta") is False


def test_tui_runtime_stop_returns_true_when_session_existed(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="iota")
    runtime.start(config)
    # Act
    ok = runtime.stop(config)
    # Assert
    assert ok is True


def test_tui_runtime_stop_returns_false_when_no_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="kappa")
    # Act
    ok = runtime.stop(config)
    # Assert
    assert ok is False


# ---------------------------------------------------------------------------
# is_running — proxy for session existence (risk-2 deferred)
# ---------------------------------------------------------------------------


def test_tui_runtime_is_running_true_when_session_active(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="lambda")
    runtime.start(config)
    # Act
    alive = runtime.is_running(config)
    # Assert
    assert alive is True


def test_tui_runtime_is_running_false_when_session_absent(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="mu")
    # Act
    alive = runtime.is_running(config)
    # Assert
    assert alive is False


# ---------------------------------------------------------------------------
# is_running — step 4 pane-activity probe semantics
# ---------------------------------------------------------------------------


def test_tui_runtime_is_running_false_when_activity_stale_beyond_window(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — start session, then back-date activity beyond default window.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="nu-stale")
    runtime.start(config)
    mux._sessions["tui-nu-stale"].activity_at = time.time() - 9_999.0
    # Act
    alive = runtime.is_running(config)
    # Assert — pane went silent past default max-idle: probe flips False.
    assert alive is False


def test_tui_runtime_is_running_respects_max_idle_s_override(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — back-date activity 100s; tighten window to 50s.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="xi-tight")
    runtime.start(config)
    mux._sessions["tui-xi-tight"].activity_at = time.time() - 100.0
    # Act
    alive = runtime.is_running(config, max_idle_s=50.0)
    # Assert — tighter window means the same stamp is now stale.
    assert alive is False


def test_tui_runtime_is_running_false_when_session_activity_unavailable(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — a legacy multiplexer fake that lacks session_activity
    # (returns None) must NOT be silently treated as "alive". The probe
    # has to fail loud to "not responsive" so the supervisor restarts.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="omicron-legacy")
    runtime.start(config)

    # Monkey-patch the activity probe to return None for this test only,
    # simulating a multiplexer impl that doesn't expose session_activity.
    def _no_activity(_name: str) -> int | None:
        return None

    mux.session_activity = staticmethod(_no_activity)  # type: ignore[method-assign]
    # Act
    alive = runtime.is_running(config)
    # Assert
    assert alive is False


# ---------------------------------------------------------------------------
# logs — captured pane text
# ---------------------------------------------------------------------------


def test_tui_runtime_logs_returns_captured_pane_text(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux, claude_bin="claude-foo")
    config = _Config(name="nu", workdir="/data/nu")
    runtime.start(config)
    # Act
    text = runtime.logs(config, lines=10)
    # Assert
    assert text == "<pane lines=10>claude-foo@/data/nu"


def test_tui_runtime_logs_returns_empty_when_session_absent(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="xi")
    # Act
    text = runtime.logs(config)
    # Assert
    assert text == ""


# ---------------------------------------------------------------------------
# send_turn — delivery primitive (step 3/4)
# ---------------------------------------------------------------------------


def test_tui_runtime_send_turn_delivers_text_to_session_pane(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — start the session so the multiplexer has somewhere to deliver.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="omicron")
    runtime.start(config)
    # Act
    runtime.send_turn(config, "hello-omicron")
    # Assert — _MemoryMultiplexer.send_text_and_submit appends to the
    # pane list, so a single send shows up as exactly one entry.
    assert mux._sessions["tui-omicron"].pane == ["hello-omicron"]


def test_tui_runtime_send_turn_returns_true_when_session_alive(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="pi")
    runtime.start(config)
    # Act
    delivered = runtime.send_turn(config, "ping")
    # Assert
    assert delivered is True


def test_tui_runtime_send_turn_returns_false_when_no_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — no start() call; session does not exist.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="rho")
    # Act
    delivered = runtime.send_turn(config, "lost-turn")
    # Assert
    assert delivered is False


def test_tui_runtime_send_turn_skips_send_when_no_session(
    mux: type[_MemoryMultiplexer],
) -> None:
    # Arrange — no start() call.
    runtime = TuiSessionRuntime(multiplexer=mux)
    config = _Config(name="sigma")
    # Act
    runtime.send_turn(config, "lost-turn")
    # Assert — the multiplexer's session registry stays empty, proving
    # the runtime guarded the send instead of letting it create a
    # phantom entry via the implicit dict-insert path.
    assert "tui-sigma" not in mux._sessions
