"""Hermes' official TUI adapter uses the shared tmux runtime boundary."""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._claude_spec import ClaudeSpec
from scitex_agent_container.config._harness_callables import _hermes_tui_inner_argv
from scitex_agent_container.runtimes.hermes_tui import (
    HermesTuiSessionRuntime,
    _delivery_copies_in_composer,
    _dismiss_heartbeat_confirmation,
    _hermes_pane_is_idle,
)


class _Mux:
    def __init__(self, exists: bool = True, panes: list[str] | None = None):
        self.alive = exists
        self.panes = list(panes or [])
        self.events: list[tuple[str, str, str | None]] = []

    def exists(self, name: str) -> bool:
        self.events.append(("exists", name, None))
        return self.alive

    def send_text_literal(self, name: str, text: str) -> None:
        self.events.append(("text", name, text))

    def send_keys(self, name: str, key: str) -> None:
        self.events.append(("key", name, key))

    def send_text_and_submit(self, name: str, text: str) -> None:
        self.events.append(("settled-submit", name, text))

    def send_text_and_submit_verified(self, name: str, text: str, **kwargs) -> int:
        self.events.append(("verified-submit", name, text))
        return 1

    def capture_content(self, name: str) -> str:
        pane = self.panes.pop(0) if self.panes else ""
        self.events.append(("capture", name, pane))
        return pane


def _config() -> AgentConfig:
    return AgentConfig(name="scholar", harness="hermes", runtime="tui")


def test_fresh_session_does_not_request_continuation():
    # Arrange
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        claude=ClaudeSpec(session="fresh"),
    )
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert "--continue" not in argv and "--create-if-missing" not in argv


def test_tui_launches_through_single_gateway_owner():
    # Arrange
    config = _config()
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert argv[:9] == [
        "/usr/bin/tini",
        "-s",
        "--",
        "python3",
        "-m",
        "scitex_agent_container.runtimes._hermes_tui_owner",
        "--state-dir",
        "/state/scholar",
        "--",
    ]


def test_continue_session_resumes_the_stable_agent_session_name():
    # Arrange
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        runtime="tui",
        claude=ClaudeSpec(session="continue"),
    )
    # Act
    argv = _hermes_tui_inner_argv(config)
    # Assert
    assert argv[argv.index("--continue") :] == [
        "--continue",
        "sac:scholar",
        "--create-if-missing",
    ]


def test_send_turn_uses_native_rpc_for_hermes_busy_input():
    # Arrange
    mux = _Mux(
        panes=[
            "Hub is compacting...",
            "♥ Heartbeat set (every 607s): check Cards\nend · ↑/↓ scroll · Esc/q close",
        ]
    )
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: (
            calls.append((state, name, text)) or "steered"
        ),
    )
    # Act
    delivered = runtime.send_turn(
        _config(), "/heartbeat every 607s check Cards", wait_ready=False
    )
    # Assert
    assert (delivered, len(calls), calls[0][1:], mux.events) == (
        True,
        1,
        ("scholar", "/heartbeat every 607s check Cards"),
        [],
    )


def test_hermes_idle_requires_latest_ready_footer_and_empty_composer():
    # Arrange
    ready = "\n ─ ready │ qwen38 27b low │ 44% ─ sac:stats\n ❯ "
    busy = (
        "\n ─ ready │ stale footer\n ❯ \n"
        " ─ ٩(๑❛ᴗ❛๑)۶ brainstorming… · 4m │ qwen38 27b low │ 66% ─ sac:scholar\n"
        " ❯ Ctrl+C to interrupt…"
    )
    human_text = "\n ─ ready │ qwen38 27b low │ 44% ─ sac:stats\n ❯ inspect this first"
    # Act
    observed = tuple(_hermes_pane_is_idle(pane) for pane in (ready, busy, human_text))
    # Assert
    assert observed == (
        True,
        False,
        False,
    )


def test_autonomous_idle_observation_is_read_only():
    # Arrange
    mux = _Mux(panes=[" ─ brainstorming… │ qwen38 27b low │ 66% ─ sac:scholar\n ❯ "])
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: calls.append(text) or "streaming",
    )
    # Act
    idle = runtime.autonomous_control_is_idle(_config())
    # Assert
    assert (idle, [event[0] for event in mux.events]) == (
        False,
        ["exists", "capture"],
    )


def test_heartbeat_dismissal_never_escapes_arbitrary_agent_work():
    # Arrange
    mux = _Mux(
        panes=[
            "old scrollback: ♥ Heartbeat set (every 607s)\n"
            "Hub is compacting...\nesc to interrupt"
        ]
        * 3
    )
    # Act
    dismissed = _dismiss_heartbeat_confirmation(
        "tui-scholar",
        "/heartbeat every 607s check Cards",
        capture_fn=mux.capture_content,
        send_keys_fn=mux.send_keys,
        max_captures=3,
        poll_s=0,
    )
    # Assert
    assert (dismissed, [event for event in mux.events if event[0] == "key"]) == (
        False,
        [],
    )


def test_non_heartbeat_turn_does_not_probe_or_dismiss_the_pane():
    # Arrange
    mux = _Mux(
        panes=[
            "♥ Heartbeat set (every 607s): old command\nend · ↑/↓ scroll · Esc/q close"
        ]
    )
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: calls.append(text) or "streaming",
    )
    # Act
    delivered = runtime.send_turn(_config(), "new guidance", wait_ready=False)
    # Assert
    assert (delivered, calls, mux.events) == (True, ["new guidance"], [])


def test_send_key_refuses_when_tmux_session_is_absent():
    # Arrange
    runtime = HermesTuiSessionRuntime(multiplexer=_Mux(exists=False))
    # Act
    delivered = runtime.send_key(_config(), "Enter")
    # Assert
    assert delivered is False


def test_visible_incoming_turn_confirms_sender_and_message_left_the_composer():
    # Arrange
    text = (
        '<channel source="operator" msg_id="m_visible">\n'
        "check signup\n</channel><!-- delivery:m_visible -->"
    )
    staged = "❯ [[ <channel source=.. [3 lines] .. delivery:m_visible --> ]]"
    submitted = staged + "\n◊ Pondering (Ctrl+C to interrupt)\n❯"
    mux = _Mux(
        panes=[
            "─ ready │ qwen38 27b low\n❯",
            staged,
            staged,
            submitted,
            submitted,
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_visible_turn(
        _config(), text, visible_delivery_id="m_visible"
    )
    # Assert
    assert (
        delivered,
        [event for event in mux.events if event[0] == "text"],
        [event for event in mux.events if event[0] == "key"],
        [event for event in mux.events if event[0] == "verified-submit"],
    ) == (
        True,
        [("text", "tui-scholar", text)],
        [("key", "tui-scholar", "Enter")],
        [],
    )


def test_collapsed_delivery_counter_rejects_mixed_operator_text():
    # Arrange
    chip = "[[ <channel source=.. [8 lines] .. delivery:n_visible --> ]]"
    text = "<channel>body</channel><!-- delivery:n_visible -->"

    # Act
    observations = (
        _delivery_copies_in_composer(
            f"❯ {chip} {chip}", text=text, delivery_id="n_visible"
        ),
        _delivery_copies_in_composer(
            f"❯ operator draft {chip}", text=text, delivery_id="n_visible"
        ),
    )

    # Assert
    assert observations == (2, None)


def test_visible_delivery_retry_normalizes_identical_collapsed_copies():
    # Arrange: this is the exact live failure shape.  The old generic echo
    # verifier appended the same collapsed paste four times because it looked
    # for a literal prefix Hermes never renders.
    text = (
        '<channel source="notifyd" msg_id="">\n'
        "digest\n</channel><!-- delivery:n_repeat -->"
    )
    chip = "[[ <channel source=.. [3 lines] .. delivery:n_repeat --> ]]"
    repeated = f"❯ {chip} {chip} {chip} {chip}"
    empty = "─ ready │ qwen38 27b low\n❯"
    staged_once = f"❯ {chip}"
    submitted = staged_once + "\n◊ Pondering (Ctrl+C to interrupt)\n❯"
    mux = _Mux(
        panes=[
            repeated,
            repeated,
            repeated,
            empty,
            staged_once,
            staged_once,
            submitted,
            submitted,
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)

    # Act
    delivered = runtime.send_visible_turn(
        _config(), text, visible_delivery_id="n_repeat", poll_s=0
    )

    # Assert
    assert (
        delivered,
        [event for event in mux.events if event[0] == "text"],
        [event[2] for event in mux.events if event[0] == "key"],
    ) == (
        True,
        [("text", "tui-scholar", text)],
        ["Escape", "Escape", "Enter"],
    )


def test_visible_delivery_retry_submits_one_staged_copy_without_repasting():
    # Arrange
    text = "<channel>digest</channel><!-- delivery:n_once -->"
    staged = "❯ <channel>digest</channel><!-- delivery:n_once -->"
    submitted = staged + "\n◊ Pondering (Ctrl+C to interrupt)\n❯"
    mux = _Mux(panes=[staged, staged, staged, submitted, submitted])
    runtime = HermesTuiSessionRuntime(multiplexer=mux)

    # Act
    delivered = runtime.send_visible_turn(
        _config(), text, visible_delivery_id="n_once", poll_s=0
    )

    # Assert
    assert (
        delivered,
        [event for event in mux.events if event[0] == "text"],
        [event[2] for event in mux.events if event[0] == "key"],
    ) == (True, [], ["Enter"])


def test_visible_delivery_never_mutates_mixed_human_and_delivery_text():
    # Arrange
    text = "<channel>digest</channel><!-- delivery:n_mixed -->"
    pane = (
        "─ ready │ qwen38 27b low\n"
        "❯ operator draft [[ <channel source=.. [3 lines] .. "
        "delivery:n_mixed --> ]]"
    )
    mux = _Mux(panes=[pane])
    runtime = HermesTuiSessionRuntime(multiplexer=mux)

    # Act
    delivered = runtime.send_visible_turn(
        _config(), text, visible_delivery_id="n_mixed", poll_s=0
    )

    # Assert
    assert (
        delivered,
        [event for event in mux.events if event[0] in {"text", "key"}],
    ) == (False, [])


def test_visible_incoming_turn_refuses_to_overwrite_staged_human_text():
    # Arrange
    mux = _Mux(panes=["\u276f operator is still typing this\nready"])
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_visible_turn(
        _config(),
        '<channel source="operator" msg_id="m_wait">\nnew\n</channel>',
        visible_delivery_id="m_wait",
    )
    # Assert
    assert (delivered, [event[0] for event in mux.events]) == (
        False,
        ["exists", "capture"],
    )


def test_visible_incoming_turn_does_not_duplicate_an_already_rendered_delivery():
    # Arrange: terminal visibility succeeded earlier, but the Cards ACK was
    # interrupted. The durable retry should confirm, not submit twice.
    mux = _Mux(
        panes=[
            '❯ <channel source="operator" msg_id="m_seen">\n'
            "already visible\n</channel><!-- delivery:m_seen -->\n❯"
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)

    # Act
    delivered = runtime.send_visible_turn(
        _config(),
        '<channel source="operator" msg_id="m_seen">\nnew\n</channel>',
        visible_delivery_id="m_seen",
    )

    # Assert
    assert (delivered, [event[0] for event in mux.events]) == (
        True,
        ["exists", "capture"],
    )


def test_visible_incoming_turn_does_not_false_fail_when_hermes_is_pondering():
    # Arrange
    text = (
        '<channel source="operator" msg_id="m_ponder">\n'
        "continue\n</channel><!-- delivery:m_ponder -->"
    )
    staged = "❯ [[ <channel source=.. [3 lines] .. delivery:m_ponder --> ]]"
    submitted = staged + "\n◊ Pondering (Ctrl+C to interrupt)\n❯"
    mux = _Mux(
        panes=[
            "─ ready │ qwen38 27b low\n❯",
            staged,
            staged,
            submitted,
            submitted,
        ]
    )
    runtime = HermesTuiSessionRuntime(multiplexer=mux)
    # Act
    delivered = runtime.send_visible_turn(
        _config(), text, visible_delivery_id="m_ponder", poll_s=0
    )
    # Assert
    assert delivered is True


def test_recovery_uses_supported_same_session_controls_in_order():
    # Arrange
    config = _config()
    config.model = "qwen38-27b"
    config.engine_key = "qwen38-27b"
    mux = _Mux()
    calls = []
    runtime = HermesTuiSessionRuntime(
        multiplexer=mux,
        rpc_submit=lambda state, name, text: calls.append(text) or "steered",
    )
    # Act
    paused = runtime.suspend_autonomous_turns(config)
    recovered = runtime.recover_turn_admission(config)
    # Assert
    assert (
        paused,
        recovered,
        calls,
    ) == (
        True,
        True,
        [
            "/heartbeat pause",
            "/model qwen38-27b --provider sac-qwen38-27b --session",
            "/heartbeat resume",
        ],
    )


class _LifecycleRuntime(HermesTuiSessionRuntime):
    def __init__(self):
        super().__init__(multiplexer=_Mux())
        self.lifecycle_events = []

    def _start_session(self, _config, **_kwargs):
        self.lifecycle_events.append("tmux-start")
        return True

    def _stop_session(self, _config):
        self.lifecycle_events.append("tmux-stop")
        return True

    def _start_inbox(self, _config):
        self.lifecycle_events.append("inbox-start")

    def _stop_inbox(self, _config):
        self.lifecycle_events.append("inbox-stop")

    def _start_recovery(self, _config):
        self.lifecycle_events.append("recovery-start")

    def _stop_recovery(self, _config):
        self.lifecycle_events.append("recovery-stop")


def test_start_attaches_authenticated_inbox_bridge_before_recovery():
    # Arrange
    runtime = _LifecycleRuntime()
    # Act
    started = runtime.start(_config())
    # Assert
    assert (started, runtime.lifecycle_events) == (
        True,
        ["tmux-start", "inbox-start", "recovery-start"],
    )


def test_stop_detaches_inbox_before_tmux_session():
    # Arrange
    runtime = _LifecycleRuntime()
    # Act
    stopped = runtime.stop(_config())
    # Assert
    assert (stopped, runtime.lifecycle_events) == (
        True,
        ["inbox-stop", "recovery-stop", "tmux-stop"],
    )
