"""``build_run_argv`` — extracted from :mod:`_apptainer_runtime`.

The apptainer-runtime module crossed the 512-line per-file cap (task
#21 follow-up to PR #365). The dominant block was the
``build_run_argv`` method — ~290 lines of bind/argv assembly that
already imports every sibling helper (``_apptainer_iso_flags``,
``_apptainer_tmpfs``, ``_apptainer_auth``, ``_apptainer_listen_env``,
``_apptainer_inner_argv``, ``_to_home_overlay``, ``_p3a_default_binds``,
``_apptainer_preflight``). Pulling it out into THIS module collapses
the runtime file under the cap while preserving the same external
surface — ``_apptainer_runtime`` becomes a thin orchestrator that
delegates argv assembly here.

Module-level constants and the quota-cache resolver also live here
now (they're only consumed by ``build_run_argv``) and are re-exported
from ``_apptainer_runtime`` for back-compat — any external import
like ``from runtimes._apptainer_runtime import RUNNER_MODULE``
continues to resolve.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..config import AgentConfig
from ._apptainer_build import _create_overlay_image

# ----------------------------------------------------------------------
# Module-level constants (moved from _apptainer_runtime, re-exported
# there for back-compat).
# ----------------------------------------------------------------------
RUNNER_MODULE = "scitex_agent_container._runners.claude_session"

# Quota-cache visibility (#16) — see the original docstring in
# _apptainer_runtime for the full motivation. Host cron refreshes the
# canonical path every 10 min; the bind is read-only and conditional on
# the file existing so quota-cron-less hosts (CI, fresh installs)
# can still launch agents.
QUOTA_CACHE_HOST_PATH_DEFAULT = "/home/ywatanabe/.scitex/quota-cache.json"
QUOTA_CACHE_CONTAINER_PATH = "/var/sac/quota-cache.json"
QUOTA_CACHE_HOST_PATH_ENV = "SAC_QUOTA_CACHE_HOST_PATH"


def _resolve_quota_cache_host_path() -> Path:
    override = os.environ.get(QUOTA_CACHE_HOST_PATH_ENV, "").strip()
    return Path(override) if override else Path(QUOTA_CACHE_HOST_PATH_DEFAULT)


def build_run_argv(
    config: AgentConfig,
    *,
    state_dir: Path,
    sif_path: Path,
    runner_argv: list[str] | None = None,
    one_shot: bool = False,
    tui: bool = False,
) -> list[str]:
    """Render the ``apptainer exec`` argv.

    Pure function — no subprocess work. Caller backgrounds the
    result and writes its PID file. ``one_shot`` is forwarded to
    :func:`_apptainer_inner_argv.build_inner_argv` when the caller
    doesn't pre-build a ``runner_argv``; it mirrors the legacy
    ``self._one_shot`` flag the class method threaded through.

    ``tui=True`` swaps the inner command from the ``python -m`` SDK
    session runner to the interactive ``claude`` TUI (same isolation /
    binds / overlay / auth / to_home as the SDK path — only the inner
    process differs). The caller (``TuiSessionRuntime``) launches the
    returned argv inside a tmux PTY rather than backgrounding it.
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

    # Agent-supplied environment file. ``deploy_to_home`` materialises each
    # agent's ``to_home/.env`` to ``$HOME/.env`` (== ``home_host/.env`` on
    # the host) BEFORE this argv is built (see TuiSessionRuntime /
    # ClaudeSessionRuntime: deploy_to_home → build_run_argv). The
    # materialised file is never auto-loaded into the agent's process, so we
    # inject it here via apptainer ``--env-file`` — the apptainer-native
    # mechanism that works for BOTH the relaxed and hardened (preflight-
    # wrapped) inner commands. Emitted FIRST among env wiring so every
    # curated ``--env`` below (quota path, state-db, auth, listen, spec.env)
    # OVERRIDES it: the .env supplies per-agent additions (e.g.
    # ``CLAUDE_CODE_TELEGRAMMER_TELEGRAM_BOT_TOKEN``), never sac's critical
    # wiring. Format is plain ``KEY=VALUE`` — apptainer ``--env-file`` is not
    # a shell, so no ``export`` prefix and no quoting of values. No-op when
    # the agent ships no ``.env``.
    env_file = home_host / ".env"
    if env_file.is_file():
        argv += ["--env-file", str(env_file)]

    # Quota-cache bind (#16) — see module-level docstring in
    # _apptainer_runtime. Bind read-only + advertise the in-container
    # path to the telegrammer bridge so its default-path lookup hits
    # the bind without any per-agent spec change.
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
    # P3a-2 (operator directive feedback_scitex_todo_single_shared_store,
    # lead a2a 214dd26d): prepend the fleet-default binds (today:
    # ~/.scitex/todo for scitex-todo's single shared store) so every
    # agent inherits the store mount even when its spec doesn't carry
    # the explicit line. Explicit spec entries to the same destination
    # override the default — see ``_p3a_default_binds``.
    from ._p3a_default_binds import apply_default_binds

    ap_for_binds = getattr(config, "apptainer", None)
    spec_binds = (
        [str(b) for b in getattr(ap_for_binds, "binds", None) or []]
        if ap_for_binds is not None
        else []
    )
    for bs in apply_default_binds(spec_binds):
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

    # TUI parity with the SDK's telegrammer wake (apply_channels →
    # _wire_telegrammer_wake): inject CLAUDE_CODE_TELEGRAMMER_TURN_URL so an
    # inbound Telegram message POSTs to the agent's /v1/turn and wakes an idle
    # session. The TUI telegrammer inherits the container env (same path as its
    # bot token via --env-file), so forward the SAME shared-plan wake URL here.
    # Without it an idle TUI agent never wakes on Telegram (the SDK↔TUI drift).
    if tui:
        from ._apptainer_inner_argv import tui_channel_plan

        _wake_url = tui_channel_plan(config).telegrammer_turn_url
        if _wake_url:
            argv += ["--env", f"CLAUDE_CODE_TELEGRAMMER_TURN_URL={_wake_url}"]

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

    # Designated credentials file (spec.claude.credentials_file) — bound
    # writable at ``$HOME/.claude/.credentials.json``. Emitted LAST among
    # binds (after the overlay-upper-home bind) so the relaxed ``--home``
    # tmpfs / upper-home bind cannot shadow it; last bind to a path wins.
    #
    # apptainer FILE binds need the in-container destination to pre-exist,
    # so first ensure an empty placeholder at the host path backing the
    # container $HOME (overlay upper-home when relaxed-directory-overlay,
    # else the workspace-home bind). Without it a fresh overlay agent FATALs
    # at boot: "destination doesn't exist in container". The placeholder
    # goes in the bind DESTINATION backing, never to_home (whose
    # credential-leak guard refuses .credentials.json). No-op when no creds
    # bind will be emitted.
    from ._apptainer_auth import credentials_file_bind, ensure_credentials_bind_target

    creds_bind = credentials_file_bind(config)
    ensure_credentials_bind_target(
        config,
        home_host=home_host,
        overlay_upper_home=upper_home,
        bind_flags=creds_bind,
    )
    argv += creds_bind

    argv.append(str(sif_path))

    # Inner command (tini-supervised runner). Dispatched on
    # ``config.kind`` via ``_apptainer_inner_argv.build_inner_argv``:
    #   * Agent       → claude_session (--mission / --a2a-* / ...)
    #   * AgentProxy  → a2a_proxy (--upstream / --trust / --redact)
    # ``-s`` registers tini as a child subreaper so it doesn't
    # emit the noisy PID-1 warning under apptainer's setuid wrapper.
    # Local import keeps the helper resolvable even if a formatter
    # auto-removes module-level unused imports during refactors.
    from ._apptainer_inner_argv import RUNNER_MODULE_PROXY, build_inner_argv

    # TUI MCP wiring: the interactive ``claude`` auto-discovers
    # ``.mcp.json`` from its cwd (the project root), NOT from ``$HOME``
    # like the SDK runner. When to_home materialised a ``$HOME/.mcp.json``
    # (workspace-home bind or overlay-upper), pass its in-container path
    # via ``--mcp-config`` so the TUI actually loads those servers.
    tui_mcp_config: str | None = None
    tui_channel_mcp: str | None = None
    tui_dev_channels: str | None = None
    if tui:
        ch = resolve_container_home(config).rstrip("/")
        has_mcp = (home_host / ".mcp.json").is_file() or (
            upper_home is not None and (upper_home / ".mcp.json").is_file()
        )
        if has_mcp:
            tui_mcp_config = f"{ch}/.mcp.json"
        # SDK-parity channels: spec.claude.channels → dev-channels flag +
        # an inline ``sac mcp channel`` subscriber MCP (server:sac only).
        from ._apptainer_inner_argv import tui_channel_config

        tui_dev_channels, tui_channel_mcp = tui_channel_config(config)

    if runner_argv is None:
        inner_argv = build_inner_argv(
            config,
            one_shot=one_shot,
            tui=tui,
            tui_mcp_config=tui_mcp_config,
            tui_channel_mcp=tui_channel_mcp,
            tui_dev_channels=tui_dev_channels,
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


__all__ = [
    "QUOTA_CACHE_CONTAINER_PATH",
    "QUOTA_CACHE_HOST_PATH_DEFAULT",
    "QUOTA_CACHE_HOST_PATH_ENV",
    "RUNNER_MODULE",
    "_resolve_quota_cache_host_path",
    "build_run_argv",
]
