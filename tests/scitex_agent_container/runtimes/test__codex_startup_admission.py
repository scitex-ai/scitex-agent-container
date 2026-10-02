"""Native startup admission from real synthetic proc/FD/JSONL boundaries."""

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_startup_admission import (
    capture_startup_admission,
    wait_for_startup_admission,
)
from scitex_agent_container.runtimes.tui_session import TuiSessionRuntime

from .test__codex_activity import _meta, _row, _turn
from .test__codex_activity_binding import AGENT, INSTANCE, _child_fd, _layout, _process

MISSION = "Public synthetic startup mission: read the assigned Card and report."
READY = "› Await instructions\ngpt-6.1-sol ultra fast · /fixture\n"
HOOKS = (
    "Hooks need review\n"
    + "\n".join(f"  public-hook-{i}" for i in range(51))
    + "\n› Review enabled hooks\n? for shortcuts\n"
)


def _accepted(layout, *, mission=MISSION, user_at=22, turn_at=21, turn="startup-turn"):
    layout["rollout"].write_text(
        _meta()
        + _turn("task_started", turn=turn, timestamp=turn_at)
        + _row(
            "event_msg",
            {"type": "user_message", "message": mission, "turn_id": turn},
            user_at,
        )
    )


@pytest.fixture
def startup_source(tmp_path):
    layout = _layout(tmp_path)
    config = AgentConfig(name=AGENT, harness="codex", runtime="tui")
    config.env["SAC_INSTANCE_UUID"] = INSTANCE
    probe = capture_startup_admission(
        config,
        pane_pid=4001,
        mission=MISSION,
        proc_root=layout["proc"],
        issued_at=20,
    )
    return layout, config, probe


def test_fresh_exact_native_user_mission_and_turn_prove_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is True


def test_native_response_user_text_can_prove_the_same_mission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    layout["rollout"].write_text(
        _meta()
        + _turn("task_started", turn="startup-turn", timestamp=21)
        + _row(
            "response_item",
            {
                "type": "message",
                "role": "user",
                "turn_id": "startup-turn",
                "content": [{"type": "input_text", "text": MISSION}],
            },
            22,
        )
    )
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is True


@pytest.mark.parametrize(
    "mission", ["prefix " + MISSION, MISSION + " suffix", "another mission"]
)
def test_a_different_native_user_message_cannot_prove_the_mission(
    startup_source, mission
):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout, mission=mission)
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_native_user_echo_without_an_accepted_turn_is_unknown(startup_source):
    # Arrange
    layout, _, probe = startup_source
    layout["rollout"].write_text(
        _meta() + _row("event_msg", {"type": "user_message", "message": MISSION}, 22)
    )
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


@pytest.mark.parametrize("user_at,turn_at", [(19, 21), (22, 19)])
def test_old_native_message_or_turn_cannot_prove_fresh_admission(
    startup_source, user_at, turn_at
):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout, user_at=user_at, turn_at=turn_at)
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_user_mission_with_a_different_turn_id_cannot_prove_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    layout["rollout"].write_text(
        _meta()
        + _turn("task_started", turn="accepted-turn", timestamp=21)
        + _row(
            "event_msg",
            {"type": "user_message", "message": MISSION, "turn_id": "unaccepted-turn"},
            22,
        )
    )
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_multiple_fresh_turns_do_not_establish_exclusive_startup_admission(
    startup_source,
):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    with layout["rollout"].open("a") as stream:
        stream.write(_turn("task_started", turn="other-turn", timestamp=23))
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_an_incomplete_native_record_cannot_prove_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    with layout["rollout"].open("a") as stream:
        stream.write('{"type":')
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_a_replaced_pane_birth_cannot_prove_startup_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    _process(layout["proc"], 4001, 0, 999, "apptainer")
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_an_unowned_cli_file_cannot_prove_startup_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    (layout["proc"] / "4003/fd/3").unlink()
    outsider = _process(layout["proc"], 5003, 0, 104)
    (outsider / "fd/3").symlink_to(layout["rollout"])
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_a_child_rollout_cannot_prove_primary_startup_admission(startup_source):
    # Arrange
    layout, _, probe = startup_source
    (layout["proc"] / "4003/fd/3").unlink()
    _child_fd(layout)
    # Act
    admitted = probe.observed(observed_at=100)
    # Assert
    assert admitted is False


def test_a_hooks_modal_refuses_even_a_valid_native_turn(startup_source):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    # Act
    admitted = wait_for_startup_admission(
        probe,
        capture_fn=lambda: HOOKS,
        timeout_s=3,
        time_fn=iter(range(20)).__next__,
        wall_time_fn=lambda: 100,
        sleep_fn=lambda _: None,
    )
    # Assert
    assert admitted is False


def test_dismissed_hooks_history_with_current_composer_allows_native_proof(
    startup_source,
):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    # Act
    admitted = wait_for_startup_admission(
        probe,
        capture_fn=lambda: HOOKS + READY,
        timeout_s=3,
        time_fn=iter(range(20)).__next__,
        wall_time_fn=lambda: 100,
        sleep_fn=lambda _: None,
    )
    # Assert
    assert admitted is True


def test_visible_payload_without_native_admission_times_out(startup_source):
    # Arrange
    _, _, probe = startup_source
    # Act
    admitted = wait_for_startup_admission(
        probe,
        capture_fn=lambda: "› " + MISSION + "\n" + READY,
        timeout_s=3,
        time_fn=iter(range(20)).__next__,
        wall_time_fn=lambda: 100,
        sleep_fn=lambda _: None,
    )
    # Assert
    assert admitted is False


def test_review_appearing_during_native_observation_cannot_report_admission(
    startup_source,
):
    # Arrange
    layout, _, probe = startup_source
    _accepted(layout)
    frames = iter([READY, HOOKS])
    # Act
    admitted = wait_for_startup_admission(
        probe,
        capture_fn=frames.__next__,
        timeout_s=3,
        time_fn=iter(range(20)).__next__,
        wall_time_fn=lambda: 100,
        sleep_fn=lambda _: None,
    )
    # Assert
    assert admitted is False


def test_missing_allocated_launch_identity_refuses_before_native_read(startup_source):
    # Arrange
    layout, config, _ = startup_source
    config.env.clear()
    # Act
    # Assert
    with pytest.raises(CodexActivityError, match="allocated launch UUID"):
        capture_startup_admission(
            config, pane_pid=4001, mission=MISSION, proc_root=layout["proc"]
        )


class _ReadyBootstrapMux:
    """A ready input device with no allocated native launch identity."""

    def capture_content(self, _name):
        return READY

    def exists(self, _name):
        return True

    def pane_pid(self, _name):
        return 4001

    def send_keys(self, *_args):
        raise AssertionError("unexpected bootstrap key before ownership proof")

    def send_text_literal(self, *_args):
        raise AssertionError("unexpected bootstrap paste before ownership proof")


def test_default_runtime_requires_launch_identity_before_pasting_to_ready_input():
    # Arrange
    config = AgentConfig(
        name=AGENT, harness="codex", runtime="tui", startup_prompts=[MISSION]
    )
    runtime = TuiSessionRuntime(multiplexer=_ReadyBootstrapMux())
    # Act
    # Assert
    with pytest.raises(CodexActivityError, match="allocated launch UUID"):
        runtime._inject_startup_prompts(config)
