"""Docker container runtime adapter."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase


class DockerRuntime(RuntimeBase):
    """Runtime for running agents inside Docker containers."""

    def _container_name(self, config: AgentConfig) -> str:
        return f"sac-{config.name}"

    def _build_docker_args(self, config: AgentConfig) -> list[str]:
        """Build the docker run argument list."""
        container_name = self._container_name(config)
        args = ["run", "-d", "--name", container_name]

        # Network
        if config.container.network != "none":
            args.extend(["--network", config.container.network])

        # Mount workdir
        workdir = config.expanded_workdir
        args.extend(["-v", f"{workdir}:/workspace"])

        # Mount claude config if it exists
        claude_dir = Path.home() / ".claude"
        if claude_dir.is_dir():
            args.extend(["-v", f"{claude_dir}:/home/agent/.claude:ro"])

        # Additional volumes
        for vol in config.container.volumes:
            expanded = vol.replace("~", str(Path.home()))
            args.extend(["-v", expanded])

        # Environment variables
        for key, value in config.env.items():
            args.extend(["-e", f"{key}={value}"])
        args.extend(["-e", "CLAUDE_DISABLE_AUTO_UPDATE=1"])

        # Working directory inside container
        args.extend(["-w", "/workspace"])

        # Image
        args.append(config.container.image)

        # Claude flags (appended after image)
        cmd_flags = []
        cmd_flags.extend(["--model", config.model])
        for channel in config.claude.channels:
            cmd_flags.extend(["--channels", channel])
        for flag in config.claude.flags:
            cmd_flags.append(flag)
        if config.claude.session == "continue":
            cmd_flags.append("--continue")

        args.extend(cmd_flags)
        return args

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
    ) -> bool:
        """Start agent in a Docker container.

        Docker's ``docker rm -f`` already handles the ``force``
        semantics inline, so the flag is accepted for signature
        compatibility with :class:`RuntimeBase`.
        """
        container_name = self._container_name(config)

        # Remove existing container if present
        subprocess.run(
            ["docker", "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )

        args = self._build_docker_args(config)
        result = subprocess.run(
            ["docker"] + args,
            capture_output=True,
            text=True,
        )

        if result.returncode != 0:
            return False

        time.sleep(2)
        return self.is_running(config)

    def stop(self, config: AgentConfig) -> bool:
        """Stop and remove a Docker container."""
        container_name = self._container_name(config)
        subprocess.run(
            ["docker", "stop", container_name], capture_output=True, check=False
        )
        subprocess.run(
            ["docker", "rm", container_name], capture_output=True, check=False
        )
        return True

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Docker container is running."""
        container_name = self._container_name(config)
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return container_name in result.stdout.splitlines()

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Docker container."""
        container_name = self._container_name(config)
        result = subprocess.run(
            ["docker", "logs", "--tail", str(lines), container_name],
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    @staticmethod
    def build_image(
        image: str = "scitex-agent-container:latest", context: str = "."
    ) -> bool:
        """Build a Docker image from the containers/ directory."""
        result = subprocess.run(
            ["docker", "build", "-t", image, context],
            text=True,
        )
        return result.returncode == 0
