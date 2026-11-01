"""Real native listener forwarding; PostgreSQL fixtures belong to this suite."""

import asyncio

from scitex_agent_container._django._control import new_dispatch_id
from tests.scitex_agent_container._django.test__listener_message import (
    Bridge,
)
from tests.scitex_agent_container._django.test__listener_message import (
    endpoints as endpoints,
)


def test_local_listener_forward_threads_dispatch_option(endpoints, pg_schema):
    # Arrange
    from types import SimpleNamespace

    from scitex_agent_container._listen._forward import forward_to_live_runner

    _, port = endpoints
    cfg = SimpleNamespace(a2a=SimpleNamespace(host="127.0.0.1", port=port))
    dispatch = new_dispatch_id()
    # Act
    result = asyncio.run(
        forward_to_live_runner(
            cfg,
            "fixture-local",
            "synthetic fixture",
            {"dispatch_id": dispatch},
            timeout=2,
        )
    )
    # Assert
    assert result.status_code == 202 and Bridge.received == [
        {"text": "synthetic fixture", "dispatch_id": dispatch}
    ]


def test_remote_listener_resolution_threads_dispatch_option(
    endpoints, pg_schema, tmp_path, env_save_restore
):
    # Arrange
    from scitex_agent_container._listen._forward import forward_to_remote_runner
    from scitex_agent_container._state import state_store

    _, port = endpoints
    state_store.record_instance_start(
        name="fixture-remote", host="fixture.invalid", a2a_port=port
    )
    shim = tmp_path / "ssh"
    shim.write_text(
        "#!/usr/bin/python3\nimport os,sys\nos.execv('/bin/sh', ['sh','-c',sys.argv[-1]])\n"
    )
    shim.chmod(0o755)
    import os

    env_save_restore.set("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    dispatch = new_dispatch_id()
    # Act
    result = asyncio.run(
        forward_to_remote_runner(
            "fixture-remote",
            "synthetic fixture",
            local_host="fixture-local.invalid",
            timeout=2,
            dispatch_id=dispatch,
        )
    )
    # Assert
    assert result.status_code == 202 and Bridge.received == [
        {"text": "synthetic fixture", "dispatch_id": dispatch}
    ]
