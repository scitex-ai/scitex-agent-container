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
            venv_path = Path(venv)
            if not venv_path.is_absolute() and not venv.startswith("~"):
                # Workspace-relative: resolve under workdir on target host.
                activate = Path(workdir) / venv_path / "bin" / "activate"
            else:
                activate = venv_path.expanduser() / "bin" / "activate"
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
    def capture_content(session_name: str) -> str:
        """Capture current screen content via hardcopy."""
        tmp_path = f"/tmp/.screen-hardcopy-{session_name}.txt"
        try:
            Path(tmp_path).unlink(missing_ok=True)
            subprocess.run(
                ["screen", "-S", session_name, "-X", "hardcopy", tmp_path],
                check=False,
                capture_output=True,
                env=_screen_env(),
            )
            import time

            time.sleep(0.5)
            if Path(tmp_path).exists():
                return Path(tmp_path).read_text(errors="replace")
            return ""
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            return ""
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    @staticmethod
    def send_keys(
        session_name: str,
        *keys: str,
        inter_key_delay_s: float | None = None,
        sleep_fn=None,
    ) -> None:
        """Send keys to a screen session via ``stuff`` command.

        ``screen -X stuff`` inserts raw bytes. Unlike tmux, ``Enter`` is
        not a keyword here — the caller must pass the literal ``\\r``
        (or ``\\n``) character when submit is intended. The
        inter-keystroke delay still matters for the same TUI-redraw
        race described in :mod:`tmux`.

        Parameters
        ----------
        session_name:
            screen session name passed verbatim to ``-S``.
        keys:
            Raw strings to stuff. Each is one ``screen -X stuff`` call.
        inter_key_delay_s:
            Seconds between stuff calls. ``None`` uses
            ``_DEFAULT_INTER_KEY_DELAY_S``. No sleep after the last.
        sleep_fn:
            Injected sleep for tests. Defaults to ``time.sleep``.
        """
        import time as _time

        from .tmux import _DEFAULT_INTER_KEY_DELAY_S  # shared default

        delay = (
            _DEFAULT_INTER_KEY_DELAY_S
            if inter_key_delay_s is None
            else inter_key_delay_s
        )
        sleep = sleep_fn or _time.sleep
        key_list = list(keys)
        for i, key in enumerate(key_list):
            subprocess.run(
                ["screen", "-S", session_name, "-X", "stuff", key],
                check=False,
                capture_output=True,
                env=_screen_env(),
            )
            if delay > 0 and i < len(key_list) - 1:
                sleep(delay)

    @staticmethod
    def send_text_and_submit(
        session_name: str,
        text: str,
        *,
        settle_s: float | None = None,
        sleep_fn=None,
    ) -> None:
        """Send text, let the TUI settle, then stuff a carriage return.

        Complement of :meth:`TmuxManager.send_text_and_submit`. Uses
        ``\\r`` (screen has no ``Enter`` keyword) as the submit byte.
        """
        import time as _time

        from .tmux import _DEFAULT_SUBMIT_SETTLE_S

        settle = _DEFAULT_SUBMIT_SETTLE_S if settle_s is None else settle_s
        sleep = sleep_fn or _time.sleep
        subprocess.run(
            ["screen", "-S", session_name, "-X", "stuff", text],
            check=False,
            capture_output=True,
            env=_screen_env(),
        )
        if settle > 0:
            sleep(settle)
        subprocess.run(
            ["screen", "-S", session_name, "-X", "stuff", "\r"],
            check=False,
            capture_output=True,
            env=_screen_env(),
        )

    @staticmethod
    def attach(session_name: str) -> None:
        """Attach to a screen session (replaces current process stdin/stdout)."""
        os.environ["SCREENDIR"] = _SCREENDIR
        os.execvp("screen", ["screen", "-r", session_name])
