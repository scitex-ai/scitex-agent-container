"""Apptainer (Singularity) container runtime adapter."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase
from .screen import ScreenManager


class ApptainerRuntime(RuntimeBase):
    """Runtime for running agents inside Apptainer containers."""

    def _resolve_sif(self, config: AgentConfig) -> str:
        """Resolve the SIF image path."""
        image = config.container.image
        if image.endswith(".sif"):
            return str(Path(image).expanduser())
        # Default location
        return str(
            Path(__file__).resolve().parent.parent.parent.parent
            / "containers"
            / "claude-code-container.sif"
        )

    def _build_exec_command(self, config: AgentConfig) -> str:
        """Build the apptainer exec command string."""
        sif_path = self._resolve_sif(config)
        workdir = config.expanded_workdir

        parts = ["apptainer", "exec"]

        # Bind mounts
        parts.extend(["--bind", f"{workdir}:/workspace"])

        # Opt-in: mount host ~/.claude read-only. Default False so the
        # container stays a clean isolation boundary (matches DockerRuntime).
        if config.container.mount_host_claude:
            claude_dir = Path.home() / ".claude"
            if claude_dir.is_dir():
                parts.extend(["--bind", f"{claude_dir}:/home/agent/.claude:ro"])

        for vol in config.container.volumes:
            expanded = vol.replace("~", str(Path.home()))
            parts.extend(["--bind", expanded])

        # Environment variables
        for key, value in config.env.items():
            parts.extend(["--env", f"{key}={value}"])
        parts.extend(["--env", "CLAUDE_DISABLE_AUTO_UPDATE=1"])

        # Forward API key if available in host env (CI / tests).
        # provision_anthropic_auth inside the runner bridges
        # SAC_ANTHROPIC_API_KEY → ANTHROPIC_API_KEY (or synthesises
        # ~/.claude/.credentials.json for OAuth tokens), so forward
        # under the sac-namespaced name.
        ci_key = os.environ.get("SAC_ANTHROPIC_API_KEY")
        if ci_key:
            parts.extend(["--env", f"SAC_ANTHROPIC_API_KEY={ci_key}"])

        # Working directory
        parts.extend(["--pwd", "/workspace"])

        # SIF
        parts.append(sif_path)

        # Claude command
        parts.append("claude")
        parts.extend(["--model", config.model])
        for channel in config.claude.channels:
            parts.extend(["--channels", channel])
        for flag in config.claude.flags:
            parts.append(flag)
        if config.claude.session == "continue":
            parts.append("--continue")

        return " ".join(parts)

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        """Start agent in Apptainer via a screen session.

        ``no_preflight`` / ``force`` are accepted for signature
        compatibility with :class:`RuntimeBase`.
        """
        cmd = self._build_exec_command(config)
        workdir = config.expanded_workdir

        # Apptainer is foreground, so wrap in screen
        return ScreenManager.start(
            session_name=config.screen_name,
            command=cmd,
            workdir=workdir,
        )

    def stop(self, config: AgentConfig) -> bool:
        """Stop Apptainer agent by terminating its screen session."""
        return ScreenManager.stop(config.screen_name)

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Apptainer agent's screen session exists."""
        return ScreenManager.exists(config.screen_name)

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Apptainer agent's screen session."""
        return ScreenManager.capture_logs(config.screen_name, lines)

    @staticmethod
    def build_image(
        def_file: str = "containers/apptainer.def",
        sif_path: str = "containers/claude-code-container.sif",
    ) -> bool:
        """Build an Apptainer SIF image."""
        result = subprocess.run(
            ["apptainer", "build", sif_path, def_file],
            text=True,
        )
        return result.returncode == 0
