"""Apptainer (Singularity) container runtime adapter."""

from __future__ import annotations

import subprocess
import time
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

    def start(self, config: AgentConfig) -> bool:
        """Start agent in Apptainer via a screen session."""
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
