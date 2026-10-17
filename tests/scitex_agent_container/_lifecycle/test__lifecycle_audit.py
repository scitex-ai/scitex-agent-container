"""Local lifecycle invocation audit log.

Pins the rootfix audit contract: every local lifecycle invocation
records actor, timestamp, and flags to JSONL, and a failed append
never raises. AAA markers per test, behaviour-shaped names, one
assertion per test. Real file writes under ``tmp_path`` only.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._lifecycle import _lifecycle_audit as audit_mod


def _read_rows(path: Path) -> list:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_audit_row_records_actor_timestamp_and_flags(tmp_path):
    # Arrange
    path = tmp_path / "audit.jsonl"
    actor = {"parent_agent": "lead-x", "user": "op", "uid": 1000, "pid": 7}
    # Act
    written = audit_mod.record_lifecycle_invocation(
        "agent-restart",
        name="scholar",
        flags={"force": True, "fresh": False},
        actor=actor,
        log_path=path,
    )
    rows = _read_rows(path)
    # Assert — one row carrying actor, timestamp, and flags.
    assert (written, rows[0]["action"], rows[0]["name"],
            rows[0]["flags"], rows[0]["actor"],
            isinstance(rows[0]["ts"], float)) == (
        True, "agent-restart", "scholar",
        {"force": True, "fresh": False}, actor, True,
    )


def test_audit_append_never_raises_on_unwritable_path(tmp_path):
    # Arrange — a log path whose parent is a regular file.
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    # Act
    written = audit_mod.record_lifecycle_invocation(
        "agent-stop", name="scholar", log_path=blocker / "audit.jsonl"
    )
    # Assert — failure degrades to False, never an exception.
    assert written is False


def test_audit_actor_names_parent_agent_from_env():
    # Arrange — a launch shelled out from a parent agent container.
    environ = {"SAC_NAME": "lead-x", "USER": "op"}
    # Act
    actor = audit_mod.resolve_audit_actor(environ)
    # Assert
    assert (actor["parent_agent"], actor["user"]) == ("lead-x", "op")


def test_audit_actor_without_parent_is_bare_launch():
    # Arrange — a bare operator/lead CLI launch has no parent agent.
    environ = {"USER": "op"}
    # Act
    actor = audit_mod.resolve_audit_actor(environ)
    # Assert
    assert actor["parent_agent"] is None


def test_audit_helper_writes_under_runtime_dir(tmp_path, env_save_restore):
    # Arrange — the runtime dir is pinned to tmp for this test.
    env_save_restore.set(
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(tmp_path / "runtime")
    )
    # Act
    audit_mod.audit("agent-stop", "scholar", force=True)
    rows = _read_rows(audit_mod.audit_log_path())
    # Assert — the helper records the invocation with its flags.
    assert (rows[-1]["action"], rows[-1]["name"],
            rows[-1]["flags"]) == (
        "agent-stop", "scholar", {"force": True},
    )
