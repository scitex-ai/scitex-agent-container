"""Generic container-engine runtime (F-CS16 phase 2b).

Builds the ``docker run`` (or ``podman run``) argv from an
:class:`AgentConfig` and owns the container's lifecycle by ID:

  * :meth:`start`        spawn the container, capture its ID
  * :meth:`stop`         ``docker stop <id>``
  * :meth:`is_running`   ``docker inspect --format ...``
  * :meth:`logs`         ``docker logs --tail N``

Apptainer dispatch lands as a sibling class in a follow-up — its
argv shape (``apptainer instance start ...``) is different enough to
warrant its own builder.

This module is the pure "shell" of phase 2b: it produces argvs and
wraps subprocess calls. Agentic-sugar mounts (skills / hooks /
scripts / mcp), auto-build of missing images, and operator UID
injection land in phases 2c and 2d on top.

The key separation from the existing
:class:`runtimes.claude_session.ClaudeSessionRuntime` is that this
module **never** runs Python code on the host — every ``start``
goes through ``docker run``; ``state_dir`` and ``workdir`` are
bind-mounted into the container; the runner's PID file becomes
the container ID file at ``<state_dir>/container_id``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase

DEFAULT_IMAGE = "scitex-agent-container:sdk-persistent"
RUNNER_MODULE = "scitex_agent_container._runners.claude_session"


def _operator_uid_gid() -> tuple[int, int]:
    """Return ``(uid, gid)`` for the host operator. Used for ``--user``.

    Posix only. On Windows / WSL the call returns ``(1000, 1000)`` as a
    safe-ish default — the docker daemon there honours the flag the
    same way, even if the numeric pair doesn't correspond to a real
    host account.
    """
    try:
        return os.getuid(), os.getgid()
    except AttributeError:  # stx-allow: fallback (reason: no posix uids on windows)
        return 1000, 1000


def _image_exists_locally(engine: str, image: str) -> bool:
    """``docker image inspect <image>`` returns 0 iff the image is local."""
    result = subprocess.run(
        [engine, "image", "inspect", image],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _build_image(
    engine: str,
    image: str,
    dockerfile: Path,
) -> bool:
    """Run ``docker build -t <image> -f <dockerfile> <context>``.

    Build context is the dockerfile's parent directory by convention —
    sufficient for the F-CS16 yaml shape, where the Dockerfile lives
    alongside the project files it COPYs (or doesn't COPY at all,
    since runtime mounts handle most of that).
    """
    if not dockerfile.is_file():
        return False
    result = subprocess.run(
        [engine, "build", "-t", image, "-f", str(dockerfile), str(dockerfile.parent)]
    )
    return result.returncode == 0


# State-dir sidecar holding the live container ID (one line, no
# trailing newline). Mirrors the existing pid-file pattern in
# _runners._session_state — written on start, read by stop /
# is_running / logs, deleted on stop success.
CONTAINER_ID_FILE = "container_id"


class ContainerRuntime(RuntimeBase):
    """Container-engine runtime.

    ``engine`` selects the binary on $PATH; the argv shape is shared
    between docker and podman (drop-in compatible). Apptainer is a
    separate runtime class.
    """

    def __init__(self, engine: str = "docker") -> None:
        if engine not in ("docker", "podman"):
            raise ValueError(
                f"ContainerRuntime: unsupported engine {engine!r}; "
                "use 'docker' or 'podman'. Apptainer has its own runtime."
            )
        self.engine = engine

    # ------------------------------------------------------------------
    # argv construction
    # ------------------------------------------------------------------

    def build_run_argv(
        self,
        config: AgentConfig,
        *,
        state_dir: Path,
        runner_argv: list[str] | None = None,
    ) -> list[str]:
        """Render the full ``<engine> run`` argv for ``config``.

        Pure function — no subprocess work, no filesystem mutation.
        Phase 2b's caller (``start``) invokes the result via
        ``subprocess.run(..., capture_output=True)`` to capture the
        emitted container ID.

        Args:
            config: the parsed yaml.
            state_dir: per-agent state directory on the host;
                bind-mounted at ``/state`` inside the container so
                state.db / heartbeat / session.jsonl writes land
                where the host's ``sac db query`` can read them.
            runner_argv: optional extra args appended after the
                image. Defaults to the standard SDK runner shape
                (``-m <RUNNER_MODULE> --name <agent>``).
        """
        image = config.image or DEFAULT_IMAGE
        # F-CS16 phase 2d — auto-pass --user $(id -u):$(id -g) so any
        # files the runner writes through /work or /state are owned
        # by the host operator, not root or UID 1000. The yaml can
        # override (e.g. for an image that demands a specific account)
        # by exporting SAC_USER=<spec> before invoking sac, but the
        # default avoids the most common bind-mount permission trap.
        uid, gid = _operator_uid_gid()
        user_spec = os.environ.get("SAC_USER", f"{uid}:{gid}")

        argv: list[str] = [
            self.engine,
            "run",
            "--detach",  # daemon mode; --rm keeps the cleanup path simple
            "--rm",
            "--name",
            config.name,
            "--user",
            user_spec,
            "--mount",
            f"type=bind,src={Path(config.workdir).expanduser()},dst=/work",
            "--mount",
            f"type=bind,src={state_dir.expanduser()},dst=/state",
            "--workdir",
            "/work",
            "--env",
            "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db",
        ]

        for key, val in (config.env or {}).items():
            argv += ["--env", f"{key}={val}"]
        for env_file in config.env_files or []:
            argv += ["--env-file", str(env_file)]

        a2a_port = self._a2a_port(config)
        if a2a_port is not None:
            # Bind to localhost only — peer-to-peer calls from other
            # containers share the docker network instead of the
            # exposed port.
            argv += ["--publish", f"127.0.0.1:{a2a_port}:{a2a_port}"]

        argv += [image]

        # Default runner argv mirrors what bare-metal start currently
        # does for the daemon path (no --print-stream → not foreground).
        if runner_argv is None:
            runner_argv = [
                "--name",
                config.name,
                "--state-root",
                "/state",
            ]
            if a2a_port is not None:
                runner_argv += [
                    "--a2a-port",
                    str(a2a_port),
                    "--a2a-host",
                    "0.0.0.0",
                ]
        argv += list(runner_argv)
        return argv

    @staticmethod
    def _a2a_port(config: AgentConfig) -> int | None:
        a2a = getattr(config, "a2a", None)
        return getattr(a2a, "port", None) if a2a else None

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(
        self,
        config: AgentConfig,
        no_preflight: bool = False,
        force: bool = False,
        dry_run: bool = False,
        foreground: bool = False,
    ) -> bool:
        """Spawn the container; persist its ID alongside other state.

        ``foreground=True`` runs in the caller's terminal (drops
        ``--detach`` and adds ``-it``); the call blocks until the
        container exits and returns True iff exit code 0. Daemon mode
        (the default) returns once the engine has emitted the
        container ID.
        """
        del no_preflight  # no preflight beyond engine availability
        if shutil.which(self.engine) is None:
            return False

        state_dir = self._state_dir(config)
        state_dir.mkdir(parents=True, exist_ok=True)

        # F-CS16 phase 2d — auto-build when the image is missing AND
        # the yaml declares a Dockerfile. dry_run skips this step;
        # Q1=a from the design doc says "auto-build on missing", but
        # dry-running shouldn't actually invoke `docker build`.
        if not dry_run and not self._ensure_image_present(config):
            return False

        # `--name` collisions: docker rejects on duplicate. force=True
        # stops the existing container first so the user gets fresh
        # state without manually scrubbing.
        if force and self.is_running(config):
            self.stop(config)
        elif self.is_running(config):
            return False

        if foreground:
            argv = self.build_run_argv(config, state_dir=state_dir)
            # Swap --detach for -it; subprocess inherits stdio.
            argv = [a for a in argv if a != "--detach"]
            argv.insert(2, "-it")
            if dry_run:
                self._write_dry_run_argv(state_dir, argv)
                return True
            return subprocess.run(argv).returncode == 0

        argv = self.build_run_argv(config, state_dir=state_dir)
        if dry_run:
            self._write_dry_run_argv(state_dir, argv)
            return True

        result = subprocess.run(argv, capture_output=True, text=True)
        if result.returncode != 0:
            return False
        container_id = (result.stdout or "").strip().split("\n")[-1]
        if not container_id:
            return False
        (state_dir / CONTAINER_ID_FILE).write_text(container_id)
        return True

    def stop(self, config: AgentConfig) -> bool:
        cid = self._read_container_id(config)
        if cid is None:
            return True  # nothing to stop is success
        result = subprocess.run(
            [self.engine, "stop", cid], capture_output=True, text=True
        )
        # Whether or not docker returned cleanly, scrub the sidecar so
        # the next start isn't fooled by a stale ID.
        try:
            (self._state_dir(config) / CONTAINER_ID_FILE).unlink()
        except FileNotFoundError:
            pass
        return result.returncode == 0

    def is_running(self, config: AgentConfig) -> bool:
        cid = self._read_container_id(config)
        if cid is None:
            return False
        result = subprocess.run(
            [
                self.engine,
                "inspect",
                "--format",
                "{{.State.Running}}",
                cid,
            ],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        cid = self._read_container_id(config)
        if cid is None:
            return ""
        result = subprocess.run(
            [self.engine, "logs", "--tail", str(lines), cid],
            capture_output=True,
            text=True,
        )
        # Combine stdout + stderr — `docker logs` writes container's
        # stderr to its stderr, not stdout, even with --tail.
        return (result.stdout or "") + (result.stderr or "")

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_image_present(self, config: AgentConfig) -> bool:
        """Auto-build the image when missing locally (F-CS16 phase 2d).

        Returns True iff the image is now available; False on any
        unrecoverable error (build failed, no Dockerfile to build
        from, etc.) so the caller can short-circuit ``start``.
        """
        image = config.image or DEFAULT_IMAGE
        if _image_exists_locally(self.engine, image):
            return True
        # Image missing. Auto-build only if the yaml declared HOW.
        dockerfile_str = (getattr(config, "dockerfile", "") or "").strip()
        if not dockerfile_str:
            return False
        dockerfile = Path(dockerfile_str).expanduser().resolve()
        return _build_image(self.engine, image, dockerfile)

    @staticmethod
    def _state_dir(config: AgentConfig) -> Path:
        """Per-agent state dir on the host (mirrors ``claude_session``)."""
        from .._runners import claude_session as _runner

        try:
            from ..runtimes.claude_session import _project_runtime_root
        except ImportError:  # stx-allow: fallback (reason: import cycle hardening)
            _project_runtime_root = lambda _c: None  # noqa: E731
        return _runner.state_dir_for(config.name, root=_project_runtime_root(config))

    def _read_container_id(self, config: AgentConfig) -> str | None:
        path = self._state_dir(config) / CONTAINER_ID_FILE
        if not path.is_file():
            return None
        cid = path.read_text(encoding="utf-8").strip()
        return cid or None

    def _write_dry_run_argv(self, state_dir: Path, argv: list[str]) -> None:
        """Persist the argv that would run, for inspection."""
        (state_dir / "container_run.argv.txt").write_text("\n".join(argv) + "\n")
