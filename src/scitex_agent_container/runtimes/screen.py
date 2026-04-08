"""Screen session management utilities."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class ScreenManager:
    """Helpers for GNU screen session lifecycle."""

    @staticmethod
    def exists(session_name: str) -> bool:
        """Check if a screen session exists."""
        result = subprocess.run(
            ["screen", "-ls", session_name],
            capture_output=True,
            text=True,
        )
        return session_name in result.stdout

    @staticmethod
    def start(session_name: str, command: str, workdir: str, env_exports: str = "") -> bool:
        """Launch a command inside a new detached screen session.

        Args:
            session_name: Name for the screen session.
            command: Shell command to execute.
            workdir: Working directory for the command.
            env_exports: Newline-separated export statements to prepend.

        Returns:
            True if the screen session was created successfully.
        """
        shell_script = (
            f"cd '{workdir}' || exit 1\n"
            f"{env_exports}\n"
            f"export CLAUDE_DISABLE_AUTO_UPDATE=1\n"
            f"exec {command}\n"
        )
        # Use login shell (-l) so that ~/.bash_profile, module loads, and
        # LD_LIBRARY_PATH are set correctly (e.g. HPC environments).
        subprocess.run(
            ["screen", "-dmS", session_name, "bash", "-l", "-c", shell_script],
            check=False,
        )

        # Give it a moment to start
        import time
        time.sleep(2)

        return ScreenManager.exists(session_name)

    @staticmethod
    def stop(session_name: str) -> bool:
        """Terminate a screen session."""
        if not ScreenManager.exists(session_name):
            return False
        subprocess.run(
            ["screen", "-S", session_name, "-X", "quit"],
            capture_output=True,
            check=False,
        )
        return True

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str:
        """Capture recent output from a screen session via hardcopy."""
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as tmp:
            tmp_path = tmp.name

        subprocess.run(
            ["screen", "-S", session_name, "-X", "hardcopy", "-h", tmp_path],
            capture_output=True,
            check=False,
        )

        log_path = Path(tmp_path)
        if log_path.exists():
            content = log_path.read_text()
            log_path.unlink(missing_ok=True)
            log_lines = content.splitlines()
            return "\n".join(log_lines[-lines:])

        return ""

    @staticmethod
    def attach(session_name: str) -> None:
        """Attach to a screen session (replaces current process stdin/stdout)."""
        import os
        os.execvp("screen", ["screen", "-r", session_name])
