"""Local kernel evidence cannot retire a foreign incarnation."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._lifecycle._stale_lease import clear_stale_instance_lease


def _refuse_pid_probe(_pid: int) -> bool:
    raise AssertionError("foreign or malformed ownership reached a local PID probe")


@pytest.mark.parametrize("host", ["foreign", None, "", "local "])
def test_foreign_or_unqualified_host_never_reaches_local_pid_probe(host) -> None:
    # Arrange
    rows = [{"id": "foreign-row", "name": "agent", "host": host, "pid": 99999999}]
    # Act
    cleared = clear_stale_instance_lease(
        "agent", instances_oracle=lambda: rows, host_reader=lambda: "local",
        pid_alive_fn=_refuse_pid_probe,
    )
    # Assert
    assert cleared == 0


@pytest.mark.parametrize("pid", [True, False, 0, -1, None, "malformed"])
def test_unqualified_pid_never_reaches_local_kernel_probe(pid) -> None:
    # Arrange
    rows = [{"id": "local-row", "name": "agent", "host": "local", "pid": pid}]
    # Act
    cleared = clear_stale_instance_lease(
        "agent", instances_oracle=lambda: rows, host_reader=lambda: "local",
        pid_alive_fn=_refuse_pid_probe,
    )
    # Assert
    assert cleared == 0


def test_indeterminate_pid_cannot_authorize_lease_retirement() -> None:
    # Arrange
    rows = [{"id": "local-row", "name": "agent", "host": "local", "pid": os.getpid()}]
    retired = []
    # Act
    clear_stale_instance_lease(
        "agent", instances_oracle=lambda: rows, host_reader=lambda: "local",
        pid_alive_fn=lambda _pid: None, stop_writer=lambda *args: retired.append(args),
    )
    # Assert
    assert retired == []


def test_unknown_host_preserves_owned_incarnation_marker() -> None:
    # Arrange
    from scitex_agent_container._runners._session_state import (
        read_instance_id,
        state_dir_for,
        write_instance_id,
    )

    directory = state_dir_for("host-unknown-marker")
    write_instance_id(directory, "preserved-incarnation")
    # Act
    clear_stale_instance_lease("host-unknown-marker", host_reader=lambda: "")
    # Assert
    assert read_instance_id(directory) == "preserved-incarnation"


def test_actual_store_retains_foreign_same_name_dead_pid(pg_schema: str) -> None:
    # Arrange
    from scitex_agent_container._state.state_store import (
        _resolve_host,
        list_active_instances,
        record_instance_start,
    )

    child = os.fork()
    if child == 0:
        os._exit(0)
    os.waitpid(child, 0)
    name = "foreign-incarnation-fence"
    local_host = _resolve_host(None)
    foreign = record_instance_start(name=name, host=local_host+"-foreign", pid=child)
    record_instance_start(name=name, pid=child)
    # Act
    clear_stale_instance_lease(name)
    survivors = [row["id"] for row in list_active_instances() if row["name"] == name]
    # Assert
    assert survivors == [foreign]
