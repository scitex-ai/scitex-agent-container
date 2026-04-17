"""Tmux session management utilities."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

# Inter-keystroke delay inside send_keys() and settle delay before
# Enter inside send_text_and_submit(). These defeat the race where
# Claude Code's TUI (Ink / React) hasn't finished rendering the
# previous key before the next one arrives — the symptom the user
# reported as "text lands but the Enter submit fails".
#
# Both values are overridable per-host via env vars so a slow HPC
# login shell (Spartan) can raise them without a code change.
_DEFAULT_INTER_KEY_DELAY_S = float(os.environ.get("SCITEX_AGENT_KEY_DELAY_S", "0.1"))
_DEFAULT_SUBMIT_SETTLE_S = float(os.environ.get("SCITEX_AGENT_SUBMIT_SETTLE_S", "0.3"))


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
    def send_keys(
        session_name: str,
        *keys: str,
        inter_key_delay_s: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Send keys to a tmux session, one ``send-keys`` call per key.

        A small delay between keys defeats the race where the TUI has
        not yet rendered the previous keystroke before the next one
        arrives. Symptom without the delay: sending ``["2", "Enter"]``
        to a radio selector lands "2" but loses Enter when the
        selector's re-render is still in flight.

        Parameters
        ----------
        session_name:
            tmux target passed verbatim to ``-t``.
        keys:
            One or more keystrokes / tmux keyword names (``"Enter"``,
            ``"C-c"``, etc.) or raw text arguments.
        inter_key_delay_s:
            Seconds to sleep between keystrokes. ``None`` (default)
            uses ``_DEFAULT_INTER_KEY_DELAY_S`` which is read from
            ``SCITEX_AGENT_KEY_DELAY_S`` at import time. No sleep after
            the last key.
        sleep_fn:
            Injected sleep — tests pass a stub to avoid real waits.
        """
        delay = (
            _DEFAULT_INTER_KEY_DELAY_S
            if inter_key_delay_s is None
            else inter_key_delay_s
        )
        key_list = list(keys)
        for i, key in enumerate(key_list):
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, key],
                check=False,
                capture_output=True,
            )
            if delay > 0 and i < len(key_list) - 1:
                sleep_fn(delay)

    @staticmethod
    def send_text_and_submit(
        session_name: str,
        text: str,
        *,
        settle_s: float | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        """Send message text, let the TUI settle, then press Enter.

        Preferred over ``send_keys(session, text + "\\r")`` because
        tmux treats the trailing ``\\r`` as raw input rather than the
        ``Enter`` keyword, and Claude Code's TUI occasionally drops it
        during an active re-render (what the user sees as "text
        arrived but submit never fired"). This helper sends the text
        first, waits for the TUI to finish debouncing, then issues a
        separate ``Enter`` keystroke.

        Parameters
        ----------
        session_name:
            tmux target.
        text:
            Message text to type. Do NOT append ``\\r`` / ``\\n``.
        settle_s:
            Seconds to wait between the text and the Enter keystroke.
            ``None`` uses ``_DEFAULT_SUBMIT_SETTLE_S`` (env-overridable
            via ``SCITEX_AGENT_SUBMIT_SETTLE_S``).
        sleep_fn:
            Injected sleep — tests pass a stub to avoid real waits.
        """
        settle = _DEFAULT_SUBMIT_SETTLE_S if settle_s is None else settle_s
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, text],
            check=False,
            capture_output=True,
        )
        if settle > 0:
            sleep_fn(settle)
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            check=False,
            capture_output=True,
        )

    @staticmethod
    def attach(session_name: str) -> None:
        """Attach to a tmux session (replaces current process)."""
        os.execvp("tmux", ["tmux", "attach", "-t", session_name])
