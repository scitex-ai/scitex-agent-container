"""Apptainer container runtime (F-CS18).

Mirrors :class:`runtimes.container.ContainerRuntime` but emits
``apptainer`` argv and tracks instances by PID rather than
container-ID — apptainer doesn't have a docker-style container daemon.

Lifecycle:

* ``start``       backgrounds ``apptainer exec`` with the SDK runner's
                  ``python -m`` invocation as the inner command;
                  captures the wrapping shell's PID into
                  ``<state_dir>/apptainer_pid``.
* ``stop``        ``kill <pid>`` (SIGTERM); ``kill -0`` to verify.
* ``is_running``  ``kill -0`` against the recorded PID.
* ``logs``        tail ``<state_dir>/stdout.log``.

Image resolution:

* ``spec.image`` ending in ``.sif``  → used directly.
* ``spec.image`` starting with ``docker://``  → first run lazily
  caches via ``apptainer build <cached>.sif docker://...`` (requires
  network egress, but no docker daemon).
* ``spec.apptainer.def_file``  → ``apptainer build <cached>.sif <def>``;
  takes precedence over ``spec.image`` when set.

The bind-mount + env shape mirrors ContainerRuntime: ``/work`` and
``/state`` come from the operator's host paths; HOME=/tmp avoids the
no-passwd-entry trap with non-1000 UIDs.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
from pathlib import Path

from ..config import AgentConfig
from .base import RuntimeBase

DEFAULT_SIF_NAME = "scitex-agent-container.sif"
RUNNER_MODULE = "scitex_agent_container._runners.claude_session"

# State-dir sidecars. Mirrors the docker-runtime container_id pattern
# but tracks the wrapping shell's PID since apptainer doesn't have a
# container-daemon ID concept.
APPTAINER_PID_FILE = "apptainer_pid"
APPTAINER_LOG_FILE = "stdout.log"


class ApptainerContainerRuntime(RuntimeBase):
    """Apptainer (Singularity) runtime — backgrounded ``apptainer exec``.

    Built for HPC compute nodes where docker isn't available. The
    operator declares ``spec.runtime: apptainer`` plus either a
    pre-built ``.sif`` path or a ``docker://`` URL apptainer can pull
    natively.
    """

    engine = "apptainer"

    # ------------------------------------------------------------------
    # argv construction
    # ------------------------------------------------------------------

    def build_run_argv(
        self,
        config: AgentConfig,
        *,
        state_dir: Path,
        sif_path: Path,
        runner_argv: list[str] | None = None,
    ) -> list[str]:
        """Render the ``apptainer exec`` argv.

        Pure function — no subprocess work. Caller backgrounds the
        result and writes its PID file.
        """
        # Hardened isolation by default — see _apptainer_iso_flags for the
        # per-flag skip logic (relaxed opt-out, operator-declared raw_args,
        # overlay/writable-tmpfs incompatibility) and docs/isolation.md.
        from ._apptainer_iso_flags import compute_iso_prepend

        ap_iso = getattr(config, "apptainer", None)
        relaxed = bool(getattr(ap_iso, "relaxed", False)) if ap_iso else False

        argv: list[str] = ["apptainer", "exec"]
        argv += compute_iso_prepend(config)
        argv += [
            # Bind-mounts: workdir → <container_workdir>, state_dir → /state/<name>.
            # apptainer accepts the docker syntax for src:dst:[options].
            # The state-dir is mounted at /state/<name> (not /state) so
            # the runner's `state_dir_for(name, root=/state)` resolves
            # to /state/<name> — matching the bind target exactly. If
            # we mounted at /state, state_dir_for would re-append <name>
            # and produce /state/<name>/<name>/, which would land on
            # disk as runtime/<name>/<name>/ — the bug this comment fixes.
            "--bind",
            f"{Path(config.workdir).expanduser()}:{config.apptainer.container_workdir}",
            "--bind",
            f"{state_dir.expanduser()}:/state/{config.name}",
            # Note — no `--env HOME=...`. Apptainer protects HOME from
            # being overridden via --env ("Overriding HOME environment
            # variable with APPTAINERENV_HOME is not permitted") and
            # doesn't have the docker no-passwd-entry trap — it
            # inherits the host's /etc/passwd entry, so HOME points at
            # a real writable home automatically.
            "--env",
            "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db",
            "--pwd",
            config.apptainer.container_workdir,
        ]

        # Extra bind-mounts from spec.container.volumes — `src:dst[:opts]`
        # entries get translated into one `--bind` flag each. Use case:
        # HPC hosts where `$HOME/.cache` is a symlink into a parallel
        # filesystem (e.g. Spartan's `~/.cache -> /data/gpfs/...`).
        # Without binding that filesystem, every `mkdir` inside the
        # container fails because the symlink target is invisible.
        container_spec = getattr(config, "container", None)
        if container_spec is not None:
            for vol in getattr(container_spec, "volumes", None) or []:
                argv += ["--bind", str(vol)]

        # v3-realign: spec.apptainer.binds (promoted from top-level
        # spec.mounts per §3). Strings already in `host:container[:mode]`
        # form — appended verbatim.
        ap_for_binds = getattr(config, "apptainer", None)
        if ap_for_binds is not None:
            for b in getattr(ap_for_binds, "binds", None) or []:
                argv += ["--bind", str(b)]

        # GPU passthrough — apptainer's --nv binds the host CUDA libs
        # and devices into the container. --rocm does the same for AMD.
        # Opt-in only: most agent workloads don't need the GPU and
        # binding it adds startup overhead + a hard dependency on the
        # host driver matching the container's CUDA toolkit.
        ap = getattr(config, "apptainer", None)
        if ap is not None:
            if getattr(ap, "nv", False):
                argv.append("--nv")
            if getattr(ap, "rocm", False):
                argv.append("--rocm")
            # Writable overlay — lets the agent install packages, write
            # caches and persist state while the base SIF stays
            # immutable. Resolution: absolute path used as-is; relative
            # paths are interpreted against the workdir.
            overlay = getattr(ap, "overlay", "") or ""
            if overlay:
                overlay_p = Path(overlay).expanduser()
                if not overlay_p.is_absolute():
                    overlay_p = Path(config.workdir).expanduser() / overlay_p
                argv += ["--overlay", str(overlay_p)]

        # Forward Anthropic auth (mirrors container.py). Order matters:
        # see runtimes/_sdk_common.py:provision_anthropic_auth — when
        # `~/.claude/.credentials.json` exists (Pro/Max OAuth flow), the
        # SDK reads the file directly and a bare `ANTHROPIC_API_KEY`
        # env shadows it (Anthropic rejects sk-ant-oat* OAuth tokens as
        # bare env). So we only pass *pay-per-token* env values; the
        # credentials.json is bind-mounted below.
        for auth_env in ("ANTHROPIC_API_KEY", "SAC_ANTHROPIC_API_KEY"):
            val = os.environ.get(auth_env)
            if val:
                argv += ["--env", f"{auth_env}={val}"]

        # Mount operator's Pro/Max credentials when present (read-only).
        # Target lives under /tmp/ (writable tmpfs / overlay) rather
        # than $HOME — the D2 preflight requires $HOME to be empty, and
        # binding under $HOME would scaffold a host-mirroring directory.
        # CLAUDE_CONFIG_DIR points the SDK at this dir so it finds the
        # credentials file without needing $HOME pollution.
        cred_file = Path.home() / ".claude" / ".credentials.json"
        if cred_file.is_file():
            argv += [
                "--bind",
                f"{cred_file}:/tmp/sac-claude/.credentials.json:ro",
                "--env",
                "CLAUDE_CONFIG_DIR=/tmp/sac-claude",
            ]

        for key, val in (config.env or {}).items():
            argv += ["--env", f"{key}={val}"]

        # Layer-5 of auto-port-allocation: forward the host-stable
        # ``sac listen`` base URL so the per-agent sidecar's
        # ``/.well-known/agent-card.json`` can advertise an ``url``
        # field that survives restarts. Without this, the card's url
        # would be built from the runner's own port (auto-allocated
        # and therefore churning every restart), and any peer that
        # cached the previous card would dangle on its next call.
        from .._listen._config import listen_base_url as _listen_base_url

        argv += ["--env", f"SAC_LISTEN_BASE_URL={_listen_base_url()}"]

        # v3-realign: spec.apptainer.raw_args (§1 escape-hatch invariant) —
        # appended verbatim after all curated args, before the SIF +
        # inner command. Lets operators bolt on flags sac doesn't model
        # (e.g. ``--userns``, ``--cleanenv``).
        ap_for_raw = getattr(config, "apptainer", None)
        if ap_for_raw is not None:
            for arg in getattr(ap_for_raw, "raw_args", None) or []:
                argv.append(str(arg))

        argv.append(str(sif_path))

        # Inner command (tini-supervised runner). Dispatched on
        # ``config.kind`` via ``_apptainer_inner_argv.build_inner_argv``:
        #   * Agent       → claude_session (--mission / --a2a-* / ...)
        #   * AgentProxy  → a2a_proxy (--upstream / --trust / --redact)
        # ``-s`` registers tini as a child subreaper so it doesn't
        # emit the noisy PID-1 warning under apptainer's setuid wrapper.
        # Local import keeps the helper resolvable even if a formatter
        # auto-removes module-level unused imports during refactors.
        from ._apptainer_inner_argv import (
            RUNNER_MODULE_PROXY,
            build_inner_argv,
        )

        if runner_argv is None:
            inner_argv = build_inner_argv(
                config, one_shot=getattr(self, "_one_shot", False)
            )
        else:
            kind = getattr(config, "kind", "Agent")
            module = RUNNER_MODULE_PROXY if kind == "AgentProxy" else RUNNER_MODULE
            inner_argv = [
                "/usr/bin/tini",
                "-s",
                "--",
                "python3",
                "-m",
                module,
            ] + list(runner_argv)

        # D2 — wrap inner cmd with the static $HOME-visibility preflight
        # (see docs/design/2026-05-13-isolation-hardening.md §D2 + §D4).
        # The preflight is `bash -c "<static-script>\nexec <inner-quoted>"`
        # so PID 1 inside the container is still tini (exec replaces the
        # bash process). Skipped under `relaxed: true` — operator opted
        # out of hardened mode.
        if relaxed:
            argv += inner_argv
        else:
            import shlex

            from ._apptainer_preflight import PREFLIGHT_SCRIPT

            inner_str = " ".join(shlex.quote(a) for a in inner_argv)
            argv += ["bash", "-c", f"{PREFLIGHT_SCRIPT}\nexec {inner_str}"]
        return argv

    # ------------------------------------------------------------------
    # SIF resolution
    # ------------------------------------------------------------------

    def resolve_sif(self, config: AgentConfig) -> Path | None:
        """Return the local SIF path for ``config``, building if needed.

        Resolution order:
          1. ``spec.apptainer.def_file`` — build from this .def.
          2. ``spec.image`` is a local ``.sif`` path — use directly.
          3. ``spec.image`` starts with ``docker://`` — cache + build.

        Returns ``None`` on any unrecoverable error (build failed,
        unparseable image reference) so the caller can short-circuit
        ``start`` with a clear message.
        """
        if shutil.which("apptainer") is None:
            return None

        cache_dir = self._image_cache_dir(config)
        cache_dir.mkdir(parents=True, exist_ok=True)

        ap = getattr(config, "apptainer", None)
        def_file_str = getattr(ap, "def_file", "") if ap is not None else ""
        if def_file_str:
            def_file = Path(def_file_str).expanduser().resolve()
            if not def_file.is_file():
                return None
            sif_path = cache_dir / f"{_safe_image_tag(str(def_file))}.sif"
            if sif_path.is_file():
                return sif_path
            return sif_path if _build_sif_from_def(sif_path, def_file) else None

        # v3-realign: prefer spec.apptainer.image; fall back to legacy
        # AgentConfig.image (kept populated for back-compat) and finally
        # to the default sac-scitex SIF path.
        ap_image = getattr(ap, "image", "") if ap is not None else ""
        image = (ap_image or config.image or "").strip()
        if not image:
            return None

        if image.endswith(".sif"):
            sif_path = Path(image).expanduser().resolve()
            return sif_path if sif_path.is_file() else None

        # Sandbox image: a directory tree built via `apptainer build
        # --sandbox`. Used on hosts where /dev/fuse isn't exposed to
        # user namespaces (Spartan compute nodes etc.) — the rootfs
        # is a regular directory tree, no squashfuse needed at exec.
        # Detection: presence of the `.singularity.d/` marker dir.
        candidate = Path(image).expanduser()
        if candidate.is_dir() and (candidate / ".singularity.d").is_dir():
            return candidate.resolve()

        if image.startswith("docker://") or image.startswith("oras://"):
            sif_path = cache_dir / f"{_safe_image_tag(image)}.sif"
            if sif_path.is_file():
                return sif_path
            return sif_path if _build_sif_from_uri(sif_path, image) else None

        # Bare image name without a scheme — assume docker://.
        uri = f"docker://{image}"
        sif_path = cache_dir / f"{_safe_image_tag(uri)}.sif"
        if sif_path.is_file():
            return sif_path
        return sif_path if _build_sif_from_uri(sif_path, uri) else None

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
        one_shot: bool = False,
    ) -> bool:
        del no_preflight
        self._one_shot = one_shot
        if shutil.which("apptainer") is None:
            return False

        state_dir = self._state_dir(config)
        state_dir.mkdir(parents=True, exist_ok=True)

        sif_path = self.resolve_sif(config)
        if sif_path is None:
            return False

        if force and self.is_running(config):
            self.stop(config)
        elif self.is_running(config):
            return False

        argv = self.build_run_argv(config, state_dir=state_dir, sif_path=sif_path)
        if dry_run:
            (state_dir / "apptainer_run.argv.txt").write_text("\n".join(argv) + "\n")
            return True

        if foreground:
            return subprocess.run(argv).returncode == 0

        # Background as a detached child process; capture stdout/stderr
        # to a single tail-able log file. The wrapping shell's PID is
        # what we kill on stop.
        log_path = state_dir / APPTAINER_LOG_FILE
        with open(log_path, "ab") as logfh:
            proc = subprocess.Popen(
                argv,
                stdout=logfh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
        (state_dir / APPTAINER_PID_FILE).write_text(str(proc.pid))
        return True

    def stop(self, config: AgentConfig) -> bool:
        pid = self._read_pid(config)
        if pid is None:
            return True
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # No wait loop yet — sac's outer lifecycle does its own poll.
        try:
            (self._state_dir(config) / APPTAINER_PID_FILE).unlink()
        except FileNotFoundError:
            pass
        return True

    def is_running(self, config: AgentConfig) -> bool:
        pid = self._read_pid(config)
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def logs(self, config: AgentConfig, lines: int = 50) -> str:
        log_path = self._state_dir(config) / APPTAINER_LOG_FILE
        if not log_path.is_file():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        all_lines = text.splitlines()
        return "\n".join(all_lines[-lines:])

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    @staticmethod
    def _state_dir(config: AgentConfig) -> Path:
        from .._runners import claude_session as _runner
        from ._sdk_common import project_runtime_root

        return _runner.state_dir_for(config.name, root=project_runtime_root(config))

    def _read_pid(self, config: AgentConfig) -> int | None:
        path = self._state_dir(config) / APPTAINER_PID_FILE
        if not path.is_file():
            return None
        try:
            return int(path.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            return None

    def _image_cache_dir(self, config: AgentConfig) -> Path:
        """Per-host SIF cache. Lives under the per-agent state dir so
        cleanup follows the agent's lifecycle."""
        return self._state_dir(config) / "images"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_image_tag(reference: str) -> str:
    """Hash an image reference / def-file path to a filename-safe tag.

    apptainer build emits a single .sif per (image, build-time) tuple;
    this hash gives us a deterministic filename so subsequent starts
    skip the rebuild. The full reference is preserved in the .sif
    metadata; the hash is just the cache key.
    """
    digest = hashlib.sha1(reference.encode("utf-8")).hexdigest()[:16]
    return digest


def _build_sif_from_uri(sif_path: Path, uri: str) -> bool:
    """``apptainer build <sif> <uri>`` — pulls + converts an OCI image."""
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["apptainer", "build", str(sif_path), uri])
    return result.returncode == 0


def _build_sif_from_def(sif_path: Path, def_file: Path) -> bool:
    """``apptainer build <sif> <def_file>`` — builds from a .def script.

    No docker daemon required even if the .def starts with
    ``Bootstrap: docker`` — apptainer's docker compatibility runs
    entirely over OCI registry pulls.
    """
    sif_path.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(["apptainer", "build", str(sif_path), str(def_file)])
    return result.returncode == 0


__all__ = [
    "ApptainerContainerRuntime",
    "APPTAINER_PID_FILE",
    "APPTAINER_LOG_FILE",
]
