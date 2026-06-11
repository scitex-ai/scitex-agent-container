"""Claude Code tmux runtime — orchestrator.

The pre-SDK ``ClaudeCodeRuntime`` drives the *interactive* ``claude``
TUI through a tmux session (no SDK package, no ``claude -p``). This
file is the orchestrator: it owns ``start`` / ``stop`` / ``is_running``
/ ``logs`` and delegates the heavy lifting to siblings under this
package so the file stays under the 512-LOC discipline cap.

Day-2 (D) cleanup:
* ``src_files`` deploy/cleanup branch removed — the A2A→tmux bridge
  (``_turn_endpoint``) supersedes it as the inbound input channel.
* ``ssh_remote`` dispatch branch removed — cross-host work goes through
  the A2A bridge, not bare-metal SSH.
* The multiplexer-lifecycle path moved to ``_session_lifecycle``.
* The auto-accept polling loop moved to ``_auto_accept_loop``.

What remains here is the runtime-class wiring + container-engine
delegation + post-start orchestration (auto-accept, startup commands,
A2A sidecar).
"""

from __future__ import annotations

import logging
import threading
import time

from ..._network.host_identity import is_local_host
from ...config import AgentConfig
from ...runtimes.a2a_sidecar import start_sidecar as _a2a_start_sidecar
from ...runtimes.a2a_sidecar import stop_sidecar as _a2a_stop_sidecar
from ...runtimes.base import RuntimeBase
from ._auto_accept_loop import send_auto_accept_keystrokes
from ._session_lifecycle import (
    build_command,
    build_env_exports,
    build_env_source_prelude,
    cleanup_workspace,
    needs_auto_accept,
    setup_workspace,
)

logger = logging.getLogger(__name__)


def _should_dispatch_remote(config: AgentConfig) -> bool:
    """True iff the config is remote AND the remote host is not ourselves.

    Day-2: ``spec.remote`` dispatch via SSHRemote was removed. This
    helper survives to log the deprecation cleanly — when a remote
    config sneaks through, log and treat as local (so the runtime path
    is unambiguous). Cross-host work belongs on the A2A bridge.
    """
    if not getattr(config.remote, "is_remote", False):
        return False
    if is_local_host(config.remote.host):
        logger.info(
            "remote.host=%r matches local identity -> falling back to local",
            config.remote.host,
        )
        return False
    logger.warning(
        "remote.host=%r set, but ssh_remote dispatch was removed in Day-2; "
        "treating as local. Use the A2A bridge for cross-host work.",
        config.remote.host,
    )
    return False


class ClaudeCodeRuntime(RuntimeBase):
    """Runtime for launching Claude Code agents in tmux sessions."""

    def _get_mux(self, config: AgentConfig) -> type:
        """Return the multiplexer class for this config."""
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

    def _run_startup_commands(self, config: AgentConfig) -> None:
        """Send startup commands to the screen session with delays.

        Day-2 (D) simplification: the legacy ``_wait_for_ready_state``
        ready-pattern probe (which depended on ``_lifecycle/ready_state``
        — removed post-ba6755e) was dropped. The A2A bridge is the
        modern readiness signal: the agent's first turn confirms
        live-ness without needing the runtime to scan the pane for a
        custom regex.
        """
        startup_spec = getattr(config, "startup", None)
        commands = (
            list(startup_spec.commands)
            if startup_spec and startup_spec.commands
            else list(config.startup_commands)
        )
        mux = self._get_mux(config)
        for sc in commands:
            if sc.delay > 0:
                time.sleep(sc.delay)
            try:
                mux.send_text_and_submit(config.screen_name, sc.command)
                logger.info(
                    "Sent startup command to %s (delay=%ds): %s",
                    config.screen_name,
                    sc.delay,
                    sc.command,
                )
            except Exception:  # stx-allow: fallback (catch-all safety net)
                logger.exception(
                    "Failed to send startup command to %s: %s",
                    config.screen_name,
                    sc.command,
                )

    def _post_start_tasks(self, config: AgentConfig) -> None:
        """Run post-start tasks: auto-accept prompts, startup commands."""
        if needs_auto_accept(config):
            mux = self._get_mux(config)
            accepted = send_auto_accept_keystrokes(config, mux)
            if not accepted:
                logger.warning(
                    "Auto-accept failed for %s; skipping startup commands",
                    config.screen_name,
                )
                return
        self._run_startup_commands(config)

    # ------------------------------------------------------------------
    # Container-runtime delegation
    # ------------------------------------------------------------------

    def _delegate_container(self, config: AgentConfig, verb: str, *args, **kw):
        """Return ``runtime.<verb>(config, ...)`` for the configured engine.

        Returns ``None`` when ``container.runtime == "none"`` so the
        caller falls back to the in-process tmux path.
        """
        runtime = config.container.runtime
        if runtime == "none":
            return None
        if runtime == "docker":
            from ..apptainer import ApptainerRuntime  # noqa: F401  (kept for parity)
            from ..docker import DockerRuntime

            return getattr(DockerRuntime(), verb)(config, *args, **kw)
        if runtime == "podman":
            from ..podman import PodmanRuntime

            return getattr(PodmanRuntime(), verb)(config, *args, **kw)
        if runtime == "apptainer":
            from ..apptainer import ApptainerRuntime

            return getattr(ApptainerRuntime(), verb)(config, *args, **kw)
        return None

    # ------------------------------------------------------------------
    # Public lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
    ) -> bool:
        """Start a Claude Code agent.

        ``dry_run``: materialise the workspace (CLAUDE.md, .mcp.json,
        .env, settings.json) but do NOT launch the multiplexer or the
        Claude Code process. Returns True when prep succeeds.
        """
        if _should_dispatch_remote(config):
            return False  # ssh_remote dispatch removed; logged in helper

        ct_result = self._delegate_container(config, "start")
        if ct_result is not None:
            return ct_result

        cmd = build_command(config)
        env_exports = build_env_exports(config)
        workdir = config.expanded_workdir

        env_source = build_env_source_prelude(workdir)
        env_exports = env_source + ("\n" + env_exports if env_exports else "")

        setup_workspace(config, workdir)

        if dry_run:
            return True

        mux = self._get_mux(config)
        started = mux.start(
            session_name=config.screen_name,
            command=cmd,
            workdir=workdir,
            env_exports=env_exports,
            venv=config.python_venv,
        )

        if started:
            try:
                _a2a_start_sidecar(config)
            except Exception:  # stx-allow: fallback (catch-all)
                logger.exception("a2a sidecar spawn failed for %s", config.name)

            has_tasks = needs_auto_accept(config) or config.startup_commands
            if has_tasks:
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
        if _should_dispatch_remote(config):
            return False

        ct_result = self._delegate_container(config, "stop")
        if ct_result is not None:
            return ct_result

        try:
            _a2a_stop_sidecar(config)
        except Exception:  # stx-allow: fallback (catch-all)
            logger.exception("a2a sidecar stop failed for %s", config.name)

        cleanup_workspace(config, config.expanded_workdir)
        return self._get_mux(config).stop(config.screen_name)

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Claude Code agent is running."""
        if _should_dispatch_remote(config):
            return False

        ct_result = self._delegate_container(config, "is_running")
        if ct_result is not None:
            return ct_result

        return self._get_mux(config).exists(config.screen_name)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Claude Code agent."""
        if _should_dispatch_remote(config):
            return ""

        ct_result = self._delegate_container(config, "logs", lines)
        if ct_result is not None:
            return ct_result

        return self._get_mux(config).capture_logs(config.screen_name, lines)


def start_tmux_runner(
    config: AgentConfig,
    *,
    no_preflight: bool = False,
    force: bool = False,
    dry_run: bool = False,
) -> bool:
    """Entry point used by the lifecycle layer for the tmux runtime.

    Mirrors the SDK runner's ``ClaudeSessionRuntime().start`` signature
    so the dispatcher can call either one through the same shape.
    """
    return ClaudeCodeRuntime().start(
        config, no_preflight=no_preflight, force=force, dry_run=dry_run
    )


__all__ = ["ClaudeCodeRuntime", "start_tmux_runner"]
