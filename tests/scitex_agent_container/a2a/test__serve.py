"""Foreground socket activation rejects invalid descriptors before reading agent files."""

import os
import socket

import pytest
from click.testing import CliRunner

from scitex_agent_container.a2a._serve import serve
from scitex_agent_container.cli_pkg.a2a_group import a2a

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX socket activation")


def test_descriptor_address_mismatch_refuses_before_loading_yaml(tmp_path):
    # Arrange
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]

        # Act
        def activate():
            return serve(
                [tmp_path / "absent.yaml"], port=port + 1, fd=listener.fileno()
            )

        # Assert
        with pytest.raises(ValueError, match="does not match"):
            activate()


@pytest.mark.parametrize("fd", [-1, 0, 1, 2])
def test_standard_stream_or_negative_cli_descriptor_is_a_usage_error(tmp_path, fd):
    # Arrange: only path existence is needed; no agent should be loaded.
    path = tmp_path / "agent.yaml"
    path.write_text("not-an-agent")
    # Act
    result = CliRunner().invoke(a2a, ["serve", str(path), "--fd", str(fd)])
    # Assert
    assert result.exit_code == 2 and "--fd" in result.output
