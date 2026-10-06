"""Published output helpers preserve the caller's protocol payload."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout, suppress
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from scitex_agent_container._logging import render_content, write_stream


@pytest.mark.parametrize("text", ['{"status":"accepted"}', "", "first\nsecond"])
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


@pytest.mark.parametrize("flush", [False, True])
def test_stream_writer_preserves_caller_requested_flushing(tmp_path, flush):
    # Arrange
    path = tmp_path / "buffered-frame.txt"
    with path.open("w", buffering=8192) as stream:
        # Act
        write_stream("accepted", stream, flush=flush)
        # Assert
        assert path.read_text() == ("accepted\n" if flush else "")


def test_concurrent_writers_keep_frames_on_their_owned_streams():
    # Arrange
    streams = [StringIO() for _ in range(100)]
    # Act
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda item: write_stream(str(item[0]), item[1]), enumerate(streams)
            )
        )
    # Assert
    assert [stream.getvalue() for stream in streams] == [f"{i}\n" for i in range(100)]


def test_failed_stream_write_raises_and_does_not_leak_its_handler():
    # Arrange
    closed = StringIO()
    closed.close()
    error = None
    healthy = StringIO()
    # Act
    try:
        write_stream("lost frame", closed)
    except ValueError as exc:
        error = exc
    write_stream("next frame", healthy)
    # Assert
    assert (type(error), "closed" in str(error), healthy.getvalue()) == (
        ValueError,
        True,
        "next frame\n",
    )


def test_failed_protocol_stdout_write_raises_and_keeps_redirects_current():
    # Arrange
    closed = StringIO()
    closed.close()
    error = None
    healthy = StringIO()
    # Act
    with redirect_stdout(closed):
        try:
            render_content("lost frame")
        except ValueError as exc:
            error = exc
    with redirect_stdout(healthy):
        render_content("next frame")
    # Assert
    assert (type(error), "closed" in str(error), healthy.getvalue()) == (
        ValueError,
        True,
        "next frame\n",
    )


@pytest.mark.parametrize("transport", ["stdout", "owned"])
def test_broken_pipe_is_reported_to_the_protocol_caller(transport):
    # Arrange
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    stream = os.fdopen(write_fd, "w")

    # Act
    def emit():
        if transport == "stdout":
            with redirect_stdout(stream):
                render_content("rejected frame")
        else:
            write_stream("rejected frame", stream, flush=True)

    # Assert
    try:
        with pytest.raises(BrokenPipeError):
            emit()
    finally:
        # Closing may retry the buffered frame against the same broken pipe.
        with suppress(BrokenPipeError):
            stream.close()


@pytest.mark.parametrize("capture_prints", [False, True])
def test_output_preserves_diagnostic_destinations_and_quiet_protocols(
    tmp_path, capture_prints
):
    # Arrange
    # An isolated process exercises actual SciTeX console and file handlers,
    # including repeated logger names and a quiet threshold.
    log_path = tmp_path / "diagnostics.log"
    code = """
import sys
import scitex_logging as slogging
from scitex_agent_container._logging import get_logger, render_content, render_rich
slogging.configure(log_file=sys.argv[1], enable_file=True, capture_prints=sys.argv[2] == 'True')
logger = get_logger('output-contract')
logger.info('diagnostic before')
render_rich('requested result', 'output-contract')
logger.info('diagnostic after [Errno 98]')
slogging.set_level(slogging.ERROR)
render_rich('hidden by quiet threshold', 'output-contract')
render_content('{"status":"accepted"}')
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[3] / "src")
    # Act
    result = subprocess.run(
        [sys.executable, "-c", code, str(log_path), str(capture_prints)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    logged = log_path.read_text()
    observed = {
        "requested_result": "requested result" in result.stdout,
        "diagnostic_stdout": "diagnostic" in result.stdout,
        "quiet_output": "hidden by quiet threshold" in result.stdout,
        "protocol": json.loads(result.stdout.splitlines()[-1]),
        "stderr_before": "diagnostic before" in result.stderr,
        "stderr_after": "diagnostic after [Errno 98]" in result.stderr,
        "file_before": "diagnostic before" in logged,
        "file_after": "diagnostic after [Errno 98]" in logged,
        "protocol_archived": '"status"' in logged,
    }
    # Assert
    assert observed == {
        "requested_result": True,
        "diagnostic_stdout": False,
        "quiet_output": False,
        "protocol": {"status": "accepted"},
        "stderr_before": True,
        "stderr_after": True,
        "file_before": True,
        "file_after": True,
        "protocol_archived": False,
    }
