"""Claude Code runtime adapter."""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase
from .claude_md import cleanup_claude_md, setup_claude_md
from .mcp_config import cleanup_mcp_config, setup_mcp_config
from .screen import ScreenManager
from .src_files import (  # noqa: F401
    cleanup_src_claude_md,
    cleanup_src_mcp_json,
    deploy_src_claude_md,
    deploy_src_mcp_json,
)
from .ssh_remote import SSHPreflightError as SSHPreflightError  # noqa: F401
from .ssh_remote import SSHRemote

logger = logging.getLogger(__name__)

# Backward-compatible alias: existing code imports _SSHRemote from this module
_SSHRemote = SSHRemote

# Backward-compatible aliases for extracted functions
_setup_claude_md = setup_claude_md
_cleanup_claude_md = cleanup_claude_md


def _has_src_files(config: AgentConfig) -> bool:
    """Check if src_CLAUDE.md or src_mcp.json exist next to the YAML."""
    if not config.config_path:
        return False
    defdir = Path(config.config_path).parent
    return (defdir / "src_CLAUDE.md").exists() or (defdir / "src_mcp.json").exists()


class ClaudeCodeRuntime(RuntimeBase):
    """Runtime for launching Claude Code agents in screen sessions."""

    def _build_command(self, config: AgentConfig) -> str:
        """Build the claude CLI command from config."""
        parts = ["claude"]
        parts.append(f"--model '{config.model}'")

        for flag in config.claude.flags:
            parts.append(flag)

        if config.claude.session == "continue":
            parts.append("--continue")

        return " ".join(parts)

    def _build_env_exports(self, config: AgentConfig) -> str:
        """Build export statements from env dict.

        Values support:
        - ~ prefix: expanded to $HOME
        - ${VAR} syntax: resolved from os.environ at launch time
        """
        import os as _os
        import re

        def _resolve(val: str) -> str:
            """Expand ~ and ${VAR} references."""
            if val.startswith("~"):
                val = val.replace("~", "$HOME", 1)
            # Resolve ${VAR} from os.environ
            return re.sub(
                r"\$\{(\w+)\}",
                lambda m: _os.environ.get(m.group(1), m.group(0)),
                val,
            )

        lines = []
        for key, value in config.env.items():
            lines.append(f'export {key}="{_resolve(str(value))}"')
        # Channels are passed via the agent YAML env block (e.g.,
        # SCITEX_OROCHI_CHANNELS) and exported above with the rest of
        # config.env.  Do NOT hard-code cross-package vars here.
        return "\n".join(lines)

    # Telegram access.json is not managed by agent-container.
    # Agent-container only passes config via env vars.

    def _needs_auto_accept(self, config: AgentConfig) -> bool:
        """Check if the claude command includes flags that trigger TUI prompts."""
        dangerous_flags = [
            "--dangerously-skip-permissions",
            "--dangerously-load-development-channels",
        ]
        return any(any(df in f for df in dangerous_flags) for f in config.claude.flags)

    def _get_screen_content(self, session_name: str) -> str:
        """Capture current screen content via hardcopy."""
        import tempfile

        with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as f:
            tmp_path = f.name

        try:
            subprocess.run(
                ["screen", "-S", session_name, "-X", "hardcopy", tmp_path],
                check=False,
                capture_output=True,
            )
            time.sleep(0.3)
            return Path(tmp_path).read_text(errors="replace")
        except Exception:
            return ""
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def _wait_for_prompt(
        self, config: AgentConfig, marker: str, timeout: int = 60
    ) -> bool:
        """Poll screen content until a prompt marker appears or timeout."""
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if not ScreenManager.exists(config.screen_name):
                return False
            content = self._get_screen_content(config.screen_name)
            if marker in content:
                return True
            time.sleep(2)
        return False

    def _send_auto_accept_keystrokes(self, config: AgentConfig) -> None:
        """Send keystrokes to accept TUI confirmation prompts in screen.

        Claude Code shows confirmation prompts for dangerous flags. This method
        polls the screen content and responds to whichever prompt appears:
        - y/n prompts (e.g. skip-permissions): send "y\\r"
        - Radio selection prompts (e.g. dev channels): send "\\r" (Enter)
        - Done when the main input prompt appears (bypass permissions)

        Uses polling to handle variable Claude Code startup times.
        """
        if not self._needs_auto_accept(config):
            return

        # Detect which prompts we expect — driven purely by the flag list
        # so any caller (including scitex-orochi) that injects dangerous
        # flags gets the right auto-accept behavior without this runtime
        # having to know which upstream feature caused the flag.
        expect_skip_permissions = any(
            "--dangerously-skip-permissions" in f for f in config.claude.flags
        )
        expect_dev_channels = any(
            "--dangerously-load-development-channels" in f for f in config.claude.flags
        )

        expected = []
        if expect_skip_permissions:
            expected.append("skip-permissions")
        if expect_dev_channels:
            expected.append("load-dev-channels")

        logger.info(
            "Auto-accepting %d TUI confirmation prompt(s) for %s: %s",
            len(expected),
            config.screen_name,
            ", ".join(expected),
        )

        # Poll screen content and respond to prompts as they appear.
        # The "bypass permissions" text in the input bar means we're done.
        timeout = 90
        start = time.monotonic()
        accepted: set[str] = set()

        while time.monotonic() - start < timeout:
            if not ScreenManager.exists(config.screen_name):
                logger.warning(
                    "Screen session %s disappeared during auto-accept",
                    config.screen_name,
                )
                return

            content = self._get_screen_content(config.screen_name)

            # Check if we've reached the main prompt (all prompts accepted)
            if "bypass permissions" in content and "Enter to confirm" not in content:
                logger.info(
                    "Auto-accept complete for %s (accepted: %s)",
                    config.screen_name,
                    ", ".join(accepted) or "none needed",
                )
                return

            # Detect and respond to radio-selection prompt (dev channels)
            # This prompt has "Enter to confirm" and radio options
            if "Enter to confirm" in content and "load-dev-channels" not in accepted:
                time.sleep(1)
                try:
                    subprocess.run(
                        ["screen", "-S", config.screen_name, "-X", "stuff", "\r"],
                        check=False,
                        capture_output=True,
                    )
                    accepted.add("load-dev-channels")
                    logger.info(
                        "Sent auto-accept Enter for load-dev-channels to %s",
                        config.screen_name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send auto-accept to %s", config.screen_name
                    )
                time.sleep(2)
                continue

            # Detect and respond to y/n prompt (skip-permissions)
            # This prompt has "Type 'y'" or similar y/n confirmation text
            if (
                "skip-permissions" in content or "Trust" in content
            ) and "skip-permissions" not in accepted:
                time.sleep(1)
                try:
                    subprocess.run(
                        ["screen", "-S", config.screen_name, "-X", "stuff", "y\r"],
                        check=False,
                        capture_output=True,
                    )
                    accepted.add("skip-permissions")
                    logger.info(
                        "Sent auto-accept y for skip-permissions to %s",
                        config.screen_name,
                    )
                except Exception:
                    logger.exception(
                        "Failed to send auto-accept to %s", config.screen_name
                    )
                time.sleep(2)
                continue

            time.sleep(2)

        logger.warning(
            "Timed out (%ds) during auto-accept for %s (accepted: %s, expected: %s)",
            timeout,
            config.screen_name,
            ", ".join(accepted),
            ", ".join(expected),
        )

    def _run_startup_commands(self, config: AgentConfig) -> None:
        """Send startup commands to the screen session with delays."""
        for sc in config.startup_commands:
            if sc.delay > 0:
                time.sleep(sc.delay)
            try:
                subprocess.run(
                    [
                        "screen",
                        "-S",
                        config.screen_name,
                        "-X",
                        "stuff",
                        f"{sc.command}\r",
                    ],
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
        """Run post-start tasks: auto-accept prompts, startup commands."""
        self._send_auto_accept_keystrokes(config)
        # Telegram access.json is not managed by agent-container
        self._run_startup_commands(config)

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        """Start a Claude Code agent.

        ``force`` is passed through to SSHRemote.start so the remote
        ``scitex-agent-container start`` call receives ``--force`` and
        stops any existing instance before relaunching.
        """
        if config.remote.is_remote:
            return SSHRemote.start(config, no_preflight=no_preflight, force=force)

        if config.container.runtime != "none":
            from .apptainer import ApptainerRuntime
            from .docker import DockerRuntime

            if config.container.runtime == "docker":
                return DockerRuntime().start(config)
            elif config.container.runtime == "apptainer":
                return ApptainerRuntime().start(config)

        cmd = self._build_command(config)
        env_exports = self._build_env_exports(config)
        workdir = config.expanded_workdir

        # v2: deploy src files from definition directory
        # v1: generate from config (legacy)
        is_v2 = bool(config.mcp_servers) or _has_src_files(config)
        if is_v2:
            deploy_src_claude_md(config, workdir)
            deploy_src_mcp_json(config, workdir)
        else:
            _setup_claude_md(config, workdir)
        setup_mcp_config(config, workdir)

        started = ScreenManager.start(
            session_name=config.screen_name,
            command=cmd,
            workdir=workdir,
            env_exports=env_exports,
            venv=config.venv,
        )

        if started:
            has_tasks = self._needs_auto_accept(config) or config.startup_commands
            if has_tasks:
                # Run post-start tasks in a foreground thread and wait for
                # completion. Using daemon=True would let the CLI exit before
                # auto-accept finishes, killing the thread prematurely.
                thread = threading.Thread(
                    target=self._post_start_tasks,
                    args=(config,),
                    daemon=False,
                    name=f"post-start-{config.screen_name}",
                )
                thread.start()
                thread.join()

        return started

    def stop(self, config: AgentConfig) -> bool:
        """Stop a Claude Code agent."""
        if config.remote.is_remote:
            return SSHRemote.stop(config)

        if config.container.runtime != "none":
            from .apptainer import ApptainerRuntime
            from .docker import DockerRuntime

            if config.container.runtime == "docker":
                return DockerRuntime().stop(config)
            elif config.container.runtime == "apptainer":
                return ApptainerRuntime().stop(config)

        is_v2 = bool(config.mcp_servers) or _has_src_files(config)
        if is_v2:
            cleanup_src_claude_md(config, config.expanded_workdir)
            cleanup_src_mcp_json(config, config.expanded_workdir)
        else:
            _cleanup_claude_md(config, config.expanded_workdir)
        cleanup_mcp_config(config, config.expanded_workdir)

        return ScreenManager.stop(config.screen_name)

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Claude Code agent is running."""
        if config.remote.is_remote:
            return SSHRemote.is_running(config)

        if config.container.runtime == "docker":
            from .docker import DockerRuntime

            return DockerRuntime().is_running(config)
        elif config.container.runtime == "apptainer":
            from .apptainer import ApptainerRuntime

            return ApptainerRuntime().is_running(config)

        return ScreenManager.exists(config.screen_name)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Claude Code agent."""
        if config.remote.is_remote:
            return SSHRemote.logs(config, lines)

        if config.container.runtime == "docker":
            from .docker import DockerRuntime

            return DockerRuntime().logs(config, lines)
        elif config.container.runtime == "apptainer":
            from .apptainer import ApptainerRuntime

            return ApptainerRuntime().logs(config, lines)

        return ScreenManager.capture_logs(config.screen_name, lines)
