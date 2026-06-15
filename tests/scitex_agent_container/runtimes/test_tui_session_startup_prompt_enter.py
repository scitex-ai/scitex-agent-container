"""Bug regression: startup_prompts must arrive submitted, not pasted.

P0 operator-reported (2026-06-15): figrecipe + todo + neurovista all
started but stalled at boot because ``_inject_startup_prompts`` pasted
``spec.claude.startup_prompts`` into the TUI input field *without
pressing Enter*. The agent looked stopped to ``sac``: the tmux pane was
alive but the prompt sat unsent on the input line, no SDK turn ever
fired, no a2a, no telegram reply. Lead recovered each agent by
attaching tmux and hitting Enter manually.

This module pins TWO guarantees on the inject path:

  1. The runtime gates the inject on ``wait_until_input_ready`` — the
     prompt MUST NOT land while claude's input field is still mounting
     (the Ink-drop window where the first Enter can be silently eaten).
  2. A defensive trailing ``Enter`` keystroke fires after
     ``send_text_and_submit`` — belt-and-suspenders against the same
     Ink-drop race that ``send_text_and_submit_verified`` exists to
     defeat for ``send_turn``. The startup inject was never wired to the
     verified primitive, so a separate post-submit Enter is the minimum
     fix that does not refactor the inject around the verified path.

Tests use a real in-memory ``MultiplexerProtocol`` (no MagicMock,
no monkeypatch-as-fixture-param) — STX-TQ002 AAA / STX-TQ007 one-assert
/ PA-306 no-mock-fixtures.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

import pytest

from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime


@dataclass
class _MemSession:
    name: str
    command: str = ""
    workdir: str = "/tmp"
    pane: list[str] = field(default_factory=list)
    activity_at: float = 0.0


class _RecordingMux:
    """Records every multiplexer call in arrival order.

    Each `send_*` / `capture_*` call appends a structured tuple to
    ``calls``. Tests assert on that list — it lets us pin the EXACT
    sequence of keystrokes the runtime drives, which is what matters for
    the unsubmitted-prompt bug (the existence of a trailing ``Enter``
    AFTER the text+submit primitive is the load-bearing invariant).
    """

    _sessions: dict[str, _MemSession]
    _calls: list[tuple]
    _input_ready: bool

    @classmethod
    def reset(cls) -> None:
        cls._sessions = {}
        cls._calls = []
        cls._input_ready = True

    @classmethod
    def exists(cls, name: str) -> bool:
        return name in cls._sessions

    @classmethod
    def start(
        cls,
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
        session_env: dict[str, str] | None = None,
    ) -> bool:
        del env_exports, venv, session_env
        cls._sessions[session_name] = _MemSession(
            name=session_name,
            command=command,
            workdir=workdir,
            activity_at=time.time(),
        )
        return True

    @classmethod
    def stop(cls, name: str) -> bool:
        return cls._sessions.pop(name, None) is not None

    @classmethod
    def capture_content(cls, name: str) -> str:
        # Always report input-ready so the wait_until_input_ready gate
        # in the inject path resolves the same way the running TUI would
        # once claude has mounted its input field.
        return "? for shortcuts" if cls._input_ready else "still booting"

    @classmethod
    def capture_logs(cls, name: str, lines: int = 50) -> str:
        return ""

    @classmethod
    def send_keys(cls, name: str, *keys: str) -> None:
        cls._calls.append(("send_keys", name, tuple(keys)))
        sess = cls._sessions.get(name)
        if sess is not None:
            sess.pane.extend(keys)

    @classmethod
    def send_text_and_submit(cls, name: str, text: str) -> None:
        cls._calls.append(("send_text_and_submit", name, text))
        sess = cls._sessions.get(name)
        if sess is not None:
            sess.pane.append(text)

    @classmethod
    def send_text_and_submit_verified(cls, name: str, text: str, **_: object) -> int:
        cls._calls.append(("send_text_and_submit_verified", name, text))
        sess = cls._sessions.get(name)
        if sess is not None:
            sess.pane.append(text)
        return 1

    @classmethod
    def attach(cls, name: str) -> None:
        return None

    @classmethod
    def session_activity(cls, name: str) -> int | None:
        sess = cls._sessions.get(name)
        return int(sess.activity_at) if sess is not None else None


@dataclass
class _Config:
    name: str
    workdir: str = "/tmp"
    startup_prompts: list[str] = field(default_factory=list)


def _builder(_config: _Config) -> list[str]:
    return ["apptainer", "exec", "img.sif", "claude"]


@pytest.fixture
def mux() -> Iterator[type[_RecordingMux]]:
    class _PerTestMux(_RecordingMux):
        pass

    _PerTestMux.reset()
    yield _PerTestMux


def test_startup_prompt_inject_fires_text_submit_primitive(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — one startup prompt, ready mux.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="figrecipe", startup_prompts=["go work"])
    # Act — full start path runs the inject under the runtime's own gate.
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — the prompt text reached the multiplexer's text-submit
    # primitive (NOT bare ``send_keys`` with a trailing ``\r``).
    text_calls = [c for c in mux._calls if c[0] == "send_text_and_submit"]
    assert text_calls == [("send_text_and_submit", "tui-figrecipe", "go work")]


def test_startup_prompt_inject_appends_defensive_enter_after_submit(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — the bug: operator saw the prompt pasted but Enter never
    # arrived. The fix MUST issue an explicit ``Enter`` keystroke AFTER
    # the text-submit primitive returns, because the Ink TUI can eat the
    # primitive's own Enter while it's still mounting the input.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="todo", startup_prompts=["start"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — find the text-submit; the very next call must be an
    # explicit ``send_keys("Enter")`` against the same session.
    seq = [
        (c[0], c[1], c[2])
        for c in mux._calls
        if c[0] in ("send_text_and_submit", "send_keys") and c[1] == "tui-todo"
    ]
    # Expect the text-submit immediately followed by a bare Enter.
    submit_idx = next(i for i, c in enumerate(seq) if c[0] == "send_text_and_submit")
    next_call = seq[submit_idx + 1]
    assert next_call == ("send_keys", "tui-todo", ("Enter",))


def test_startup_prompt_inject_waits_for_input_ready_before_sending(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — capture_content returns the not-ready string. The
    # inject MUST refuse to fire the text-submit while the TUI's input
    # field has not yet bound (the operator's bug was the prompt landing
    # on a not-yet-bound field, so the Enter dropped on the floor).
    mux._input_ready = False
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="neurovista", startup_prompts=["mission"])
    # Act — call start with a tiny boot-drain so the test doesn't hang.
    # The inject's own readiness gate has a short timeout (per-prompt
    # best-effort: failure logs + skips rather than raises).
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — text-submit was NOT called because input never became
    # ready (the runtime's gate raised TuiInputNotReadyError, which the
    # per-prompt best-effort handler logged + swallowed).
    text_calls = [c for c in mux._calls if c[0] == "send_text_and_submit"]
    assert text_calls == []


def test_startup_prompt_inject_skipped_when_list_empty(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — no startup_prompts.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="quiet", startup_prompts=[])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — no text-submit + no defensive Enter for an empty list.
    text_calls = [c for c in mux._calls if c[0] == "send_text_and_submit"]
    enter_calls = [c for c in mux._calls if c[0] == "send_keys" and c[2] == ("Enter",)]
    assert (text_calls, enter_calls) == ([], [])


def test_startup_prompt_inject_each_prompt_gets_defensive_enter(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — two prompts; BOTH must be submitted AND followed by an
    # explicit defensive Enter (the second prompt is no less prone to
    # the Ink-drop race than the first).
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(
        name="multi",
        startup_prompts=["first turn", "second turn"],
    )
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — exactly 2 text-submits + 2 defensive Enters.
    text_calls = [c for c in mux._calls if c[0] == "send_text_and_submit"]
    enter_calls = [c for c in mux._calls if c[0] == "send_keys" and c[2] == ("Enter",)]
    assert (len(text_calls), len(enter_calls)) == (2, 2)
