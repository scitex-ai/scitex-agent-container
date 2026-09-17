"""Atomic bracketed-paste contract for long TUI turns."""

from __future__ import annotations

import shutil
import subprocess
import uuid
from typing import Any

import pytest

from scitex_agent_container._runners._tmux.tmux import TmuxManager, TmuxPasteError


class _RunnerRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: list[str], **kwargs: object) -> object | None:
        self.calls.append((list(argv), dict(kwargs)))
        return None


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


class _FailPasteRunner(_RunnerRecorder):
    def __call__(self, argv: list[str], **kwargs: object) -> _Result:
        super().__call__(argv, **kwargs)
        return _Result(1 if argv[1] == "paste-buffer" else 0)


class _RealBufferProbe:
    def __init__(self) -> None:
        self.loaded = ""

    def __call__(self, argv: list[str], **kwargs: Any) -> object:
        result = subprocess.run(argv, **kwargs)
        if argv[1] == "load-buffer" and result.returncode == 0:
            self.loaded = subprocess.run(
                ["tmux", "show-buffer", "-b", argv[3]],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        return result


def test_three_kib_multiline_prompt_loads_byte_identically_via_stdin() -> None:
    # Arrange
    prompt = "\n".join(f"step {index}: " + ("payload " * 12) for index in range(40))
    runner = _RunnerRecorder()

    # Act
    TmuxManager.send_text_literal(
        "tui-handyman-01",
        prompt,
        runner=runner,
        buffer_name="sac-test-buffer",
    )

    # Assert
    assert (
        len(prompt) >= 3_000,
        runner.calls[0][0],
        runner.calls[0][1].get("input"),
        any(prompt in argument for argv, _kwargs in runner.calls for argument in argv),
    ) == (
        True,
        ["tmux", "load-buffer", "-b", "sac-test-buffer", "-"],
        prompt,
        False,
    )


def test_one_bracketed_paste_targets_exact_pane_then_deletes_buffer() -> None:
    # Arrange
    runner = _RunnerRecorder()

    # Act
    TmuxManager.send_text_literal(
        "tui-handyman-01",
        "one payload",
        runner=runner,
        buffer_name="sac-test-buffer",
    )

    # Assert
    assert [argv for argv, _kwargs in runner.calls] == [
        ["tmux", "load-buffer", "-b", "sac-test-buffer", "-"],
        [
            "tmux",
            "paste-buffer",
            "-p",
            "-r",
            "-b",
            "sac-test-buffer",
            "-t",
            "=tui-handyman-01:",
        ],
        ["tmux", "delete-buffer", "-b", "sac-test-buffer"],
    ]


def test_failed_paste_raises_and_still_deletes_buffer() -> None:
    # Arrange
    runner = _FailPasteRunner()

    # Act
    try:
        TmuxManager.send_text_literal(
            "tui-handyman-01",
            "one payload",
            runner=runner,
            buffer_name="sac-test-buffer",
        )
        error = ""
    except TmuxPasteError as exc:
        error = str(exc)

    # Assert
    assert (
        "bracket-paste" in error,
        [argv[1] for argv, _kwargs in runner.calls],
    ) == (
        True,
        ["load-buffer", "paste-buffer", "delete-buffer"],
    )


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_real_tmux_buffer_preserves_three_kib_multiline_prompt_byte_for_byte() -> None:
    # Arrange
    session = f"sac-paste-{uuid.uuid4().hex[:8]}"
    prompt = "α-long\n" + ("payload-0123456789\n" * 180)
    probe = _RealBufferProbe()
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", session, "sleep", "30"],
        check=True,
        capture_output=True,
    )

    # Act
    try:
        TmuxManager.send_text_literal(
            session,
            prompt,
            runner=probe,
            buffer_name="sac-real-probe-buffer",
        )
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session}:"],
            check=False,
            capture_output=True,
        )

    # Assert
    assert probe.loaded == prompt
