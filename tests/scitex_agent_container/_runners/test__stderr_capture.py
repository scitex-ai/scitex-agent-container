"""Unit tests for the runner's stderr-capture helper.

No mocks: the capture path is exercised against the stderr of a REAL
subprocess. A tiny Python program writes a known multi-line traceback to
stderr and exits non-zero; we read that stream line-by-line and feed each
line to ``StderrCapture.callback`` exactly the way the claude-agent-sdk's
``_handle_stderr`` reader does. We then assert the captured text reaches
the enriched failure detail. Pure-function behaviour (placeholder
detection, enrichment, log write) is checked against real strings and a
real ``tmp_path`` directory.

Style: AAA marker comments, descriptive >=3-word names, one assert each.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from scitex_agent_container._runners._stderr_capture import (
    StderrCapture,
    enrich_detail_with_stderr,
    is_sdk_stderr_placeholder,
    write_stderr_log,
)

# A real program that emits a recognisable traceback on stderr and fails.
# Mirrors the kind of crash the claude subprocess produces (a stale-resume
# or a hook traceback) — the actionable reason lives in the stderr tail.
_FAILING_PROGRAM = (
    "import sys\n"
    "print('No conversation found for session abc-123', file=sys.stderr)\n"
    "raise RuntimeError('boom: the real underlying cause')\n"
)

# The SDK placeholder substituted onto ProcessError when stderr was NOT
# piped — the bug this helper exists to defeat.
_PLACEHOLDER_DETAIL = (
    "Command failed (exit code: 1)\nError output: Check stderr output for details"
)


def _run_failing_subprocess_capturing_stderr() -> tuple[StderrCapture, int]:
    """Run the failing program and stream its stderr into a StderrCapture.

    This is the real data path: a child process writes to its stderr fd,
    the parent reads it line-by-line and invokes the per-line callback —
    the same contract the SDK's stderr reader honours. Returns the
    populated capture and the process exit code.
    """
    capture = StderrCapture()
    proc = subprocess.run(
        [sys.executable, "-c", _FAILING_PROGRAM],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in proc.stderr.splitlines():
        capture.callback(line)
    return capture, proc.returncode


class TestStderrCaptureFromRealSubprocess:
    """A real subprocess's stderr survives into the capture buffer."""

    def test_capture_keeps_real_traceback_final_line(self) -> None:
        # Arrange
        # (the failing program is fixed; nothing to set up)
        # Act
        capture, _rc = _run_failing_subprocess_capturing_stderr()
        # Assert
        assert "boom: the real underlying cause" in capture.text()

    def test_capture_keeps_real_stderr_diagnostic_line(self) -> None:
        # Arrange
        # (the failing program is fixed; nothing to set up)
        # Act
        capture, _rc = _run_failing_subprocess_capturing_stderr()
        # Assert
        assert "No conversation found for session abc-123" in capture.text()

    def test_failing_subprocess_exits_nonzero(self) -> None:
        # Arrange
        # (the failing program is fixed; nothing to set up)
        # Act
        _capture, rc = _run_failing_subprocess_capturing_stderr()
        # Assert
        assert rc != 0


class TestEnrichDetailReplacesPlaceholderWithRealStderr:
    """The SDK placeholder is swapped for the captured real stderr."""

    def test_enriched_detail_contains_real_cause(self) -> None:
        # Arrange
        capture, _rc = _run_failing_subprocess_capturing_stderr()
        # Act
        enriched = enrich_detail_with_stderr(_PLACEHOLDER_DETAIL, capture.text())
        # Assert
        assert "boom: the real underlying cause" in enriched

    def test_enriched_detail_drops_the_sdk_placeholder(self) -> None:
        # Arrange
        capture, _rc = _run_failing_subprocess_capturing_stderr()
        # Act
        enriched = enrich_detail_with_stderr(_PLACEHOLDER_DETAIL, capture.text())
        # Assert
        assert "Check stderr output for details" not in enriched

    def test_enriched_detail_keeps_the_exit_code_head(self) -> None:
        # Arrange
        capture, _rc = _run_failing_subprocess_capturing_stderr()
        # Act
        enriched = enrich_detail_with_stderr(_PLACEHOLDER_DETAIL, capture.text())
        # Assert
        assert "Command failed (exit code: 1)" in enriched


class TestEnrichDetailWithoutPlaceholder:
    """Real-stderr-bearing details are augmented, not clobbered."""

    def test_appends_captured_when_detail_lacks_it(self) -> None:
        # Arrange
        detail = "RuntimeError: something else entirely"
        captured = "extra stderr line the sdk omitted"
        # Act
        enriched = enrich_detail_with_stderr(detail, captured)
        # Assert
        assert enriched == f"{detail}\nCaptured stderr: {captured}"

    def test_returns_detail_unchanged_when_already_present(self) -> None:
        # Arrange
        captured = "the only line"
        detail = f"RuntimeError: wrap\nCaptured stderr: {captured}"
        # Act
        enriched = enrich_detail_with_stderr(detail, captured)
        # Assert
        assert enriched == detail

    def test_returns_detail_unchanged_when_nothing_captured(self) -> None:
        # Arrange
        detail = "RuntimeError: no stderr was captured"
        # Act
        enriched = enrich_detail_with_stderr(detail, "")
        # Assert
        assert enriched == detail


class TestPlaceholderDetection:
    """The SDK placeholder string is recognised, real stderr is not."""

    def test_detects_the_sdk_placeholder_string(self) -> None:
        # Arrange
        detail = _PLACEHOLDER_DETAIL
        # Act
        flagged = is_sdk_stderr_placeholder(detail)
        # Assert
        assert flagged is True

    def test_real_stderr_is_not_flagged_as_placeholder(self) -> None:
        # Arrange
        detail = "real: boom"
        # Act
        flagged = is_sdk_stderr_placeholder(detail)
        # Assert
        assert flagged is False


class TestWriteStderrLogToRealDir:
    """The captured stderr is persisted to a real on-disk log file."""

    def test_writes_captured_text_to_log_file(self, tmp_path: Path) -> None:
        # Arrange
        captured = "No conversation found for session abc-123\nboom: real cause"
        # Act
        path = write_stderr_log(tmp_path, captured)
        # Assert
        assert path is not None and "boom: real cause" in path.read_text()

    def test_returns_none_when_nothing_to_write(self, tmp_path: Path) -> None:
        # Arrange
        empty = "   "
        # Act
        path = write_stderr_log(tmp_path, empty)
        # Assert
        assert path is None
