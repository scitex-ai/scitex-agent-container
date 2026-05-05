"""Docker container runtime adapter."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase


class DockerRuntime(RuntimeBase):
    """Runtime for running agents inside Docker containers.

    Subclass and override :attr:`BIN` for OCI-compatible alternatives
    (e.g. ``PodmanRuntime``). The CLI surfaces of docker and podman
    overlap on every command this adapter calls, so the only thing
    that needs to change is the binary name.
    """

    #: Container engine binary. Subclasses override (e.g. ``"podman"``).
    BIN: str = "docker"

    def _container_name(self, config: AgentConfig) -> str:
        return f"sac-{config.name}"

    def _build_docker_args(self, config: AgentConfig) -> list[str]:
        """Build the docker run argument list."""
        container_name = self._container_name(config)
        # ``-t`` allocates a TTY without attaching stdin (``-i`` is intentionally
        # omitted because we run detached). Without a TTY, the claude CLI
        # auto-falls to ``--print`` mode and exits immediately demanding
        # stdin/prompt — observed in CI 2026-04-27.
        args = ["run", "-d", "-t", "--name", container_name]

        # Network
        if config.container.network != "none":
            args.extend(["--network", config.container.network])

        # Mount workdir
        workdir = config.expanded_workdir
        args.extend(["-v", f"{workdir}:/workspace"])

        # Mount host's ~/.claude (opt-in, default False). Unconditional
        # mount would leak host identity/skills/MCP/memory into every
        # container — the container is the isolation boundary. Enable
        # per-agent via ``spec.container.mount_host_claude: true``.
        if config.container.mount_host_claude:
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

        # Forward API key if available in host env (CI / tests).
        # The container-side Claude CLI expects ANTHROPIC_API_KEY.
        ci_key = os.environ.get("SCITEX_AGENT_CONTAINER_CI_ANTHROPIC_API_KEY")
        if ci_key:
            args.extend(["-e", f"ANTHROPIC_API_KEY={ci_key}"])

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
            [self.BIN, "rm", "-f", container_name],
            capture_output=True,
            check=False,
        )

        args = self._build_docker_args(config)
        result = subprocess.run(
            [self.BIN] + args,
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
            [self.BIN, "stop", container_name], capture_output=True, check=False
        )
        subprocess.run(
            [self.BIN, "rm", container_name], capture_output=True, check=False
        )
        return True

    def is_running(self, config: AgentConfig) -> bool:
        """Check if the Docker container is running."""
        container_name = self._container_name(config)
        result = subprocess.run(
            [self.BIN, "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
        )
        return container_name in result.stdout.splitlines()

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        """Get logs from the Docker container."""
        container_name = self._container_name(config)
        result = subprocess.run(
            [self.BIN, "logs", "--tail", str(lines), container_name],
            capture_output=True,
            text=True,
        )
        return result.stdout + result.stderr

    @classmethod
    def build_image(
        cls,
        image: str = "scitex-agent-container:latest",
        context: str = ".",
        dockerfile: str | None = None,
    ) -> bool:
        """Build a container image from the given context.

        Uses the subclass's :attr:`BIN` (so ``PodmanRuntime.build_image``
        invokes ``podman build``).

        Args:
            image: ``-t`` tag for the built image.
            context: build context dir (typically ``containers/``).
            dockerfile: optional ``-f`` override; needed when the
                ``containers/`` directory holds more than one
                Dockerfile (F-CS16: cli-tui vs sdk-persistent).
        """
        argv = [cls.BIN, "build", "-t", image]
        if dockerfile is not None:
            argv += ["-f", dockerfile]
        argv.append(context)
        result = subprocess.run(argv, text=True)
        return result.returncode == 0
