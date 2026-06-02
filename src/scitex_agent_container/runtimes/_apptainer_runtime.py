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
from .base import RuntimeBase

DEFAULT_SIF_NAME = "scitex-agent-container.sif"
RUNNER_MODULE = "scitex_agent_container._runners.claude_session"

# State-dir sidecars. Mirrors the docker-runtime container_id pattern
# but tracks the wrapping shell's PID since apptainer doesn't have a
# container-daemon ID concept.
APPTAINER_PID_FILE = "apptainer_pid"
APPTAINER_LOG_FILE = "stdout.log"

# ----------------------------------------------------------------------
# Quota-cache visibility (#16)
# ----------------------------------------------------------------------
# Every agent SIF needs to see its own account's live quota numbers
# (5h utilization %, 7d utilization %, OAuth TTL hours) so:
#   - the in-container claude-code-telegrammer bot enriches its outbound
#     signature with the live quota (PR-A);
#   - the `sac account quota` helper exposes the same data
#     programmatically to the agent (self-awareness);
#   - every A2A message carries the sender's quota as structured
#     metadata for back-pressure / failover decisions by peers.
#
# The host cron refreshes ``QUOTA_CACHE_HOST_PATH`` every 10 minutes.
# We bind it read-only at a stable container path so both Python
# (`sac account quota`) and the TS bridge see the same file.
#
# Bind is conditional on the host file existing — apptainer errors
# hard on a missing bind source, and a quota-cron-less host (CI,
# fresh install) must still be able to launch agents. The downstream
# consumers (telegrammer, `sac account quota`) all degrade gracefully
# when the file is absent.
QUOTA_CACHE_HOST_PATH_DEFAULT = "/home/ywatanabe/.scitex/quota-cache.json"
QUOTA_CACHE_CONTAINER_PATH = "/var/sac/quota-cache.json"
# Env override for the host-side cache path. Empty / unset → default.
# Honest injection seam (no monkeypatch / mocks): tests redirect via a
# real env mutation through ``env_save_restore``; non-test code on hosts
# with the cron at the canonical path is unaffected.
QUOTA_CACHE_HOST_PATH_ENV = "SAC_QUOTA_CACHE_HOST_PATH"


def _resolve_quota_cache_host_path() -> Path:
    override = os.environ.get(QUOTA_CACHE_HOST_PATH_ENV, "").strip()
    return Path(override) if override else Path(QUOTA_CACHE_HOST_PATH_DEFAULT)


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
        # ADR-0003 D6: runtime/<name>/home/ → /home/agent. to_home/
        # materialises here (see _to_home.py) so SDK $HOME/.claude/
        # discovery works without manual operator config.
        home_host = state_dir.expanduser() / "home"
        home_host.mkdir(parents=True, exist_ok=True)
        argv += ["--bind", f"{home_host}:/home/agent"]

        # Quota-cache bind (#16) — see module-level docstring.
        # Bind read-only + advertise the in-container path to the
        # telegrammer bridge so its default-path lookup hits the bind
        # without any per-agent spec change. The env propagates through
        # apptainer's --env into the inner bun process via the MCP
        # server's stdio spawn (it inherits the agent process env).
        quota_src = _resolve_quota_cache_host_path()
        if quota_src.is_file():
            argv += [
                "--bind",
                f"{quota_src}:{QUOTA_CACHE_CONTAINER_PATH}:ro",
                "--env",
                f"CLAUDE_CODE_TELEGRAMMER_TELEGRAM_QUOTA_CACHE_PATH={QUOTA_CACHE_CONTAINER_PATH}",
            ]

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
        #
        # ADR-0003 D6 follow-up: when the destination is under
        # /home/agent/ (the host-side runtime/<name>/home/ bind), apptainer
        # no longer scaffolds parent directories — the host dir IS the
        # filesystem at /home/agent. We pre-create the parent on the
        # host side so the bind has somewhere to land.
        ap_for_binds = getattr(config, "apptainer", None)
        if ap_for_binds is not None:
            for b in getattr(ap_for_binds, "binds", None) or []:
                bs = str(b)
                if ":" in bs:
                    _, _, rest = bs.partition(":")
                    dst = rest.split(":", 1)[0]
                    if dst.startswith("/home/agent/"):
                        rel = dst[len("/home/agent/") :]
                        (home_host / rel).mkdir(parents=True, exist_ok=True)
                argv += ["--bind", bs]

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
            #
            # Declarative auto-create (see docs/isolation.md §7):
            # ``spec.apptainer.overlay_size`` + the default
            # ``overlay_create_if_missing=True`` flag drives
            # ``apptainer overlay create --size <MB> <path>`` when the
            # overlay file is missing. Without ``overlay_size`` we fail
            # loudly here (FileNotFoundError) instead of letting
            # apptainer error cryptically at exec time.
            overlay = getattr(ap, "overlay", "") or ""
            if overlay:
                overlay_p = Path(overlay).expanduser()
                if not overlay_p.is_absolute():
                    overlay_p = Path(config.workdir).expanduser() / overlay_p
                if not overlay_p.exists():
                    overlay_size = getattr(ap, "overlay_size", "") or ""
                    create_ok = getattr(ap, "overlay_create_if_missing", True)
                    if overlay_size and create_ok:
                        _create_overlay_image(overlay_p, overlay_size)
                    elif overlay_size:
                        raise FileNotFoundError(
                            f"overlay {overlay_p} missing and "
                            "overlay_create_if_missing=false; pre-create with "
                            "`apptainer overlay create --size <MB> <path>` or "
                            "flip overlay_create_if_missing back to true."
                        )
                    else:
                        raise FileNotFoundError(
                            f"overlay {overlay_p} missing; set "
                            "spec.apptainer.overlay_size (e.g. '5G') for "
                            "declarative auto-create, or pre-create with "
                            "`apptainer overlay create`."
                        )
                argv += ["--overlay", str(overlay_p)]

        # Sized /tmp scratch (spec.apptainer.tmpfs_size, default "2G").
        # A --containall container otherwise gets a 64 MB session tmpfs
        # at /tmp, which fills mid-run during the full test suite. The
        # helper emits `--workdir <state_dir>/tmp-scratch` to relocate
        # /tmp + /var/tmp onto the host filesystem (capacity >> 64 MB)
        # and fails loud (TmpfsSpaceError) if that filesystem has less
        # than tmpfs_size free. No-op when tmpfs_size is "" (opt-out) or
        # when the operator already declared --workdir in raw_args. The
        # flag is curated (emitted before raw_args) so an operator's own
        # --workdir still wins via the helper's raw_args skip.
        from ._apptainer_tmpfs import tmpfs_workdir_flags

        argv += tmpfs_workdir_flags(config, state_dir)

        # Anthropic-auth argv — emits the backend wiring (env + creds
        # bind). Branches internally on whether spec.claude.provider is
        # active: provider → API-key backend (ANTHROPIC_BASE_URL +
        # SAC_ANTHROPIC_API_KEY + clean CLAUDE_CONFIG_DIR, OAuth bind
        # skipped); otherwise → the OAuth path (forward host auth env +
        # bind the resolved .credentials.json). Extracted to
        # _apptainer_auth so this runtime file stays under the line cap.
        from ._apptainer_auth import auth_argv

        argv += auth_argv(config, state_dir)

        for key, val in (config.env or {}).items():
            argv += ["--env", f"{key}={val}"]

        # Layer-5 of auto-port-allocation + bus auth — forward the
        # host-stable ``sac listen`` base URL and the host-generated bearer
        # so the in-container ``sac mcp channel`` adapter can reach AND
        # authenticate to the bus. Extracted to ``_apptainer_listen_env`` so
        # the runtime file stays under the line cap; the helper fails loud
        # when ``server:sac`` is registered but the bearer is unresolvable
        # (see its docstring). UNCONDITIONAL w.r.t. the relaxed escape-hatch
        # below: relaxed specs bypass the preflight wrapper but still need
        # bus auth, else their adapter can never subscribe.
        from ._apptainer_listen_env import listen_env_flags

        argv += listen_env_flags(config)

        # v3-realign: spec.apptainer.raw_args (§1 escape-hatch invariant) —
        # appended verbatim after all curated args, before the SIF +
        # inner command. Lets operators bolt on flags sac doesn't model
        # (e.g. ``--userns``, ``--cleanenv``).
        ap_for_raw = getattr(config, "apptainer", None)
        if ap_for_raw is not None:
            for arg in getattr(ap_for_raw, "raw_args", None) or []:
                argv.append(str(arg))

        # Relaxed + directory-overlay + explicit ``--home`` shadows the
        # to_home tree. ``deploy_to_home_overlay`` materialises the tree
        # into ``<overlay>/upper/<container_home>/``, but a raw-arg
        # ``--home /home/agent`` makes apptainer mount a FRESH tmpfs at
        # that path (verified via `mount`: ``tmpfs on /home/agent``),
        # which shadows the overlay's upper-home — so $HOME/.mcp.json,
        # $HOME/CLAUDE.md, $HOME/.claude/ are all silently absent in the
        # container. The SDK runner's ``merge_home_mcp_servers`` then
        # reads an empty ``$HOME/.mcp.json`` and a per-agent MCP (e.g. an
        # agent's own telegrammer bot) never reaches the SDK.
        #
        # Fix: bind the materialised upper-home OVER the container HOME,
        # appended AFTER raw_args so it wins over the ``--home`` tmpfs
        # (apptainer applies user binds after home setup). No-op for
        # non-relaxed / non-directory-overlay specs (resolver returns
        # None) and when the upper-home wasn't materialised.
        from ._to_home_overlay import (
            resolve_container_home,
            resolve_overlay_upper_home,
        )

        upper_home = resolve_overlay_upper_home(config)
        if upper_home is not None and upper_home.is_dir():
            container_home = resolve_container_home(config)
            argv += ["--bind", f"{upper_home}:{container_home}"]

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
        # (see docs/adr/0001-isolation-hardening.md §D2 + §D4).
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


__all__ = [
    "ApptainerContainerRuntime",
    "APPTAINER_PID_FILE",
    "APPTAINER_LOG_FILE",
]
