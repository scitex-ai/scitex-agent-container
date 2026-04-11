"""Tmux session management utilities."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path


class TmuxManager:
    """Helpers for tmux session lifecycle."""

    @staticmethod
    def exists(session_name: str) -> bool:
        """Check if a tmux session exists."""
        result = subprocess.run(
            ["tmux", "has-session", "-t", session_name],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    @staticmethod
    def start(
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
    ) -> bool:
        """Launch a command inside a new detached tmux session.

        Args:
            session_name: Name for the tmux session.
            command: Shell command to execute.
            workdir: Working directory for the command.
            env_exports: Newline-separated export statements to prepend.
            venv: Path to virtualenv to activate before running command.

        Returns:
            True if the tmux session was created successfully.
        """
        venv_activate = ""
        if venv:
            activate = Path(venv).expanduser() / "bin" / "activate"
            venv_activate = f"source '{activate}' || exit 1\n"

        shell_script = (
            f"cd '{workdir}' || exit 1\n"
            f"{venv_activate}"
            f"{env_exports}\n"
            f"export CLAUDE_DISABLE_AUTO_UPDATE=1\n"
            f"exec {command}\n"
        )

        Path(workdir).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "tmux",
                "new-session",
                "-d",
                "-s",
                session_name,
                "bash",
                "-l",
                "-c",
                shell_script,
            ],
            check=False,
        )

        time.sleep(2)
        return TmuxManager.exists(session_name)

    @staticmethod
    def stop(session_name: str) -> bool:
        """Terminate a tmux session.

        Returns True if the session was alive and has been terminated.
        """
        if not TmuxManager.exists(session_name):
            return False

        subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True,
            check=False,
        )

        time.sleep(0.5)
        return not TmuxManager.exists(session_name)

    @staticmethod
    def capture_content(session_name: str) -> str:
        """Capture current pane content via capture-pane."""
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True,
            text=True,
        )
        return result.stdout if result.returncode == 0 else ""

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str:
        """Capture recent output from a tmux session."""
        result = subprocess.run(
            [
                "tmux",
                "capture-pane",
                "-t",
                session_name,
                "-p",
                "-S",
                str(-lines),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return result.stdout
        return ""

    @staticmethod
    def send_keys(session_name: str, *keys: str) -> None:
        """Send keys to a tmux session."""
        for key in keys:
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, key],
                check=False,
                capture_output=True,
            )

    @staticmethod
    def attach(session_name: str) -> None:
        """Attach to a tmux session (replaces current process)."""
        os.execvp("tmux", ["tmux", "attach", "-t", session_name])
