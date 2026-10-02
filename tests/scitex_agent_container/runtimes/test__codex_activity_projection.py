"""Native heartbeat publication preserves canonical authority and UNKNOWN."""

import json
from types import SimpleNamespace

import pytest

from scitex_agent_container._lifecycle._tui_heartbeat_loop import _beat_one
from scitex_agent_container._runners._session_state import write_heartbeat
from scitex_agent_container._state.state_store_hostname import resolve_host
from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_activity_projection import (
    promote_codex_activity,
)

from .test__codex_activity import THREAD, _meta, _tool, _turn
from .test__codex_activity_binding import AGENT, HOST, INSTANCE, _layout


class _Diary:
    def __init__(self):
        self.beats = []
        self.instances = []

    def record_heartbeat(self, **fields):
        self.beats.append(fields)

    def record_instance_heartbeat(self, instance_id, heartbeat):
        self.instances.append((instance_id, heartbeat))


def _promote(layout, *, writer=None, instance_reader=None, birth_reader=None, now=100):
    diary = _Diary()

    def write(state_dir, **fields):
        write_heartbeat(state_dir, db_writer=diary, **fields)
        if writer is not None:
            writer(state_dir)

    observation = promote_codex_activity(
        layout["state"],
        AGENT,
        host=HOST,
        write_fn=write,
        instance_reader=instance_reader or (lambda _: dict(layout["record"])),
        birth_reader=birth_reader or (lambda _: dict(layout["birth"])),
        proc_root=layout["proc"],
        now_fn=lambda: now,
    )
    return observation, diary


def _cache(layout):
    return json.loads((layout["state"] / "heartbeat.json").read_text())


def test_native_projection_uses_compiled_launch_and_existing_instance_lease(tmp_path):
    # Arrange: a current native root has completed a real correlated pair.
    layout = _layout(tmp_path)

    # Act: exercise the real shared writer and its diary/instance forwarding seams.
    observation, diary = _promote(layout)
    beat = _cache(layout)

    # Assert: canonical launch/thread identity and completed work reach the lease.
    assert (
        beat["writer"],
        beat["engine"],
        beat["model"],
        beat["session_id"],
        beat["tools_started"],
        beat["tools_completed"],
        beat["last_turn_status"],
        diary.instances[0][0],
        diary.instances[0][1]["session_id"],
        diary.instances[0][1]["progress_seq"],
        observation.event_seq,
    ) == (
        "codex-rollout-events",
        "native-selected",
        "gpt-6.1-sol",
        THREAD,
        1,
        1,
        "complete",
        INSTANCE,
        THREAD,
        4,
        4,
    )


def test_native_projection_does_not_import_stale_claude_caps_phase_or_usage(tmp_path):
    # Arrange: old harness sidecars would normally produce CAPPED/blocked/99 turns.
    layout = _layout(tmp_path)
    state = layout["state"]
    (state / "capped").write_text("stale")
    (state / "phase.txt").write_text("blocked")
    (state / "quota.json").write_text(json.dumps({"turns": 99, "input_tokens": 999}))
    (state / "session.jsonl").write_text("quota exceeded: unrelated old harness\n")

    # Act: publish only native instruments, keeping provider capacity unobserved.
    _promote(layout)
    beat = _cache(layout)

    # Assert: typed UNKNOWN survives instead of stale false health/cap inference.
    assert (
        beat["capacity_status"],
        beat["capped"],
        beat["current_phase"],
        beat["turns_completed"],
        beat.get("input_tokens"),
        beat.get("session_jsonl_bytes"),
        beat["authoritative_heartbeat"]["state"],
    ) == ("unknown", None, "", 1, None, None, "idle")


@pytest.mark.parametrize("manager", ["hub", "stats", "app", "ui", "cards"])
def test_five_error_zero_tool_managers_remain_blocked_with_unknown_capacity(
    tmp_path, manager
):
    # Arrange: a terminal native provider/filesystem error never started a tool.
    layout = _layout(tmp_path)
    layout["rollout"].write_text(
        _meta()
        + _turn("task_started")
        + _turn(
            "task_complete", timestamp=12, error={"message": f"private-{manager}-error"}
        )
    )

    # Act: publish actual terminal lifecycle through the common heartbeat contract.
    _promote(layout)
    beat = _cache(layout)

    # Assert: live runtime/accepted turn does not make these managers productive.
    assert (
        beat["last_turn_status"],
        beat["tools_started"],
        beat["tools_completed"],
        beat["authoritative_heartbeat"]["state"],
        beat["capped"],
        beat["capacity_status"],
    ) == ("error", 0, 0, "blocked", None, "unknown")


def test_new_active_turn_can_work_while_previous_terminal_error_remains_visible(
    tmp_path,
):
    # Arrange: the native loop accepted new work after the prior failure.
    layout = _layout(tmp_path)
    layout["rollout"].write_text(
        _meta()
        + _turn("task_started")
        + _turn("task_complete", timestamp=12, error={"message": "private"})
        + _turn("task_started", turn="turn-2", timestamp=13)
        + _tool("function_call", timestamp=14)
    )

    # Act: reflect both present activity and the historical error honestly.
    _promote(layout)
    beat = _cache(layout)

    # Assert: the ongoing turn is active, while its prior error is retained.
    assert (
        beat["state"],
        beat["authoritative_heartbeat"]["state"],
        beat["last_turn_status"],
        beat["tools_inflight"],
    ) == ("busy", "active", "error", 1)


@pytest.mark.parametrize(
    "mutation", ["missing-owner", "ended", "missing-birth", "wrong-model-route"]
)
def test_missing_or_wrong_authority_preserves_the_previous_heartbeat(
    tmp_path, mutation
):
    # Arrange: a previous authoritative cache exists before evidence becomes unknown.
    layout = _layout(tmp_path)
    previous = b'{"writer":"previous-owner","session_id":"old-session"}\n'
    (layout["state"] / "heartbeat.json").write_bytes(previous)
    instance_reader = None
    birth_reader = None

    def missing(_):
        return None

    if mutation == "missing-owner":
        instance_reader = missing
    elif mutation == "ended":
        layout["record"]["ended_at"] = "ended"
    elif mutation == "missing-birth":
        birth_reader = missing
    else:
        compiled = json.loads(layout["birth"]["compiled_spec_json"])
        compiled["harness"] = "hermes"
        layout["birth"]["compiled_spec_json"] = json.dumps(compiled)

    # Act: attempt native publication with missing/wrong canonical authority.
    with pytest.raises(CodexActivityError):
        _promote(layout, instance_reader=instance_reader, birth_reader=birth_reader)

    # Assert: UNKNOWN does not overwrite a known previous authority.
    assert (layout["state"] / "heartbeat.json").read_bytes() == previous


def test_owner_change_after_write_retracts_only_this_observer_cache(tmp_path):
    # Arrange: the canonical record ends during the real shared writer call.
    layout = _layout(tmp_path)
    previous = b'{"writer":"previous-owner","session_id":"old-session"}\n'
    (layout["state"] / "heartbeat.json").write_bytes(previous)

    def end_owner(_):
        layout["record"]["ended_at"] = "ended"

    # Act: fence again after publication and detect the actual metadata change.
    with pytest.raises(CodexActivityError):
        _promote(layout, writer=end_owner)

    # Assert: this stale observer's cache is restored without a new authority claim.
    assert (layout["state"] / "heartbeat.json").read_bytes() == previous


def test_concurrent_successor_cache_is_preserved_after_old_owner_race(tmp_path):
    # Arrange: a successor publishes after this observer's write.
    layout = _layout(tmp_path)
    successor = b'{"writer":"successor","session_id":"successor-session"}\n'

    def replace_owner(state):
        (state / "instance_id").write_text("successor-instance")
        (state / "heartbeat.json").write_bytes(successor)

    # Act: detect the changed canonical marker after publication.
    with pytest.raises(CodexActivityError):
        _promote(layout, writer=replace_owner)

    # Assert: retraction cannot clobber a cache another owner already owns.
    assert (layout["state"] / "heartbeat.json").read_bytes() == successor


def test_truncated_replay_cannot_regress_counters_within_same_engine(tmp_path):
    # Arrange: a published pair exists, then the same inode loses its tool history.
    layout = _layout(tmp_path)
    _promote(layout)
    previous = (layout["state"] / "heartbeat.json").read_bytes()
    layout["rollout"].write_text(_meta() + _turn("task_started"))

    # Act: a syntactically complete shortened replay must not reset trusted work.
    with pytest.raises(CodexActivityError, match="regressed"):
        _promote(layout, now=101)

    # Assert: preserve the previous native watermark rather than fabricated zero.
    assert (layout["state"] / "heartbeat.json").read_bytes() == previous


def test_listener_uses_native_birth_harness_even_when_current_spec_defaults_to_hermes(
    tmp_path,
):
    # Arrange: an explicit engine launch selected native Codex from a Hermes default.
    layout = _layout(tmp_path)
    host = resolve_host(None)
    layout["record"]["host"] = host
    layout["birth"]["host"] = host
    diary = _Diary()

    def writer(state_dir, **fields):
        write_heartbeat(state_dir, db_writer=diary, **fields)

    def promoter(state_dir, name, **fields):
        return promote_codex_activity(
            state_dir,
            name,
            **fields,
            instance_reader=lambda _: dict(layout["record"]),
            proc_root=layout["proc"],
            now_fn=lambda: 100,
        )

    # Act: the actual listener branch drives the real native projection/writer.
    written = _beat_one(
        {
            "name": AGENT,
            "state_dir": layout["state"],
            "config": SimpleNamespace(harness="hermes"),
        },
        snapshot={f"tui-{AGENT}": 1},
        write_fn=writer,
        codex_promote_fn=promoter,
        birth_reader=lambda _: dict(layout["birth"]),
    )

    # Assert: pane/spec defaults cannot replace the selected native thread identity.
    assert (written, _cache(layout)["writer"], _cache(layout)["session_id"]) == (
        True,
        "codex-rollout-events",
        THREAD,
    )


def test_listener_does_not_fall_back_to_pane_heartbeat_when_native_binding_is_unknown(
    tmp_path,
):
    # Arrange: native birth identity exists, but its canonical instance has ended.
    layout = _layout(tmp_path)
    host = resolve_host(None)
    layout["record"].update(host=host, ended_at="ended")
    layout["birth"]["host"] = host
    previous = b'{"writer":"previous-native","session_id":"previous-thread"}\n'
    (layout["state"] / "heartbeat.json").write_bytes(previous)

    def promoter(state_dir, name, **fields):
        return promote_codex_activity(
            state_dir,
            name,
            **fields,
            instance_reader=lambda _: dict(layout["record"]),
            proc_root=layout["proc"],
            now_fn=lambda: 100,
        )

    # Act: live pane activity alone cannot bypass the native owner gate.
    written = _beat_one(
        {
            "name": AGENT,
            "state_dir": layout["state"],
            "config": SimpleNamespace(harness="codex"),
        },
        snapshot={f"tui-{AGENT}": 99},
        write_fn=write_heartbeat,
        codex_promote_fn=promoter,
        birth_reader=lambda _: dict(layout["birth"]),
    )

    # Assert: the old authority survives and no generic observer beat is written.
    assert (written, (layout["state"] / "heartbeat.json").read_bytes()) == (
        False,
        previous,
    )
