"""Claude Code runtime adapter."""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase
from .claude_md import cleanup_claude_md, setup_claude_md
from .mcp_config import cleanup_mcp_config, setup_mcp_config
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
        if not config.claude.auto_accept:
            return False
        dangerous_flags = [
            "--dangerously-skip-permissions",
            "--dangerously-load-development-channels",
        ]
        return any(any(df in f for df in dangerous_flags) for f in config.claude.flags)

    def _get_mux(self, config: AgentConfig) -> type:
        """Get the multiplexer class for this config."""
        from .multiplexer import get_multiplexer

        return get_multiplexer(config)

    def _send_keys(self, config: AgentConfig, *keys: str) -> None:
        """Send keys to the agent's multiplexer session."""
        self._get_mux(config).send_keys(config.screen_name, *keys)

    def _get_content(self, config: AgentConfig) -> str:
        """Capture current content from the agent's multiplexer session."""
        return self._get_mux(config).capture_content(config.screen_name)

    def _wait_for_prompt(
        self, config: AgentConfig, marker: str, timeout: int = 60
    ) -> bool:
        """Poll screen content until a prompt marker appears or timeout."""
        mux = self._get_mux(config)
        start = time.monotonic()
        while time.monotonic() - start < timeout:
            if not mux.exists(config.screen_name):
                return False
            content = self._get_content(config)
            if marker in content:
                return True
            time.sleep(2)
        return False

    def _send_auto_accept_keystrokes(self, config: AgentConfig) -> bool:
        """Poll multiplexer content and auto-accept TUI prompts.

        Uses modular prompt handlers from prompts.py. Each handler
        detects a specific prompt and sends the appropriate keystrokes.
        Prompt order is not assumed — all handlers are checked each poll.

        Returns True if all prompts were accepted, False on timeout.
        """
        from .prompts import PROMPT_HANDLERS, detect_and_respond, is_ready

        if not self._needs_auto_accept(config):
            return True

        handler_names = [h.name for h in PROMPT_HANDLERS]
        logger.info(
            "Auto-accepting TUI prompts for %s (handlers: %s)",
            config.screen_name,
            ", ".join(handler_names),
        )

        timeout = 90
        start = time.monotonic()
        accepted: set[str] = set()
        mux = self._get_mux(config)

        def _send(session_name: str, *keys: str) -> None:
            mux.send_keys(session_name, *keys)

        while time.monotonic() - start < timeout:
            if not mux.exists(config.screen_name):
                logger.warning(
                    "Session %s disappeared during auto-accept",
                    config.screen_name,
                )
                return False

            content = self._get_content(config)
            if content.strip():
                logger.debug(
                    "Pane content for %s:\n%s", config.screen_name, content[:500]
                )
            else:
                logger.debug("Pane content empty for %s", config.screen_name)

            # Check if claude is ready (all prompts done)
            if is_ready(content):
                logger.info(
                    "Auto-accept complete for %s (accepted: %s)",
                    config.screen_name,
                    ", ".join(accepted) or "none needed",
                )
                return True

            # Try each handler against current content
            matched = detect_and_respond(
                content,
                accepted,
                lambda *keys: _send(config.screen_name, *keys),
            )
            if matched:
                accepted.add(matched)
                time.sleep(2)
                continue

            time.sleep(2)

        logger.warning(
            "Timed out (%ds) during auto-accept for %s (accepted: %s)",
            timeout,
            config.screen_name,
            ", ".join(accepted),
        )
        return False

    def _run_startup_commands(self, config: AgentConfig) -> None:
        """Send startup commands to the screen session with delays."""
        for sc in config.startup_commands:
            if sc.delay > 0:
                time.sleep(sc.delay)
            try:
                self._send_keys(config, f"{sc.command}\r")
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
        if self._needs_auto_accept(config):
            accepted = self._send_auto_accept_keystrokes(config)
            if not accepted:
                logger.warning(
                    "Auto-accept failed for %s; skipping startup commands",
                    config.screen_name,
                )
                return
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

        mux = self._get_mux(config)
        started = mux.start(
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

        return self._get_mux(config).stop(config.screen_name)

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

        return self._get_mux(config).exists(config.screen_name)

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

        return self._get_mux(config).capture_logs(config.screen_name, lines)
