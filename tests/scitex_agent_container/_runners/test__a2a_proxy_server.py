"""The proxy's actual HTTP server must leave signal ownership with its runner."""

import json
import subprocess
import sys

import pytest

from ._a2a_proxy_workspace import proxy_workspace


@pytest.fixture(scope="module")
def native_signal_ownership():
    """Only a disposable child changes dispositions or receives signals."""
    script = (
        "import asyncio, json; "
        "from tests.scitex_agent_container._runners._a2a_proxy_signal_fixture "
        "import observe_signal_ownership; "
        "print(json.dumps(asyncio.run(observe_signal_ownership())))"
    )
    with proxy_workspace() as (_root, environment):
        result = subprocess.run(
            [sys.executable, "-c", script],
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
    return json.loads(result.stdout.splitlines()[-1])


def test_actual_server_serves_health(native_signal_ownership):
    # Arrange
    result = native_signal_ownership
    # Act
    health = result["health"]
    # Assert
    assert health == {"status": "ok"}


def test_actual_server_retains_runner_handlers_while_serving(native_signal_ownership):
    # Arrange
    result = native_signal_ownership
    # Act
    handlers = result["during"]
    # Assert
    assert handlers == [True, True]


def test_actual_server_preserves_handlers_after_shutdown(native_signal_ownership):
    # Arrange
    result = native_signal_ownership
    # Act
    handlers = result["after"]
    # Assert
    assert handlers == [True, True]


def test_owned_handlers_receive_real_child_signals(native_signal_ownership):
    # Arrange
    result = native_signal_ownership
    # Act
    received = result["received"]
    # Assert
    assert received == [15, 2]
