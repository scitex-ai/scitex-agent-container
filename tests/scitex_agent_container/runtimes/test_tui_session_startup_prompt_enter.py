"""Bug regression: startup_prompts must arrive SUBMITTED, not pasted.

P0 operator-reported (2026-06-15 → recurring): figrecipe + todo + neurovista
all started but stalled at boot because ``_inject_startup_prompts`` left
``spec.startup_prompts`` pasted into the TUI input field *without pressing
Enter*. The agent looked stopped to ``sac``: the tmux pane was alive but the
prompt sat unsent on the input line, no turn ever fired, no a2a, no telegram.

Root cause (card sac-tui-startup-prompt-enter-drop): the injection did not
follow the containerized Ink/React ``claude`` TUI's proven keystroke contract
(``_skills/scitex-agent-container/45_agent-to-agent-recovery-tmux.md``):

  1. the prompt TEXT was pasted with NON-literal ``send-keys`` (no ``-l``), so
     the Ink TUI could silently drop it; and
  2. the submit ``Enter`` fired on a blind fixed sleep (inside
     ``send_text_and_submit``) plus a second ungated defensive ``Enter`` —
     BOTH landing in the pane's BUSY/initialising window where the Ink TUI
     eats Enter — BEFORE the idle-gated verify ran.

The fix pins TWO guarantees on the inject path:

  1. the prompt is pasted LITERALLY (``send_text_literal`` → ``send-keys -l``),
     never non-literally; and
  2. the ONLY ``Enter`` is the idle-gated one from
     ``verify_submit_by_advancement`` (wait-for-idle → one Enter → verify the
     buffer advanced → bounded retry → fail loud). No blind/defensive Enter.

Tests use a real in-memory ``MultiplexerProtocol`` whose pane models the live
TUI (pending after the literal paste, cleared once an Enter lands) — no
MagicMock, no monkeypatch-as-fixture-param (STX-TQ002 AAA / STX-TQ007
one-assert / PA-306 no-mock-fixtures).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterator

import pytest

from scitex_agent_container._runners._tmux.tmux import TuiInputNotReadyError
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

# Live-TUI pane snapshots. Both carry the input-ready marker (``? for
# shortcuts``) + the idle status bar (``bypass permissions``) so the runtime's
# readiness gate resolves; they differ only in the compose box: PENDING holds
# ``❯\xa0<text>`` (NBSP gap, as Claude's Ink TUI renders a paste), CLEARED holds
# an empty ``❯``.
_STATUS = "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents"
_MARKER = "? for shortcuts"
_CLEARED_PANE = f"❯ \n{_STATUS}\n{_MARKER}"


def _pending_pane(text: str) -> str:
    return f"❯\xa0{text}\n{_STATUS}\n{_MARKER}"


@dataclass
class _MemSession:
    name: str
    command: str = ""
    workdir: str = "/tmp"
    # The unsent compose buffer: set by ``send_text_literal``, cleared when an
    # ``Enter`` lands — exactly the live TUI's submit semantics.
    pending: str | None = None
    activity_at: float = 0.0


class _RecordingMux:
    """Records every multiplexer call in arrival order AND models the pane.

    ``send_text_literal`` sets the pending compose buffer; a subsequent
    ``send_keys("Enter")`` clears it (submission landed). ``capture_content``
    renders the pending-vs-cleared pane accordingly, so the runtime's
    idle-gated ``verify_submit_by_advancement`` is exercised end-to-end: it
    waits for the paste to render, sends ONE Enter once idle, and observes the
    buffer advance.
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
        if not cls._input_ready:
            return "still booting"
        sess = cls._sessions.get(name)
        if sess is not None and sess.pending:
            return _pending_pane(sess.pending)
        return _CLEARED_PANE

    @classmethod
    def capture_logs(cls, name: str, lines: int = 50) -> str:
        return ""

    @classmethod
    def send_keys(cls, name: str, *keys: str) -> None:
        cls._calls.append(("send_keys", name, tuple(keys)))
        sess = cls._sessions.get(name)
        if sess is not None and "Enter" in keys:
            # Enter submits the pending compose buffer → it clears.
            sess.pending = None

    @classmethod
    def send_text_literal(cls, name: str, text: str) -> None:
        cls._calls.append(("send_text_literal", name, text))
        sess = cls._sessions.get(name)
        if sess is not None:
            sess.pending = text

    @classmethod
    def send_text_and_submit(cls, name: str, text: str) -> None:
        # Present for MultiplexerProtocol parity; the inject path no longer
        # uses it (it pastes literally + idle-gated submit). Recorded so a
        # test can assert it is NOT used on the boot inject path.
        cls._calls.append(("send_text_and_submit", name, text))
        sess = cls._sessions.get(name)
        if sess is not None:
            sess.pending = None

    @classmethod
    def send_text_and_submit_verified(cls, name: str, text: str, **_: object) -> int:
        cls._calls.append(("send_text_and_submit_verified", name, text))
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


def _literal_calls(mux: type[_RecordingMux]) -> list[tuple]:
    return [c for c in mux._calls if c[0] == "send_text_literal"]


def _enter_calls(mux: type[_RecordingMux]) -> list[tuple]:
    return [c for c in mux._calls if c[0] == "send_keys" and c[2] == ("Enter",)]


def test_startup_prompt_inject_pastes_text_literally(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — one startup prompt, ready mux.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="figrecipe", startup_prompts=["go work"])
    # Act — full start path runs the inject under the runtime's own gate.
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — the prompt reached the LITERAL (``-l``) paste primitive, once.
    assert _literal_calls(mux) == [("send_text_literal", "tui-figrecipe", "go work")]


def test_startup_prompt_inject_never_uses_non_literal_submit(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — the old bug pasted via the coupled (blind-Enter)
    # ``send_text_and_submit``; the fix must NOT use it on the boot inject.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="todo", startup_prompts=["start"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — no coupled text-submit was used on the inject path.
    non_literal = [c for c in mux._calls if c[0] == "send_text_and_submit"]
    assert non_literal == []


def test_startup_prompt_inject_submits_via_single_idle_gated_enter(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — the paste renders as pending; the idle-gated verify must send
    # exactly ONE Enter, which clears the buffer (submission verified).
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="neurovista", startup_prompts=["mission"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — exactly one Enter (no blind + no defensive; just the gated one).
    assert len(_enter_calls(mux)) == 1


def test_startup_prompt_inject_pastes_before_it_submits(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — order matters: the literal paste must precede the submit Enter
    # (submitting first would fire into an empty prompt).
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="order", startup_prompts=["do it"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — first literal paste arrives before the first Enter.
    kinds = [c[0] for c in mux._calls if c[0] in ("send_text_literal", "send_keys")]
    first_paste = kinds.index("send_text_literal")
    first_enter = next(i for i, k in enumerate(kinds) if k == "send_keys")
    assert first_paste < first_enter


def test_startup_prompt_inject_does_not_paste_when_input_never_ready(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — a runtime whose readiness gate NEVER resolves (raises). The
    # inject MUST refuse to paste onto a not-yet-bound input (the operator's
    # bug: the prompt landing on an unbound field, Enter dropped). A real
    # subclass forces the condition deterministically (no mock, no 60s hang).
    class _NeverReadyRuntime(TuiSessionRuntime):
        def wait_until_input_ready(self, config, **_kw):  # type: ignore[override]
            raise TuiInputNotReadyError("input never bound (test)")

    runtime = _NeverReadyRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="wedged", startup_prompts=["mission"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — nothing was pasted because the input never became ready.
    assert _literal_calls(mux) == []


def test_startup_prompt_inject_skipped_when_list_empty(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — no startup_prompts.
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="quiet", startup_prompts=[])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — nothing pasted and nothing submitted for an empty list.
    assert (_literal_calls(mux), _enter_calls(mux)) == ([], [])


def test_startup_prompt_inject_each_prompt_pasted_and_submitted(
    mux: type[_RecordingMux],
) -> None:
    # Arrange — two prompts; BOTH must be pasted literally AND submitted via
    # the idle-gated Enter (the second is no less prone to the drop than the
    # first).
    runtime = TuiSessionRuntime(multiplexer=mux, command_builder=_builder)
    config = _Config(name="multi", startup_prompts=["first turn", "second turn"])
    # Act
    runtime.start(config, boot_drain_timeout_s=0.01)
    # Assert — exactly 2 literal pastes + 2 idle-gated Enters.
    assert (len(_literal_calls(mux)), len(_enter_calls(mux))) == (2, 2)
