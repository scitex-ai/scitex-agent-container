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

from pathlib import Path

from .._runtime_paths import runtime_base_dir
from ..config import AgentConfig
from ..config._harness_registry import (
    CLAUDE_AGENT_SDK,
    HARNESS_DESCRIPTORS,
    OPENAI_AGENTS,
)
from ._apptainer_overlay import ensure_overlay_dirs, overlay_flags
from ._apptainer_quota_cache import (
    QUOTA_CACHE_CONTAINER_PATH,
    QUOTA_CACHE_HOST_PATH_DEFAULT,
    QUOTA_CACHE_HOST_PATH_ENV,
    _resolve_quota_cache_host_path,
)

# ----------------------------------------------------------------------
# Module-level constants (moved from _apptainer_runtime, re-exported
# there for back-compat). DERIVED from the harness registry (v4 step 4,
# ``config._harness_registry``) — the registry entry is the single
# source for each runner-module path.
# ----------------------------------------------------------------------
RUNNER_MODULE = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].runner_module

# OpenAI harness runner module (scitex-todo card ``openai-compat-2``).
# NOT DISPATCHED from here YET: the v4 step-2 refusal at the top of
# ``build_run_argv`` guards every shape of this argv, so a non-Anthropic
# harness raises instead of dispatching. The registry's ``openai-agents``
# entry carries the real module + argv builder; key-based launch is
# migration step 7
# (card ``sac-v4-layering-refactor-harness-runtime-inference-20260813``).
RUNNER_MODULE_OPENAI = HARNESS_DESCRIPTORS[OPENAI_AGENTS].runner_module

# Quota-cache constants + resolver now live in _apptainer_quota_cache (this
# file sat at the 512-line cap); imported below and re-exported via __all__ so
# both historical import paths keep resolving.


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
    # v4 STEP-2 LOUDNESS (card sac-v4-layering-refactor-harness-runtime-
    # inference-20260813): every shape of this argv launches the CLAUDE
    # harness (TUI, SDK runner, pre-built runner_argv), so a
    # non-Anthropic ``config.harness`` refuses HERE — before any side
    # effect (home mkdir, overlay provisioning, auth provisioning).
    # The old check sat in the pre-built runner_argv branch below and
    # read ``getattr(config, "provider", None)`` — a field the harness
    # rename removed — so it was DEAD, while the auth step later in this
    # function reads ``config.harness`` CORRECTLY. That split-brain is
    # exactly the bug this guard retires: OPENAI_* auth env provisioned,
    # Claude runner launched, no error anywhere.
    from ..config._harness_types import ensure_harness_matches_claude_launch

    ensure_harness_matches_claude_launch(
        config,
        launching=(
            "the interactive claude TUI"
            if tui
            else f"runner module {RUNNER_MODULE!r}"
        ),
    )

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

    # Overlay precondition — idempotent, fail-loud, form-agnostic.
    # Apptainer CREATES `<overlay>/upper` + `<overlay>/work`, but it
    # lstat()s the overlay ROOT and refuses to create THAT: a missing root
    # is a hard FATAL ("failed to open overlay image ... no such file or
    # directory") that sac's lifecycle classifier could only label
    # `container_creation_unknown`. Nothing used to guarantee the root
    # existed — the fleet's overlays exist only as an INCIDENTAL side-effect
    # of deploy_to_home_overlay's `<overlay>/upper/<home>` mkdir(parents=True),
    # which is gated on a resolver blind to the `--overlay=<path>` (=-joined)
    # spelling that `sac agents create --template ...` emits. A brand-new
    # agent was therefore STILLBORN. Provision it EXPLICITLY here — the one
    # choke point every apptainer launch (SDK + TUI) passes through — reading
    # BOTH raw_args spellings. See _apptainer_overlay.
    ensure_overlay_dirs(config)

    # Relaxed-overlay double-bind fix: when a directory overlay
    # materialises the to_home tree into the overlay upper-home, THAT
    # upper-home — not the workspace-home — must back the container
    # $HOME (/home/agent). Both binds target the same path; apptainer
    # keeps only the FIRST and skips the later duplicate ("already in
    # mount point list"). Historically the workspace-home bind emitted
    # here won and silently shadowed the overlay upper, so the freshly
    # materialised to_home (.mcp.json / settings.json / credentials
    # placeholder) vanished — the agent booted on a stale/empty home
    # (cred FATAL; per-agent telegrammer MCP absent). Resolve the
    # upper-home up-front and, when it will back /home/agent, SKIP this
    # workspace-home bind so the overlay upper (bound below, after
    # raw_args) is the SINGLE authoritative home. Everything that
    # pre-creates bind-target parents or reads the agent .env then
    # targets ``home_backing`` (the resolved winner), never the
    # unmounted workspace-home. Non-overlay specs are unaffected
    # (resolver returns None → home_backing == home_host, bind emitted).
    from ._to_home_overlay import (
        resolve_container_home,
        resolve_overlay_upper_home,
    )

    _upper_home = resolve_overlay_upper_home(config)
    _overlay_backs_home = (
        _upper_home is not None
        and _upper_home.is_dir()
        and str(resolve_container_home(config)) == "/home/agent"
    )
    home_backing = (
        _upper_home if (_upper_home is not None and _overlay_backs_home) else home_host
    )
    if not _overlay_backs_home:
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
    # ``CCT_BOT_TOKEN``), never sac's critical
    # wiring. Format is plain ``KEY=VALUE`` — apptainer ``--env-file`` is not
    # a shell, so no ``export`` prefix and no quoting of values. No-op when
    # the agent ships no ``.env``.
    env_file = home_backing / ".env"
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
            f"CCT_QUOTA_CACHE_PATH={QUOTA_CACHE_CONTAINER_PATH}",
        ]

    # Host access + working directory are the SOLE responsibility of the
    # explicit ``apptainer.binds`` + ``spec.workdir`` — there is NO
    # ``access`` knob (removed 2026-06-23: it silently injected a whole-home
    # bind, a ``/work`` alias, and a ``--pwd`` rewrite, so the spec's
    # ``binds:`` list was NOT the source of truth). SSoT rule now:
    #   * ``spec.workdir`` → ONLY the ``--pwd`` the inner process opens at.
    #   * every mount      → an explicit ``apptainer.binds`` entry.
    # A "full" agent declares ``- /home/<user>:/home/<user>:rw`` (whole-home
    # reach) and sets ``workdir`` to a path under it; a capsule declares
    # ``- <writable>:/work:rw`` (+ its data binds) and ``workdir: /work``.
    # sac no longer emits ANY whole-home / ``/work`` / workdir bind itself —
    # what the spec says is exactly what mounts. (Replaces the deleted
    # ``_apptainer_access`` helpers.)
    argv += [
        # state_dir → /state/<name> (not /state) so the runner's
        # `state_dir_for(name, root=/state)` resolves to /state/<name> —
        # matching the bind target exactly. If we mounted at /state,
        # state_dir_for would re-append <name> and produce
        # /state/<name>/<name>/ on disk — the bug this comment fixes.
        "--bind",
        f"{state_dir.expanduser()}:/state/{config.name}",
        # No `--env HOME=...`: apptainer protects HOME from --env override
        # and inherits the host /etc/passwd entry, so HOME points at a real
        # writable home automatically.
        "--env",
        "SCITEX_AGENT_CONTAINER_STATE_DB=/state/state.db",
        # AND ITS SIBLING, which was missing and blinded fleet liveness.
        #
        # `beat_is_recent(name)` resolves `runtime_base_dir() / name /
        # heartbeat.json`, and `runtime_base_dir()` honours this env var
        # before falling back to `~/.scitex/agent-container/runtime`. Inside a
        # container `~` is /home/agent — ephemeral, and no agent ever writes a
        # beat there — so the fallback made the lookup answer None for EVERY
        # name. Measured 2026-08-27 from a live container: a beat file 30
        # seconds old, and beat_is_recent returning None for this agent, for a
        # real peer, and for a name that does not exist alike. Live, dead and
        # nonexistent were indistinguishable, which is
        # `sac-agent-liveness-undetectable-and-no-autoheal-20260823`.
        #
        # The HOST runtime root, not /state: /state binds THIS agent only
        # (measured: 1 entry), so it answers self-liveness and nothing else.
        # The host root carries every agent (measured: 79 dirs, 29 with a live
        # beat) and is already reachable wherever the spec declares a
        # whole-home bind.
        #
        # Setting it where that bind is ABSENT costs nothing: the reader then
        # finds no file and returns None, which is what it returns today. This
        # can make the answer better and cannot make it worse.
        "--env",
        f"SCITEX_AGENT_CONTAINER_RUNTIME_DIR={runtime_base_dir()}",
        "--pwd",
        str(Path(config.workdir).expanduser()),
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
    # 2026-08-09 gh-hosts.yml incident: a spec bind reached the argv with NO
    # check, so a credential dir that EXISTED but was EMPTY mounted fine and
    # delivered nothing — 12 agents spent hours believing they had no GitHub
    # token. spec_binds_checked is the gate: a credential bind that cannot
    # deliver REFUSES the start, any other absent source logs ERROR.
    from ._apptainer_bind_guard import spec_binds_checked
    from ._p3a_default_binds import apply_default_binds

    for bs in apply_default_binds(spec_binds_checked(config)):
        if ":" in bs:
            _, _, rest = bs.partition(":")
            dst = rest.split(":", 1)[0]
            if dst.startswith("/home/agent/"):
                rel = dst[len("/home/agent/") :]
                (home_backing / rel).mkdir(parents=True, exist_ok=True)
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
        # Writable overlay (spec.apptainer.overlay) — path resolution,
        # declarative sized-image auto-create and the ``--overlay`` flag
        # itself now live in _apptainer_overlay, alongside the directory-
        # overlay provisioning already run above, so every overlay concern
        # sits in ONE module. Mirrors the tmpfs_workdir_flags /
        # nested_build_flags / auth_argv extraction pattern below. No-op
        # for raw_args-declared overlays (passed through verbatim).
        argv += overlay_flags(config)

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

    # Nested apptainer build/pull (spec.apptainer.nested_build) — lets the
    # SOLVER reproduce a capsule's pinned env from inside its own SAC
    # container (pull a published docker:// image, or build a
    # Dockerfile-derived def whose %post runs as root), then exec it.
    # Emits /dev/fuse + the /etc/subuid+subgid masks + APPTAINER_TMPDIR/
    # CACHEDIR. No-op when off; adds NO host-FS bind so it composes with
    # access: capsule. See _apptainer_nested (verified in sac-scitex.sif).
    from ._apptainer_nested import nested_build_flags

    argv += nested_build_flags(config, state_dir)

    # Anthropic-auth argv — emits the backend wiring (env + creds
    # bind). Branches internally on whether spec.claude.provider is
    # active: provider → API-key backend (ANTHROPIC_BASE_URL +
    # SAC_ANTHROPIC_API_KEY + clean CLAUDE_CONFIG_DIR, OAuth bind
    # skipped); otherwise → the OAuth path (forward host auth env +
    # bind the resolved .credentials.json). Extracted to
    # _apptainer_auth so this runtime file stays under the line cap.
    from ._apptainer_auth import auth_argv

    argv += auth_argv(config, state_dir)

    # Agent env = the FLEET-DEFAULT layer merged UNDER spec.env (spec.env
    # WINS). See _fleet_env for the precedence rule and why it never raises.
    from ._fleet_env import effective_env

    for key, val in effective_env(config).items():
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
    _ap_raw = getattr(config, "apptainer", None)
    spec_raw_args = [str(a) for a in (getattr(_ap_raw, "raw_args", None) or [])]
    argv += spec_raw_args

    # Every flag contributor has now run. The ordering-sensitive tail
    # passes — duplicate-``--env`` collapse, the banned-port refusal, the
    # overlay-upper-home bind, the secret lift, the last-wins credentials
    # bind and the malformed-flag guard — live in _apptainer_argv_finalize,
    # which documents why each one has to see the COMPLETE flag region.
    # resolve_overlay_upper_home was already resolved up-front; reuse it.
    from ._apptainer_argv_finalize import finalize_flag_argv

    argv = finalize_flag_argv(
        argv,
        config,
        state_dir=state_dir,
        home_host=home_host,
        upper_home=_upper_home,
        spec_raw_args=spec_raw_args,
    )

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
    tui_settings: str | None = None
    if tui:
        ch = resolve_container_home(config).rstrip("/")
        has_mcp = (home_host / ".mcp.json").is_file() or (
            _upper_home is not None and (_upper_home / ".mcp.json").is_file()
        )
        if has_mcp:
            tui_mcp_config = f"{ch}/.mcp.json"
        # TUI settings/hooks: the interactive ``claude`` reads hooks/settings
        # from ``$HOME/.claude/settings.json`` at USER scope (and from
        # ``<cwd>/.claude/settings{,.local}.json`` at PROJECT scope). It does
        # NOT read a ``$HOME/.claude/settings.local.json`` — there is no
        # ``.local.json`` at user scope. So the skip-permissions key + SAC
        # channel hooks + the ``_shared`` baseline honest-grounding Stop gate /
        # lint PostToolUse are materialised into ``$HOME/.claude/settings.json``
        # (``setup_settings_json(..., filename="settings.json")`` in the
        # runtime's materialize_workspace) and picked up by user-scope
        # discovery — no flag required. We ALSO point ``--settings`` at the same
        # file as belt-and-suspenders / SDK parity; note the flag is a no-op for
        # the interactive TUI (it replaces discovery and only applies to
        # print/SDK mode — see ``_sdk_common._container_settings_path`` and
        # skill ``25_claude-setup-delivery``), so user-scope ``settings.json``
        # is what actually carries the suite. Gated on the host-backing file so
        # a spec without one doesn't aim ``--settings`` at a missing path; the
        # legacy ``settings.local.json`` is accepted as a fallback for baselines
        # not yet renamed (setup_settings_json folds it forward into
        # settings.json at materialize time).
        for _rel in (".claude/settings.json", ".claude/settings.local.json"):
            if (home_host / _rel).is_file() or (
                _upper_home is not None and (_upper_home / _rel).is_file()
            ):
                tui_settings = f"{ch}/{_rel}"
                break
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
            tui_settings=tui_settings,
        )
    else:
        kind = getattr(config, "kind", "Agent")
        if kind == "AgentProxy":
            module = RUNNER_MODULE_PROXY
        else:
            # Claude runner unconditionally: the top-of-function harness
            # guard already refused any non-Anthropic spec (v4 step 2 —
            # the old ``getattr(config, "provider", None)`` read here
            # was DEAD, so ``RUNNER_MODULE_OPENAI`` was never actually
            # dispatched). ``RUNNER_MODULE`` is now DERIVED from the
            # harness registry's SDK entry (v4 step 4); dispatching
            # OTHER entries' modules here is migration step 7.
            module = RUNNER_MODULE
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
    # Jailed-capsule mount boundary (non-bypassable, fail-loud): forces
    # --containall + rejects shared-FS binds/--pwd. See _apptainer_jail.
    from ._apptainer_jail import enforce_jail

    enforce_jail(config, argv)
    return argv


__all__ = [
    "QUOTA_CACHE_CONTAINER_PATH",
    "QUOTA_CACHE_HOST_PATH_DEFAULT",
    "QUOTA_CACHE_HOST_PATH_ENV",
    "RUNNER_MODULE",
    "_resolve_quota_cache_host_path",
    "build_run_argv",
]
