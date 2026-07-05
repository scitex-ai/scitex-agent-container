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

import os
import shutil
import signal
import subprocess
from pathlib import Path

from ..config import AgentConfig

# Re-exported for back-compat — extracted to _apptainer_build, still
# imported here so existing `mod._build_sif_* / mod._safe_image_tag`
# references keep resolving.
from ._apptainer_build import (  # noqa: F401
    _build_sif_from_def,
    _build_sif_from_uri,
    _create_overlay_image,
    _listen_token_path,
    _read_listen_bearer,
    _safe_image_tag,
)
from ._apptainer_build import resolve_sif as _resolve_sif

# Task #21 split — argv assembly + module-level constants extracted to
# _apptainer_build_argv.py. Re-exported here so existing imports like
# ``from runtimes._apptainer_runtime import RUNNER_MODULE`` keep
# resolving (see _apptainer_build_argv module docstring for the
# motivation — pre-task-#21 this file was 47 lines over the 512-line
# cap; the bind/argv method was the dominant block, ~290 LoC).
from ._apptainer_build_argv import (  # noqa: F401
    QUOTA_CACHE_CONTAINER_PATH,
    QUOTA_CACHE_HOST_PATH_DEFAULT,
    QUOTA_CACHE_HOST_PATH_ENV,
    RUNNER_MODULE,
    _resolve_quota_cache_host_path,
)
from ._apptainer_build_argv import (
    build_run_argv as _build_run_argv_impl,
)
from .base import RuntimeBase

DEFAULT_SIF_NAME = "scitex-agent-container.sif"

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
        result and writes its PID file. Implementation extracted to
        :func:`_apptainer_build_argv.build_run_argv` so this runtime
        module stays under the 512-line per-file cap (task #21 — the
        method's argv-assembly body was ~290 LoC).
        """
        return _build_run_argv_impl(
            config,
            state_dir=state_dir,
            sif_path=sif_path,
            runner_argv=runner_argv,
            one_shot=getattr(self, "_one_shot", False),
        )

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

        Thin delegate over :func:`_apptainer_build.resolve_sif` — this
        method only supplies the per-agent image cache dir.
        """
        if shutil.which("apptainer") is None:
            return None
        cache_dir = self._image_cache_dir(config)
        cache_dir.mkdir(parents=True, exist_ok=True)
        return _resolve_sif(config, cache_dir)

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
        if shutil.which("apptainer") is None and not dry_run:
            # Fail loud (clew handoff 2026-05-31 P1): the silent
            # ``return False`` used to bubble up as a generic
            # ``Failed to start agent`` with no diagnostic. Name both
            # real causes — host install missing, or nested-SIF
            # without ``spec.apptainer.nested_mode: "escape"`` (P2).
            # ``dry_run`` bypasses the guard: dry-run only emits argv,
            # so a dev box / CI runner without apptainer can still
            # validate the argv path; the real run still raises.
            raise RuntimeError(
                "apptainer binary not found on $PATH — cannot start "
                f"agent '{config.name}'. Causes: (1) apptainer is not "
                "installed on this host (install via `apt-get install "
                "apptainer` or the apptainer/ppa); (2) running INSIDE "
                "a SIF that does not bundle apptainer on PATH, i.e. a "
                "nested-apptainer self-spawn without "
                '`spec.apptainer.nested_mode: "escape"` set in the '
                "agent's spec.yaml (escape forwards the inner "
                "`apptainer exec` to the bare host)."
            )

        state_dir = self._state_dir(config)
        state_dir.mkdir(parents=True, exist_ok=True)

        # Dry-run on a host without apptainer must still resolve a
        # local ``.sif`` path so the argv-emit completes — the class
        # wrapper short-circuits on missing apptainer, so bypass it.
        if dry_run and shutil.which("apptainer") is None:
            cache_dir = self._image_cache_dir(config)
            cache_dir.mkdir(parents=True, exist_ok=True)
            sif_path = _resolve_sif(config, cache_dir)
        else:
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

        # Build the host-side ``apptainer`` PROCESS env from a GENERIC,
        # name-agnostic allowlist (operator directive 2026-07-05) — NOT
        # the full ambient ``os.environ``. ``minimal_launch_env`` keeps
        # only generic system + apptainer-owned namespaces, so no stale
        # ambient var of ANY name reaches the apptainer process (and thus
        # the container). This is defense-in-depth behind the primary fix
        # (``--cleanenv`` is now unconditionally in the argv — see
        # ``_apptainer_iso_flags``); sac's code names ZERO downstream vars.
        #
        # Then append the host ``~/.cargo/bin`` to the CONTAINER PATH via
        # apptainer's ``APPTAINERENV_APPEND_PATH`` directive, set on the
        # apptainer HOST process env (NOT a ``--env`` flag — that sets a
        # container var and ``--env PATH=...`` would clobber PATH). Lets
        # host-only cargo CLIs (e.g. rtk) resolve inside the container.
        # Skip-if-missing + append-not-clobber live in the pure helper.
        # ``APPTAINERENV_APPEND_PATH`` survives ``--cleanenv`` (verified),
        # so relaxed agents keep their cargo-bin PATH append.
        from ._apptainer_host_env import (
            host_cargo_bin_append_env,
            minimal_launch_env,
        )

        launch_env = minimal_launch_env(os.environ)
        launch_env.update(host_cargo_bin_append_env(launch_env))

        # Jailed-capsule guardrail: strip the apptainer/singularity bind
        # env vars from the launch environment so NO env-injected bind
        # survives (--containall drops apptainer's DEFAULT auto-binds but
        # not an APPTAINER_BIND-injected one). build_run_argv already
        # fail-loud-rejects a forbidden env bind before we get here; this
        # scrub is the belt-and-suspenders removal for a jailed capsule.
        from ._apptainer_jail import is_jailed, scrub_bind_env

        if is_jailed(config):
            scrub_bind_env(launch_env)

        if foreground:
            return subprocess.run(argv, env=launch_env).returncode == 0

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
                env=launch_env,
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


__all__ = [
    "ApptainerContainerRuntime",
    "APPTAINER_PID_FILE",
    "APPTAINER_LOG_FILE",
]
