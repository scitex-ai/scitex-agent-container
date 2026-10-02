"""Filesystem-backed kernel/FD fixtures for exact native runtime ownership."""

import json
import os

import pytest

from scitex_agent_container.runtimes._codex_activity import CodexActivityError
from scitex_agent_container.runtimes._codex_activity_binding import (
    assert_codex_binding_current,
    bind_codex_runtime,
)

from .test__codex_activity import CHILD, THREAD, _meta, _tool, _turn

INSTANCE = "932ebcea-3619-4e60-a6d2-7069a1381815"
AGENT = "native-fixture"
HOST = "host-fixture"
CGROUP = "/user.slice/fixture.scope"


def _process(proc_root, pid, parent, start, comm="codex", cgroup=CGROUP):
    process = proc_root / str(pid)
    process.mkdir(exist_ok=True)
    fields = ["S", str(parent), "999991", "999992"] + ["0"] * 15 + [str(start)]
    (process / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))
    (process / "cgroup").write_text(f"0::{cgroup}\n")
    (process / "comm").write_text(comm + "\n")
    (process / "fd").mkdir(exist_ok=True)
    return process


def _layout(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "instance_id").write_text(INSTANCE)
    proc = tmp_path / "proc"
    proc.mkdir()
    _process(proc, 4001, 0, 100, "apptainer")
    _process(proc, 4002, 4001, 101, "starter")
    native = _process(proc, 4003, 4002, 102)
    _process(proc, 4004, 4003, 103, "codex-code-mode-host")
    rollout = tmp_path / f"rollout-native-{THREAD}.jsonl"
    rollout.write_text(
        _meta()
        + _turn("task_started")
        + _tool("function_call")
        + _tool("function_call_output", timestamp=13)
        + _turn("task_complete", timestamp=14)
    )
    (native / "fd" / "3").symlink_to(rollout)
    record = {
        "id": INSTANCE,
        "name": AGENT,
        "host": HOST,
        "ended_at": None,
        "remote": False,
        "screen": f"tui-{AGENT}",
        "pid": 4001,
        "process_start_time": 100,
        "process_uid": os.getuid(),
        "control_group": CGROUP,
        "scope_unit": "fixture.scope",
        "scope_invocation_id": "a" * 32,
    }
    birth = {
        "incarnation_id": INSTANCE,
        "agent_id": AGENT,
        "host": HOST,
        "compiled_spec_json": json.dumps(
            {
                "name": AGENT,
                "runtime": "tui",
                "harness": "codex",
                "engine_key": "native-selected",
                "claude": {"model": "gpt-6.1-sol"},
            }
        ),
    }
    return {
        "proc": proc,
        "state": state,
        "rollout": rollout,
        "record": record,
        "birth": birth,
    }


def _bind(layout):
    return bind_codex_runtime(
        layout["record"],
        instance_id=INSTANCE,
        agent_name=AGENT,
        host=HOST,
        proc_root=layout["proc"],
    )


def _child_fd(layout, fd=4):
    child = layout["rollout"].parent / f"rollout-child-{CHILD}.jsonl"
    child.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {
                    "id": CHILD,
                    "session_id": THREAD,
                    "source": {
                        "subagent": {"thread_spawn": {"parent_thread_id": THREAD}}
                    },
                },
            }
        )
        + "\n"
    )
    (layout["proc"] / "4003" / "fd" / str(fd)).symlink_to(child)
    return child


def test_canonical_owner_binds_its_exclusive_cli_root_and_excludes_child_fds(tmp_path):
    # Arrange: the same native process owns root and child files plus a code-mode host.
    layout = _layout(tmp_path)
    _child_fd(layout)
    _child_fd(layout, 5)

    # Act: derive the descriptor from canonical ownership and primary metadata.
    binding = _bind(layout)

    # Assert: only the owned CLI root defines this runtime/thread.
    assert (
        binding.instance_id,
        binding.pane.pid,
        binding.native.pid,
        binding.thread_id,
        binding.rollout_fd,
        binding.rollout_identity,
    ) == (
        INSTANCE,
        4001,
        4003,
        THREAD,
        3,
        (layout["rollout"].stat().st_dev, layout["rollout"].stat().st_ino),
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("name", "peer"),
        ("host", "other-host"),
        ("ended_at", "ended"),
        ("remote", True),
        ("screen", "unowned-pane"),
        ("pid", 4999),
        ("process_start_time", 99),
        ("process_uid", -1),
        ("control_group", "/another.scope"),
    ],
)
def test_unowned_or_stale_canonical_record_cannot_bind_a_runtime(
    tmp_path, field, value
):
    # Arrange: a mismatch in any canonical owner field.
    layout = _layout(tmp_path)
    layout["record"][field] = value

    # Act and assert: readable native files cannot authorize another process.
    # Assert
    with pytest.raises(CodexActivityError):
        _bind(layout)


def test_non_descendant_codex_is_not_selected_even_if_it_owns_the_same_root(tmp_path):
    # Arrange: the root FD belongs to a peer outside the recorded pane's tree.
    layout = _layout(tmp_path)
    (layout["proc"] / "4003" / "fd" / "3").unlink()
    peer = _process(layout["proc"], 4009, 0, 109)
    (peer / "fd" / "3").symlink_to(layout["rollout"])

    # Act and assert: an accessible peer file is not a runtime binding.
    # Assert
    with pytest.raises(CodexActivityError, match="exclusive CLI root"):
        _bind(layout)


def test_multiple_cli_root_fds_are_ambiguous_instead_of_using_newest_file(tmp_path):
    # Arrange: two different CLI root files are open in the owner process.
    layout = _layout(tmp_path)
    other = tmp_path / f"rollout-other-{CHILD}.jsonl"
    other.write_text(_meta(CHILD))
    (layout["proc"] / "4003" / "fd" / "4").symlink_to(other)

    # Act and assert: no timestamp heuristic chooses a session.
    # Assert
    with pytest.raises(CodexActivityError, match="exclusive CLI root"):
        _bind(layout)


def test_duplicate_root_fd_in_same_owner_is_one_file_identity(tmp_path):
    # Arrange: one owner has opened the same root inode twice.
    layout = _layout(tmp_path)
    (layout["proc"] / "4003" / "fd" / "4").symlink_to(layout["rollout"])

    # Act: deduplicate by actual file identity.
    binding = _bind(layout)

    # Assert: duplicated descriptor handles do not imply two sessions.
    assert binding.thread_id == THREAD


@pytest.mark.parametrize(
    "change", ["pid-reuse", "lineage", "cgroup", "fd-reuse", "ended"]
)
def test_bound_owner_is_fenced_again_before_publication(tmp_path, change):
    # Arrange: the descriptor was valid before a metadata race.
    layout = _layout(tmp_path)
    binding = _bind(layout)
    if change == "pid-reuse":
        _process(layout["proc"], 4003, 4002, 999)
    elif change == "lineage":
        _process(layout["proc"], 4003, 0, 102)
    elif change == "cgroup":
        _process(layout["proc"], 4003, 4002, 102, cgroup="/other.scope")
    elif change == "fd-reuse":
        fd = binding.rollout_path
        fd.unlink()
        fd.symlink_to(_child_fd(layout, 6))
    else:
        layout["record"]["ended_at"] = "ended"

    # Act and assert: stale PID/session evidence cannot renew the owner.
    # Assert
    with pytest.raises(CodexActivityError):
        assert_codex_binding_current(binding, layout["record"])


def test_inherited_home_and_handover_uuid_do_not_select_the_native_owner(
    tmp_path, monkeypatch
):
    # Arrange: the listener inherited another private home and handover identity.
    layout = _layout(tmp_path)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.setenv("SAC_INSTANCE_UUID", "492e3624-e623-454f-91fe-d28b6b22305b")

    # Act: bind only canonical instance/kernel/FD evidence.
    binding = _bind(layout)

    # Assert: separate handover identity and listener home cannot reroute activity.
    assert (binding.instance_id, binding.thread_id) == (INSTANCE, THREAD)
