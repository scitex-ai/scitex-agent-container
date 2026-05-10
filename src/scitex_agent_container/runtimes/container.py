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

DEFAULT_IMAGE = "scitex-agent-container:scitex"
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
        # User selection (default: let the image's USER stand). Newb's
        # working CI pattern showed that overriding --user with the
        # host operator's UID breaks the Anthropic SDK auth — without
        # an /etc/passwd entry for the foreign UID, the SDK's homedir
        # lookup falls back unpredictably and the OAuth credentials
        # file is read but Anthropic responds "Not logged in" on the
        # call. Letting the image's USER drive everything matches
        # newb's reliable behaviour. Set ``SAC_USER=$(id -u):$(id -g)``
        # explicitly when host-UID alignment for /work / /state writes
        # really matters (local-dev convenience; CI is ephemeral so it
        # doesn't).
        user_spec = os.environ.get("SAC_USER")

        argv: list[str] = [
            self.engine,
            "run",
            "--detach",  # daemon mode; lifecycle.stop handles removal
            "--name",
            config.name,
        ]
        if user_spec:
            argv += ["--user", user_spec]
        # Pre-create + loosen permissions on the bind-mount sources.
        # The container runs as the image's ``agent`` user (uid 1000)
        # by default; when sac itself runs on the host as a different
        # UID (e.g. CI runner uid 1001), the container can't write to
        # /work or /state without permissive mode. ``0o777`` is loose
        # but acceptable: the directories already contain
        # operator-only secrets in agent state.db and the host
        # filesystem is the protection boundary; other users on the
        # same host who could read these dirs could already read sac's
        # config. CI runners are single-user ephemeral.
        workdir_host = Path(config.workdir).expanduser()
        state_dir_host = state_dir.expanduser()
        workdir_host.mkdir(parents=True, exist_ok=True)
        state_dir_host.mkdir(parents=True, exist_ok=True)
        try:
            workdir_host.chmod(0o777)
            state_dir_host.chmod(0o777)
        except PermissionError:  # stx-allow: fallback (reason: chmod can fail when sac doesn't own the dir; the bind-mount may still work via group perms)
            pass

        argv += [
            "--mount",
            f"type=bind,src={workdir_host},dst=/work",
            "--mount",
            f"type=bind,src={state_dir_host},dst=/state",
            "--workdir",
            "/work",
            "--env",
            "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db",
        ]

        # Also bind the workdir at its HOST path inside the container,
        # so anything that reads ``cfg.workdir`` (the SDK runner's
        # ``ClaudeAgentOptions.cwd``, hooks, helper scripts) sees the
        # same path on both sides. ``/work`` stays as the canonical
        # in-container working dir; the second mount is just an alias.
        host_workdir_str = str(workdir_host)
        argv += [
            "--mount",
            f"type=bind,src={workdir_host},dst={host_workdir_str}",
        ]

        # spec.user — uniform user override for the container.
        # ""             → image's USER stands (typically `agent`).
        # "host"         → run as host operator's UID:GID. Required when
        #                  spec.mounts grants host-shaped paths and you
        #                  want files written from inside to land as your
        #                  user on the host.
        # "<uid>:<gid>"  → explicit numeric.
        # Falls back to SAC_USER env (legacy local-dev convenience).
        user_field = str(getattr(config, "user", "") or "")
        if user_field == "host":
            argv += ["--user", f"{os.getuid()}:{os.getgid()}"]
        elif user_field:
            argv += ["--user", user_field]
        # SAC_USER env handling already happened upstream of this block.

        # spec.mounts — declarative extra bind mounts. Each entry is
        # {"src": <host>, "dst": <ctr>, "mode": "rw"|"ro"}. Path expansion
        # (~ / $VAR) is applied to BOTH src and dst so YAMLs stay portable
        # — operators write ``${HOME}/proj/foo`` and sac resolves it
        # against the launching shell's environment.
        for m in getattr(config, "mounts", []) or []:
            src = os.path.expandvars(os.path.expanduser(str(m.get("src", ""))))
            dst = os.path.expandvars(os.path.expanduser(str(m.get("dst", ""))))
            mode = m.get("mode", "rw")
            if not src or not dst:
                continue
            entry = f"type=bind,src={src},dst={dst}"
            if mode == "ro":
                entry += ",readonly"
            argv += ["--mount", entry]

        # Forward Anthropic auth — SAC_ANTHROPIC_API_KEY ONLY, and
        # ONLY when the credentials.json path below isn't being used.
        # If both end up set in the container, the SDK's auto-reader
        # picks the env path over the file and Anthropic returns
        # "Not logged in" / 401 for ``sk-ant-oat*`` bearers passed
        # without their refresh_token / expiresAt context. Newb's
        # working runner.py codifies exactly the same rule.
        # We deliberately do NOT forward a host-side ANTHROPIC_API_KEY
        # under any circumstance — see the module-level comment in
        # ``runtimes/_sdk_common.py``.
        sac_val = os.environ.get("SAC_ANTHROPIC_API_KEY")

        # Materialise/mount Pro/Max OAuth credentials.json into the
        # container so the SDK uses the file-based credentials_file
        # flow (Anthropic rejects ``sk-ant-oat01-…`` OAuth tokens
        # passed as bare ``ANTHROPIC_API_KEY`` env without the full
        # refresh_token / expiresAt context).
        #
        # Resolution order (mirrors newb's ``_container_runner``):
        #   1. ``$SAC_CLAUDE_CODE_CREDENTIALS_JSON`` env — full file
        #      content as the var value. Materialise to a 0644
        #      tempfile and bind-mount that. Designed for CI: a
        #      single GH Actions secret is the only required input,
        #      no shell provisioning step needed (and no chance of
        #      writing to ``${HOME}/.claude/.credentials.json`` on
        #      the runner where it could leak between jobs).
        #   2. ``~/.claude/.credentials.json`` exists on the host —
        #      bind-mount the original file (local-dev path).
        #
        # Mount target: ``/home/agent/.claude/.credentials.json`` —
        # the image's ``agent`` user's $HOME (per Dockerfile useradd
        # -m). With the default --user behavior dropped above, the
        # container runs as ``agent`` (uid 1000), so the SDK's
        # ``Path.home()`` resolves to ``/home/agent`` via /etc/passwd.
        #
        # Read-write (no ``:ro``): the SDK refreshes the OAuth access
        # token mid-session and writes the new one back to the file.
        # With a read-only mount the refresh fails and Anthropic
        # responds "Not logged in" on the next call. Containers and
        # CI runners are ephemeral so the leak surface dies with the
        # job.
        cred_mount_src: Path | None = None
        creds_env = os.environ.get("SAC_CLAUDE_CODE_CREDENTIALS_JSON", "").strip()
        if creds_env:
            import tempfile

            tmp = tempfile.NamedTemporaryFile(
                "w", prefix="sac-creds-", suffix=".json", delete=False
            )
            tmp.write(creds_env)
            tmp.close()
            os.chmod(tmp.name, 0o644)
            cred_mount_src = Path(tmp.name)
        else:
            host_creds = Path.home() / ".claude" / ".credentials.json"
            if host_creds.is_file():
                cred_mount_src = host_creds
        if cred_mount_src is not None:
            # The cred-mount target is the agent's effective $HOME inside
            # the container. Resolution order:
            #   1. spec.env.HOME if the YAML overrides it (host-shaped).
            #   2. image default /home/agent.
            # The SDK's Path.home() reads /etc/passwd (image-baked) plus
            # the HOME env, so this matches what the SDK will look for.
            env_home = (getattr(config, "env", None) or {}).get("HOME")
            ctr_home = os.path.expandvars(env_home) if env_home else "/home/agent"
            cred_dst = f"{ctr_home}/.claude/.credentials.json"
            argv += ["-v", f"{cred_mount_src}:{cred_dst}"]

        # Now decide whether to forward SAC_ANTHROPIC_API_KEY into the
        # container. ONLY when the credentials file path is NOT in use
        # — otherwise the SDK's auto-reader picks the env path over
        # the file (per newb's runner.py comment) and Anthropic
        # rejects bearer-without-refresh.
        if sac_val and cred_mount_src is None:
            argv += ["--env", f"SAC_ANTHROPIC_API_KEY={sac_val}"]

        # ${VAR} references in spec.env values are expanded against the
        # launching shell's environment — same convention as spec.mounts.
        for key, val in (config.env or {}).items():
            expanded = os.path.expandvars(str(val))
            argv += ["--env", f"{key}={expanded}"]
        for env_file in config.env_files or []:
            argv += ["--env-file", str(env_file)]

        a2a_port = self._a2a_port(config)
        if a2a_port is not None:
            # Bind to localhost only — peer-to-peer calls from other
            # containers share the docker network instead of the
            # exposed port.
            argv += ["--publish", f"127.0.0.1:{a2a_port}:{a2a_port}"]

        argv += [image]

        if runner_argv is None:
            runner_argv = [
                "--name",
                config.name,
                "--state-root",
                "/state",
            ]
            # Forward the first startup_command as --mission so the SDK
            # has a boot prompt. Without this the runner just heartbeats
            # and 'docker logs' would never see assistant output.
            cmds = list(getattr(config, "startup_commands", []) or [])
            if cmds and getattr(cmds[0], "command", ""):
                runner_argv += ["--mission", cmds[0].command]
                # --print-stream mirrors assistant text to stdout AND
                # signals the runner to ``exit_after`` the mission turn.
                # That's the smoke-test contract — but it's wrong for
                # long-lived agents that listen on A2A or run autonomous
                # drives. Only enable it when neither is on.
                a2a_port_decision = self._a2a_port(config)
                auto_decision = getattr(config, "autonomous", None)
                stay_alive = a2a_port_decision is not None or (
                    auto_decision is not None
                    and getattr(auto_decision, "enabled", False)
                )
                if not stay_alive:
                    runner_argv += ["--print-stream"]
            if a2a_port is not None:
                runner_argv += [
                    "--a2a-port",
                    str(a2a_port),
                    "--a2a-host",
                    "0.0.0.0",
                ]
            # F-CS3 phase 2: forward autonomous spec to the runner.
            auto = getattr(config, "autonomous", None)
            if auto is not None and getattr(auto, "enabled", False):
                runner_argv += [
                    "--autonomous-enabled",
                    "--autonomous-drive-until",
                    auto.drive_until,
                    "--autonomous-max-turns",
                    str(auto.max_turns),
                    "--autonomous-kick-text",
                    auto.kick_text,
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
        # Daemon argv no longer uses --rm so logs survive an unexpected
        # exit. The matching cleanup happens here: best-effort `rm -f`
        # after `stop` so the container name doesn't squat the next
        # start.
        subprocess.run([self.engine, "rm", "-f", cid], capture_output=True, text=True)
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
        """Per-agent state dir on the host."""
        from .._runners import claude_session as _runner
        from ._sdk_common import project_runtime_root

        return _runner.state_dir_for(config.name, root=project_runtime_root(config))

    def _read_container_id(self, config: AgentConfig) -> str | None:
        path = self._state_dir(config) / CONTAINER_ID_FILE
        if not path.is_file():
            return None
        cid = path.read_text(encoding="utf-8").strip()
        return cid or None

    def _write_dry_run_argv(self, state_dir: Path, argv: list[str]) -> None:
        """Persist the argv that would run, for inspection."""
        (state_dir / "container_run.argv.txt").write_text("\n".join(argv) + "\n")
