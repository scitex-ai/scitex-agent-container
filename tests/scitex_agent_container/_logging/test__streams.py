"""Published output helpers preserve the caller's protocol payload."""

from io import StringIO

import pytest

from scitex_agent_container._logging import render_content, write_stream


@pytest.mark.parametrize("text", ["{\"status\":\"accepted\"}", "", "first\nsecond"])
def test_write_stream_preserves_protocol_payload_on_the_owned_stream(text):
    # Arrange
    stream = StringIO()
    # Act
    write_stream(text, stream)
    # Assert
    assert stream.getvalue() == text + "\n"


def test_render_content_preserves_completion_payload_on_stdout(capsys):
    # Arrange
    completion = "complete -F _sac_completion sac"
    # Act
    render_content(completion)
    # Assert
    assert capsys.readouterr() == (completion + "\n", "")


def test_write_stream_requires_a_caller_destination():
    # Arrange
    text = "synthetic protocol frame"
    # Act
    # Assert
    with pytest.raises(TypeError, match="stream"):
        write_stream(text)
