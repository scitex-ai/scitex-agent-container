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
from .settings_json import cleanup_settings_json, setup_settings_json
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


def _encode_workdir_for_claude_projects(workdir: str) -> str:
    """Encode a workdir path the way Claude Code names its projects dir.

    Claude Code stores per-project session history under
    ``~/.claude/projects/<encoded>/`` where ``<encoded>`` is the absolute
    workdir with every ``/`` replaced by ``-`` (the leading slash becomes a
    leading ``-``; dot-prefixed path segments like ``.dotfiles`` produce a
    double-dash, which is expected).
    """
    abs_path = str(Path(workdir).expanduser().resolve() if Path(workdir).expanduser().exists() else Path(workdir).expanduser())
    return abs_path.replace("/", "-")


def _session_resumable(
    workdir: str,
    user_home: str | None = None,
    max_age_minutes: int | None = None,
) -> bool:
    """Return True iff Claude Code has a resumable session for ``workdir``.

    A session is considered resumable when
    ``~/.claude/projects/<encoded>/`` exists and contains at least one
    non-empty ``*.jsonl`` transcript. Used by the ``continue-or-new``
    session mode to decide whether ``--continue`` is safe to pass.

    If ``max_age_minutes`` is set, the most-recently-modified jsonl must be
    newer than that many minutes; otherwise returns False (treat as stale).
    """
    import time as _time

    home = Path(user_home) if user_home else Path.home()
    encoded = _encode_workdir_for_claude_projects(workdir)
    proj_dir = home / ".claude" / "projects" / encoded
    if not proj_dir.is_dir():
        return False
    candidates = []
    for entry in proj_dir.glob("*.jsonl"):
        try:
            st = entry.stat()
            if entry.is_file() and st.st_size > 0:
                candidates.append((st.st_mtime, entry))
        except OSError:
            continue
    if not candidates:
        return False
    if max_age_minutes is not None:
        newest_mtime = max(mtime for mtime, _ in candidates)
        age_minutes = (_time.time() - newest_mtime) / 60
        if age_minutes > max_age_minutes:
            logger.info(
                "session age %.1f min > max_age_minutes=%d for %s, treating as stale",
                age_minutes,
                max_age_minutes,
                workdir,
            )
            return False
    return True


def _has_src_files(config: AgentConfig) -> bool:
    """Check if src_CLAUDE.md or src_mcp.json exist next to the YAML."""
    if not config.config_path:
        return False
    defdir = Path(config.config_path).parent
    return (defdir / "src_CLAUDE.md").exists() or (defdir / "src_mcp.json").exists()


class ClaudeCodeRuntime(RuntimeBase):
    """Runtime for launching Claude Code agents in screen sessions."""

    def _build_command(self, config: AgentConfig) -> str:
        """Build the claude CLI command from config.

        Session modes:
          - ``continue-or-new`` (default): pass ``--continue`` only when a
            prior session exists for the workdir; otherwise launch fresh.
            Graceful fallback is silent (logged at info level) so rolling
            restarts preserve /compact history without risking hard failure.
          - ``continue``: always pass ``--continue`` (may fail if no prior
            session — explicit opt-in for callers that want strict resume).
          - ``new``: never pass ``--continue``.
        """
        parts = ["claude"]
        parts.append(f"--model '{config.model}'")

        for flag in config.claude.flags:
            parts.append(flag)

        mode = config.claude.session
        max_age = config.claude.continue_max_age_minutes
        if mode == "continue":
            if max_age is not None and not _session_resumable(config.expanded_workdir, max_age_minutes=max_age):
                logger.warning(
                    "session=continue: session too stale (max_age=%d min) for %s, launching fresh",
                    max_age,
                    config.expanded_workdir,
                )
            else:
                parts.append("--continue")
        elif mode == "continue-or-new":
            if _session_resumable(config.expanded_workdir, max_age_minutes=max_age):
                parts.append("--continue")
                logger.info(
                    "session=continue-or-new: resumable session found for %s, passing --continue",
                    config.expanded_workdir,
                )
            else:
                logger.info(
                    "session=continue-or-new: no resumable session for %s, launching fresh",
                    config.expanded_workdir,
                )
        elif mode == "resume":
            resume_id = config.claude.resume_id.strip()
            if resume_id:
                parts.append(f"--resume '{resume_id}'")
                logger.info(
                    "session=resume: passing --resume %s for %s",
                    resume_id,
                    config.expanded_workdir,
                )
            else:
                # No explicit ID — fall back to --continue (most recent session)
                logger.warning(
                    "session=resume: no resume_id set for %s, falling back to --continue",
                    config.expanded_workdir,
                )
                parts.append("--continue")
        # mode == "new" (or any other): no --continue flag

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

    def _setup_auto_accept_log(self, config: AgentConfig) -> logging.Logger:
        """Create a file logger for auto-accept diagnostics."""
        from datetime import datetime

        log_dir = Path.home() / ".scitex" / "agent-container" / "logs" / config.name
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "auto-accept.log"

        file_logger = logging.getLogger(f"auto-accept.{config.name}")
        file_logger.setLevel(logging.DEBUG)
        # Remove old handlers to avoid duplicates on restart
        file_logger.handlers.clear()
        handler = logging.FileHandler(str(log_file), mode="a")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
        )
        file_logger.addHandler(handler)
        file_logger.info(
            "=== Auto-accept session started at %s ===",
            datetime.now().isoformat(),
        )
        return file_logger

    def _send_auto_accept_keystrokes(self, config: AgentConfig) -> bool:
        """Poll multiplexer content and auto-accept TUI prompts.

        Uses modular prompt handlers from prompts.py. Each handler
        detects a specific prompt and sends the appropriate keystrokes.
        Prompt order is not assumed — all handlers are checked each poll.

        Logs every poll cycle to ~/.scitex/agent-container/logs/{name}/auto-accept.log
        for post-mortem diagnosis of hung agents.

        Returns True if all prompts were accepted, False on timeout.
        """
        from .prompts import PROMPT_HANDLERS, detect_and_respond, is_ready

        if not self._needs_auto_accept(config):
            return True

        flog = self._setup_auto_accept_log(config)
        handler_names = [h.name for h in PROMPT_HANDLERS]
        logger.info(
            "Auto-accepting TUI prompts for %s (handlers: %s)",
            config.screen_name,
            ", ".join(handler_names),
        )
        flog.info("Handlers: %s", ", ".join(handler_names))

        timeout = 90
        start = time.monotonic()
        accepted: set[str] = set()
        mux = self._get_mux(config)
        poll_count = 0
        content_preview = "(not yet polled)"

        def _send(session_name: str, *keys: str) -> None:
            mux.send_keys(session_name, *keys)

        while time.monotonic() - start < timeout:
            poll_count += 1
            elapsed = time.monotonic() - start

            if not mux.exists(config.screen_name):
                msg = f"Session {config.screen_name} disappeared at poll {poll_count} ({elapsed:.0f}s)"
                logger.warning(msg)
                flog.warning(msg)
                return False

            content = self._get_content(config)
            content_preview = content.strip()[:300] if content.strip() else "(empty)"
            flog.debug(
                "Poll %d (%.0fs) accepted=%s content:\n%s",
                poll_count,
                elapsed,
                accepted or "none",
                content_preview,
            )

            # Check if claude is ready (all prompts done)
            if is_ready(content):
                msg = f"Auto-accept complete for {config.screen_name} (accepted: {accepted or 'none'}) after {elapsed:.0f}s"
                logger.info(msg)
                flog.info(msg)
                return True

            # Try each handler against current content
            matched = detect_and_respond(
                content,
                accepted,
                lambda *keys: _send(config.screen_name, *keys),
            )
            if matched:
                accepted.add(matched)
                flog.info(
                    "Matched handler '%s' at poll %d (%.0fs), sent keys",
                    matched,
                    poll_count,
                    elapsed,
                )
                time.sleep(2)
                continue

            time.sleep(2)

        msg = (
            f"TIMEOUT ({timeout}s) for {config.screen_name} "
            f"after {poll_count} polls. accepted={accepted or 'none'}. "
            f"Last content:\n{content_preview}"
        )
        logger.warning(msg)
        flog.warning(msg)
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
        setup_settings_json(config, workdir)

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
        cleanup_settings_json(config, config.expanded_workdir)

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
