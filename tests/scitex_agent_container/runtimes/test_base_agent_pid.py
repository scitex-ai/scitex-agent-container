"""``RuntimeBase.agent_pid`` — the seam's default is honest "unknown".

``agent_pid`` is what hands ``instances.pid`` its value
(``_lifecycle._instances.record_local_instance``). It is deliberately NOT
abstract, and its default is ``None``: a runtime that cannot name a
long-lived LOCAL pid (docker / podman / SSHRemote — the process lives in
another namespace or on another host) must leave it ``None``.

``None`` is honestly "unknown", and every consumer treats it as such
(``state_db_gc`` skips it, ``_stale_lease`` leaves the row alone,
``_send_diagnosis._pid_alive`` returns ``None``, not ``False``). A
plausible-but-wrong pid is strictly WORSE, because pids get REUSED — a stale
one can be recycled by an unrelated process and would then vouch for a dead
agent as alive.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig


def test_agent_pid_defaults_to_none() -> None:
    # Arrange — a runtime that cannot name a long-lived local pid inherits
    # the base default rather than fabricating a value.
    from scitex_agent_container.runtimes.base import RuntimeBase

    cfg = AgentConfig(name="base-default", runtime="apptainer")
    # Act
    pid = RuntimeBase.agent_pid(object(), cfg)  # type: ignore[arg-type]
    # Assert
    assert pid is None
