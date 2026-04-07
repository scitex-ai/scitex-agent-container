"""Claude Code runtime adapter."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase
from .screen import ScreenManager

logger = logging.getLogger(__name__)


class ClaudeCodeRuntime(RuntimeBase):
    """Runtime for launching Claude Code agents in screen sessions."""

    def _build_command(self, config: AgentConfig) -> str:
        """Build the claude CLI command from config."""
        parts = ["claude"]
        parts.append(f"--model '{config.model}'")

        for channel in config.claude.channels:
            parts.append(f"--channels {channel}")

        for flag in config.claude.flags:
            parts.append(flag)

        if config.claude.session == "continue":
            parts.append("--continue")

        return " ".join(parts)

    def _build_env_exports(self, config: AgentConfig) -> str:
        """Build export statements from env dict."""
        lines = []
        for key, value in config.env.items():
            lines.append(f'export {key}="{value}"')
        return "\n".join(lines)

    def _watchdog_screen_name(self, config: AgentConfig) -> str:
        """Derive a screen session name for the watchdog."""
        return f"{config.screen_name}-watchdog"

    def _start_watchdog(self, config: AgentConfig) -> bool:
        """Start telegrammer-watchdog in a companion screen session."""
        if not config.watchdog.enabled:
            return True

        watchdog_bin = shutil.which("telegrammer-watchdog")
        if watchdog_bin is None:
            # Try importing the package to resolve the script path
            try:
                from claude_code_telegrammer import get_bin_path

                watchdog_bin = get_bin_path("telegrammer-watchdog")
            except (ImportError, FileNotFoundError):
                logger.warning(
                    "Watchdog enabled but telegrammer-watchdog not found. "
                    "Install claude-code-telegrammer: "
                    "pip install claude-code-telegrammer"
                )
                return False

        wd = config.watchdog
        env_exports = (
            f'export TELEGRAMMER_SESSION="{config.screen_name}"\n'
            f'export TELEGRAMMER_WATCHDOG_INTERVAL="{wd.interval}"\n'
            f'export TELEGRAMMER_RESP_Y_N="{wd.resp_y_n}"\n'
            f'export TELEGRAMMER_RESP_Y_Y_N="{wd.resp_y_y_n}"\n'
            f'export TELEGRAMMER_RESP_WAITING="{wd.resp_waiting}"'
        )

        watchdog_session = self._watchdog_screen_name(config)
        cmd = f"{watchdog_bin} --session {config.screen_name} --interval {wd.interval}"

        started = ScreenManager.start(
            session_name=watchdog_session,
            command=cmd,
            workdir=config.expanded_workdir,
            env_exports=env_exports,
        )

        if started:
            logger.info("Watchdog started in screen session: %s", watchdog_session)
        else:
            logger.error("Failed to start watchdog screen session: %s", watchdog_session)

        return started

    def _stop_watchdog(self, config: AgentConfig) -> bool:
        """Stop the companion watchdog screen session."""
        if not config.watchdog.enabled:
            return True

        watchdog_session = self._watchdog_screen_name(config)
        if ScreenManager.exists(watchdog_session):
            stopped = ScreenManager.stop(watchdog_session)
            if stopped:
                logger.info("Watchdog stopped: %s", watchdog_session)
            return stopped
        return True

    def _setup_telegram_access(self, config: AgentConfig) -> None:
        """Write access.json for Telegram channel if configured."""
        tg = config.telegram
        if not (tg.auto_connect and tg.allowed_users):
            return

        access_dir = Path.home() / ".claude" / "channels" / "telegram"
        access_dir.mkdir(parents=True, exist_ok=True)
        access_file = access_dir / "access.json"

        access_data = {
            "dmPolicy": "pairing",
            "allowFrom": tg.allowed_users,
        }

        access_file.write_text(json.dumps(access_data, indent=2) + "\n")
        logger.info(
            "Telegram access.json written with %d allowed users: %s",
            len(tg.allowed_users),
            access_file,
        )

    def _run_startup_commands(self, config: AgentConfig) -> None:
        """Send startup commands to the screen session with delays.

        Runs in a background thread so start() returns immediately.
        """
        for sc in config.startup_commands:
            if sc.delay > 0:
                time.sleep(sc.delay)
            try:
                subprocess.run(
                    ["screen", "-S", config.screen_name, "-X", "stuff", f"{sc.command}\r"],
                    check=False,
                    capture_output=True,
                )
                logger.info(
                    "Sent startup command to %s (delay=%ds): %s",
                    config.screen_name,
                    sc.delay,
                    sc.command,
                )
            except Exception:
                logger.exception(
                    "Failed to send startup command to %s: %s",
                    config.screen_name,
                    sc.command,
                )

    def _post_start_tasks(self, config: AgentConfig) -> None:
        """Run post-start tasks: telegram setup and startup commands."""
        self._setup_telegram_access(config)
        self._run_startup_commands(config)

    def start(self, config: AgentConfig) -> bool:
        """Start a Claude Code agent."""
        # If container runtime is requested, delegate
        if config.container.runtime != "none":
            from .docker import DockerRuntime
            from .apptainer import ApptainerRuntime

            if config.container.runtime == "docker":
                return DockerRuntime().start(config)
            elif config.container.runtime == "apptainer":
                return ApptainerRuntime().start(config)

        cmd = self._build_command(config)
        env_exports = self._build_env_exports(config)
        workdir = config.expanded_workdir

        started = ScreenManager.start(
            session_name=config.screen_name,
            command=cmd,
            workdir=workdir,
            env_exports=env_exports,
        )

        # Start watchdog after the agent is running
        if started:
            self._start_watchdog(config)

            # Run post-start tasks in background thread
            has_tasks = (
                (config.telegram.auto_connect and config.telegram.allowed_users)
                or config.startup_commands
            )
            if has_tasks:
                thread = threading.Thread(
                    target=self._post_start_tasks,
                    args=(config,),
                    daemon=True,
                    name=f"post-start-{config.screen_name}",
                )
                thread.start()

        return started

    def stop(self, config: AgentConfig) -> bool:
        """Stop a Claude Code agent."""
        # Stop watchdog first
        self._stop_watchdog(config)

        if config.container.runtime != "none":
            from .docker import DockerRuntime
            from .apptainer import ApptainerRuntime

            if config.container.runtime == "docker":
                return DockerRuntime().stop(config)
            elif config.container.runtime == "apptainer":
                return ApptainerRuntime().stop(config)

        return ScreenManager.stop(config.screen_name)

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Claude Code agent is running."""
        if config.container.runtime == "docker":
            from .docker import DockerRuntime
            return DockerRuntime().is_running(config)
        elif config.container.runtime == "apptainer":
            from .apptainer import ApptainerRuntime
            return ApptainerRuntime().is_running(config)

        return ScreenManager.exists(config.screen_name)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Claude Code agent."""
        if config.container.runtime == "docker":
            from .docker import DockerRuntime
            return DockerRuntime().logs(config, lines)
        elif config.container.runtime == "apptainer":
            from .apptainer import ApptainerRuntime
            return ApptainerRuntime().logs(config, lines)

        return ScreenManager.capture_logs(config.screen_name, lines)
