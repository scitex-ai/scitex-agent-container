"""Screen session management utilities."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

# Use a consistent screen socket directory across all contexts
# (SSH, local terminal, cron). macOS defaults to /var/folders/...
# when SCREENDIR is not set, which differs from the user's terminal.
_SCREENDIR = str(Path.home() / ".screen")


def _screen_env() -> dict[str, str]:
    """Return env dict with SCREENDIR set consistently."""
    env = dict(os.environ)
    env["SCREENDIR"] = _SCREENDIR
    return env


class ScreenManager:
    """Helpers for GNU screen session lifecycle."""

    @staticmethod
    def exists(session_name: str) -> bool:
        """Check if a screen session exists."""
        result = subprocess.run(
            ["screen", "-ls", session_name],
            capture_output=True,
            text=True,
            env=_screen_env(),
        )
        return session_name in result.stdout

    @staticmethod
    def start(
        session_name: str,
        command: str,
        workdir: str,
        env_exports: str = "",
        venv: str = "",
    ) -> bool:
        """Launch a command inside a new detached screen session.

        Args:
            session_name: Name for the screen session.
            command: Shell command to execute.
            workdir: Working directory for the command.
            env_exports: Newline-separated export statements to prepend.
            venv: Path to virtualenv to activate before running command.

        Returns:
            True if the screen session was created successfully.
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
        # Use login shell (-l) so that ~/.bash_profile, module loads, and
        # LD_LIBRARY_PATH are set correctly (e.g. HPC environments).
        Path(_SCREENDIR).mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["screen", "-dmS", session_name, "bash", "-l", "-c", shell_script],
            check=False,
            env=_screen_env(),
        )

        # Give it a moment to start
        import time

        time.sleep(2)

        return ScreenManager.exists(session_name)

    @staticmethod
    def stop(session_name: str) -> bool:
        """Terminate a screen session reliably.

        Sends ``screen -X quit`` first (polite shutdown). If the session
        is still listed afterwards, falls back to ``pkill -9`` on the
        SCREEN parent process, which matches the session name in its
        command line. Finally runs ``screen -wipe`` to clean up dead
        socket entries.

        Returns True if the session was alive and has been terminated
        (or was already gone by the end), False if the session never
        existed.
        """
        import time

        if not ScreenManager.exists(session_name):
            return False

        subprocess.run(
            ["screen", "-S", session_name, "-X", "quit"],
            capture_output=True,
            check=False,
            env=_screen_env(),
        )
        # Give screen up to ~1.5s to release the socket.
        for _ in range(15):
            time.sleep(0.1)
            if not ScreenManager.exists(session_name):
                return True

        # Escalate: find the SCREEN parent PID by pattern-matching the
        # name in its cmdline and send SIGKILL. ``pkill -f`` matches the
        # full command line, so ``SCREEN -dmS <name>`` gets caught.
        subprocess.run(
            ["pkill", "-9", "-f", f"SCREEN.*{session_name}"],
            capture_output=True,
            check=False,
        )
        subprocess.run(
            ["screen", "-wipe"],
            capture_output=True,
            check=False,
            env=_screen_env(),
        )
        return not ScreenManager.exists(session_name)

    @staticmethod
    def capture_logs(session_name: str, lines: int = 50) -> str:
        """Capture recent output from a screen session via hardcopy."""
        with tempfile.NamedTemporaryFile(mode="r", suffix=".log", delete=False) as tmp:
            tmp_path = tmp.name

        subprocess.run(
            ["screen", "-S", session_name, "-X", "hardcopy", "-h", tmp_path],
            capture_output=True,
            check=False,
            env=_screen_env(),
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
        os.environ["SCREENDIR"] = _SCREENDIR
        os.execvp("screen", ["screen", "-r", session_name])
