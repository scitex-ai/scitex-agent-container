"""Dataclass definitions for agent configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict


@dataclass
class ContainerSpec:
    runtime: str = "none"  # none | docker | apptainer
    image: str = "scitex-agent-container:latest"
    volumes: list[str] = field(default_factory=list)
    network: str = "host"
    # Opt-in auto-mount of the host's ``~/.claude`` directory at
    # ``/home/agent/.claude:ro`` inside the container. Default False: the
    # container is the isolation boundary, and auto-mounting leaks host
    # identity/skills/MCP/memory into every agent — surprising default.
    # Set ``mount_host_claude: true`` in the YAML only when the agent
    # actually needs host-agent identity/memory/skills from ``~/.claude``.
    mount_host_claude: bool = False


@dataclass
class ClaudeSpec:
    # v3-realign: model lives under spec.claude.model (promoted from
    # top-level spec.model — §3). Empty = runtime default.
    model: str = ""
    channels: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # v3 escape hatch (§1 invariant): splat ``**raw_options`` into
    # ``ClaudeAgentOptions`` so power users can reach any SDK option
    # sac doesn't model. Merged on top of curated keys; raw_options wins.
    raw_options: dict = field(default_factory=dict)
    # Session restart strategy. One of:
    #   continue     try --continue; fall back to a fresh launch if no
    #                prior session jsonl exists (default; safe).
    #   new-session  never pass --continue — always start fresh.
    #   resume       pass --resume <resume_id> (explicit session ID).
    # Legacy aliases accepted at load time: `continue-or-new` -> `continue`,
    # `new` -> `new-session`.
    session: str = "continue"
    # Only resume if the most recent session jsonl is newer than this many minutes.
    # None = no age check (always resume if session exists).
    continue_max_age_minutes: int | None = None
    # Explicit session ID to pass to --resume. Only used when session="resume".
    resume_id: str = ""
    auto_accept: bool = True
    # Saved-account name (from ``sac account list``) whose credential
    # snapshot this agent runs on. ``""`` = the host's live
    # ``~/.claude/.credentials.json`` (current default).
    #
    # When set, the runtime COPIES that account's ``.credentials.json``
    # into the agent's own state dir at start (frozen boot-copy, not a
    # live bind), so two agents pinned to two accounts never fight one
    # mount. The copy is bound RW so in-container ~1h token refresh keeps
    # working on the agent's private copy.
    #
    # Takes effect on next start/restart — a host ``/login`` does NOT
    # move a pinned agent (that is the point of pinning), and changing
    # this field requires ``sac agent restart`` to re-copy the snapshot.
    account: str = ""


@dataclass
class HealthSpec:
    enabled: bool = False
    interval: int = 30
    timeout: int = 5
    method: str = "multiplexer-alive"


# Parsed for backward compat but not interpreted by runtime.
# Watchdog lifecycle is managed externally via hooks.
@dataclass
class WatchdogSpec:
    enabled: bool = False
    interval: float = 1.5
    resp_y_n: str = "1"
    resp_y_y_n: str = "2"
    resp_waiting: str = "/speak-and-call"


# F-CS3 — autonomous drive-until-done.
#
# claude-session runners do ONE turn and idle by default; multi-turn
# tasks have to wrap externally with a2a peer post-turn loops, and
# every project ends up rewriting that scaffolding. The autonomous
# block lets the runner natively:
#
#   1. Watch each assistant turn for a text match (``drive_until``);
#      hitting it exits the runner with code 0.
#   2. After ``idle_kick_after_s`` of no tool activity AND no match,
#      post ``kick_text`` so the conversation keeps moving.
#   3. Cap at ``max_turns`` to prevent runaway loops.
#
# Phase 1 (this dataclass + parser + validator) lands the schema so
# yamls can author the contract today; the runner-side enforcement
# (consume these fields in _runners.claude_session) lands in phase 2.
# An ``enabled`` row authored under the schema before phase 2 ships
# is harmless — the runner just ignores it for now.
# F-CS18 — apptainer-specific extension hook.
#
# Apptainer reads OCI images natively (`apptainer build sif docker://...`),
# so for the no-extras case spec.image alone is enough — sac just
# `apptainer build`s the SIF and runs it. For HPC-specific layering
# (extra pip packages, system libs, env vars), the operator can either:
#
#   * declare `spec.apptainer.post` — sac synthesises a `.def` with
#     `Bootstrap: docker` + `%post` + `%environment` and builds from it.
#   * declare `spec.apptainer.def_file` — sac runs `apptainer build`
#     against the operator's hand-written `.def` (full control).
#
# All fields are optional; an `apptainer:` block with no fields set is
# equivalent to none at all.
@dataclass
class ApptainerSpec:
    """Apptainer-specific image-build extensions (F-CS18)."""

    # v3-realign: apptainer-engine-scoped knobs promoted from top-level.
    image: str = ""
    """SIF path or docker:// URL — promoted from top-level spec.image (§3).
    Empty = fall back to the default sac-scitex SIF."""

    binds: list[str] = field(default_factory=list)
    """Bind mounts as ``host:container[:mode]`` strings — promoted from
    top-level spec.mounts (§3)."""

    env: dict[str, str] = field(default_factory=dict)
    """Env vars exported into the container — promoted from top-level
    spec.env (§3)."""

    raw_args: list[str] = field(default_factory=list)
    """v3 escape hatch (§1 invariant): appended verbatim to the
    ``apptainer exec`` argv after all curated args. Lets operators bolt
    on flags sac doesn't model."""

    container_workdir: str = "/work"
    """Path inside the container where ``spec.workdir`` gets bind-mounted
    (and where the runner's ``--pwd`` lands). Default ``/work``.
    Override when the SIF expects a different mount point (e.g. a
    pre-baked ``WORKDIR`` in the .def file)."""

    post: str = ""
    """Shell snippet run inside the SIF build (apptainer's `%post`).
    Lines are concatenated verbatim. Empty = no extension."""

    environment: dict = field(default_factory=dict)
    """Env vars baked into the SIF (apptainer's `%environment`). Same
    shape as ``spec.env`` — KEY: VALUE pairs."""

    def_file: str = ""
    """Path to a hand-authored ``.def`` file (apptainer's native
    build language). Mutually exclusive with `post`/`environment`:
    when set, sac uses this file verbatim and ignores `post`."""

    nv: bool = False
    """Forward host NVIDIA driver/libs into the container (apptainer's
    ``--nv``). Required for CUDA workloads on GPU nodes; harmless on
    CPU-only hosts but only set when needed."""

    rocm: bool = False
    """Forward host AMD ROCm libs (apptainer's ``--rocm``). Mutually
    exclusive with ``nv`` in practice (no host has both)."""

    overlay: str = ""
    """Writable apptainer overlay image (``--overlay <file>``). Empty =
    no overlay (tmpfs writable layer). Non-absolute paths resolve
    against ``spec.workdir``. See ``docs/isolation.md`` §7."""

    overlay_size: str = ""
    """When set together with ``overlay``, sac auto-creates the overlay
    image with the given size if it doesn't exist before launching.
    Accepts apptainer-style sizes with units M/MB/G/GB only (e.g.
    ``"5G"``, ``"500M"``, ``"1024MB"``). K/KB are explicitly rejected —
    apptainer's ``overlay create --size`` takes integer MB so sub-MB
    granularity makes no sense. Empty = no auto-create (default;
    missing overlay raises FileNotFoundError at launch with a clear
    message). See ``docs/isolation.md`` §7."""

    overlay_create_if_missing: bool = True
    """When True (default) AND ``overlay_size`` is set AND the overlay
    path does not exist, sac runs ``apptainer overlay create --size
    <MB> <path>`` before launching. When False, sac never creates
    overlays even if size is given (operator must pre-create — sac
    raises FileNotFoundError instead). See ``docs/isolation.md`` §7."""

    relaxed: bool = False
    """Opt out of sac's hardened defaults (auto-prepended
    ``--containall``/``--cleanenv``/``--writable-tmpfs``/``--home``).
    See ``docs/isolation.md``."""

    fakeroot: bool = False
    """Apptainer ``--fakeroot`` — uid 0 inside via user-namespace
    remapping; operator uid on host. Pairs with the D5 preflight's
    ``/proc/self/uid_map`` detection (see ``docs/isolation.md``)."""


@dataclass
class AutonomousSpec:
    enabled: bool = False
    drive_until: str = "DONE"
    max_turns: int = 50
    idle_kick_after_s: int = 120
    kick_text: str = "Continue. Print DONE when finished."


@dataclass
class RestartSpec:
    policy: str = "never"  # never | on-failure | always
    max_retries: int = 3
    backoff_initial: int = 30
    backoff_max: int = 300
    backoff_multiplier: int = 2


# Inbound A2A surface for an agent. The SDK runner launches a sidecar
# HTTP server exposing ``/v1/turn`` + ``/.well-known/agent.json``.
# ``port`` semantics:
#   * ``"auto"`` (default) — sac allocates via port_allocator at start.
#     Clients should reach the agent through ``sac listen`` (one host
#     port, name-in-path); per-agent ports are internal IPC.
#   * ``int``   — operator-pinned; collisions raise at start time.
#   * ``None``  — sidecar disabled (no inbound HTTP).
@dataclass
class A2ASpec:
    host: str = "127.0.0.1"
    port: int | str | None = "auto"

    @property
    def is_auto(self) -> bool:
        return self.port == "auto"

    @property
    def is_disabled(self) -> bool:
        return self.port is None


@dataclass
class ContextManagementConfig:
    """Context-lifecycle policy for an agent.

    Defaults mirror ``strategy="noop"`` so absence of the ``context_management``
    block preserves existing behavior (sensor disabled).
    """

    trigger_at_percent: float = 70.0
    strategy: str = "noop"  # "compact" | "restart" | "noop"
    warn_before_n_checks: int = 0
    check_interval_seconds: int = 300
    state_file: str = "~/.scitex/agent-container/state/<agent>.json"

    @property
    def enabled(self) -> bool:
        return self.strategy != "noop"


@dataclass
class SkillsSpec:
    required: list[str] = field(default_factory=list)  # Auto-loaded at startup
    available: list[str] = field(default_factory=list)  # Available but not auto-loaded
    # How sac materializes the skill list into the agent's CLAUDE.md:
    #   "at-import" — resolve each name to file paths and emit `@<path>` lines
    #                 so Claude Code inlines the content at session start
    #                 (default — eager loading per Anthropic @-import).
    #   "block"     — emit a ```skills <name>``` block (legacy lazy form).
    injection_mode: str = "at-import"
    # Strategies used to resolve a skill name → file paths in at-import mode.
    # Each entry runs independently; results are unioned + deduped.
    #   "skill-id" — Anthropic-canonical: walk skill roots, for each
    #                ``<dir>/SKILL.md`` resolve identity as
    #                ``frontmatter.name`` (if set) ELSE ``<dir>.name``.
    #                Match if identity equals the requested value.
    #                See https://docs.claude.com/en/docs/claude-code/skills.
    #   "tag"      — files where frontmatter ``tags:`` contains the value
    #                (orchestration extension; not in Anthropic spec but
    #                used by ywatanabe ``tags-expand`` pattern).
    #   "filename" — files whose basename (without ``.md``) matches
    #                (opt-in; broader than ``skill-id``, can over-match).
    match_by: list[str] = field(default_factory=lambda: ["skill-id", "tag"])
    # Comparison style for ``match_by`` strategies.
    #   "exact"   — value == candidate (default)
    #   "partial" — value substring of candidate (case-sensitive)
    match_style: str = "exact"


@dataclass
class HostsSpec:
    """Where an agent should run, in either singleton or multi-instance form.

    Mutually exclusive — exactly one of ``host`` or ``hosts`` may be set:

    * ``host`` (singular) — exactly one instance runs:
        - empty / absent: local singleton (runs wherever sac is invoked)
        - string: pinned to that host
        - list: priority order; first available host wins (fallback chain)
    * ``hosts`` (plural) — multiple instances run, one per host:
        - "all": one per fleet host (replaces the old per-host mode)
        - list of host names: one per listed host (subset)

    Validator (in ``_validation.py``) enforces mutual exclusion + types.
    Loader composes effective ids: ``hosts`` triggers the
    ``<name>-<HOST>`` suffix; ``host`` keeps the bare name.
    """

    host: str | list[str] = ""
    hosts: str | list[str] = field(default_factory=list)


@dataclass
class SchedulingSpec:
    """Fleet-wide scheduling policy for an agent (shared-host layout).

    ``mode`` controls effective-id composition and launch-skip behavior:
      * ``per-host`` (default): agent is started on every host that runs
        ``sac agent start <name>``; the effective id is ``<metadata.name>-<HOST>``
        unless the name already ends with ``-<HOST>``.
      * ``singleton``: exactly one instance fleet-wide. The effective id
        stays as the bare ``<metadata.name>``. Only launched on
        ``preferred-host``; on other hosts the launch is a no-op.

    ``fallback-hosts`` is recorded for observability but not acted on
    automatically — manual failover today.
    """

    mode: str = "per-host"
    preferred_host: str = ""
    fallback_hosts: list[str] = field(default_factory=list)


@dataclass
class ListenPort:
    """Declaration of a port/socket an external tool binds on behalf of an agent.

    The container NEVER binds these — it just validates the shape and
    echoes them in ``status --json`` so orchestrators can see what
    sidecars are expected to exist. ``owner`` is free-form (e.g.
    ``"orochi"``) to identify the plugin that actually listens.
    """

    port: int = 0
    proto: str = "tcp"  # tcp | udp | unix
    path: str = ""  # unix-socket path (when proto == "unix")
    name: str = ""
    owner: str = ""


@dataclass
class HookSpec:
    """All hook points supported by the container.

    Each entry is a list of opaque commands — shell strings or http(s)
    URLs. The container executes them fire-and-forget; errors are
    logged but never raised to the caller. Absent keys default to
    empty lists (feature disabled).
    """

    pre_start: list[str] = field(default_factory=list)
    post_start: list[str] = field(default_factory=list)
    pre_stop: list[str] = field(default_factory=list)
    post_stop: list[str] = field(default_factory=list)
    on_compact: list[str] = field(default_factory=list)
    on_restart: list[str] = field(default_factory=list)
    on_diff: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "pre_start": len(self.pre_start),
            "post_start": len(self.post_start),
            "pre_stop": len(self.pre_stop),
            "post_stop": len(self.post_stop),
            "on_compact": len(self.on_compact),
            "on_restart": len(self.on_restart),
            "on_diff": len(self.on_diff),
        }


@dataclass
class StartupCommand:
    delay: int = 0  # seconds after startup
    command: str = ""


@dataclass
class AgentConfig:
    """Parsed agent configuration from a YAML definition file."""

    name: str
    runtime: str = "apptainer"
    # Top-level container image. Empty = use the default sac-scitex SIF.
    # (`spec.dockerfile` was dropped 2026-05-13 with the docker ripout.)
    image: str = ""
    model: str = "sonnet"
    # Empty default means "use the per-agent workspace under sac's
    # user-state root" — resolved by `expanded_workdir` below to
    # `~/.scitex/agent-container/runtime/agents/<name>/`. Setting
    # `spec.workdir` explicitly overrides that.
    workdir: str = ""
    python_venv: str = ""  # resolved venv path (post _resolve_python_venv)
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(
        default_factory=list
    )  # .env file paths (workspace-relative ok)
    screen_name: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    container: ContainerSpec = field(default_factory=ContainerSpec)
    claude: ClaudeSpec = field(default_factory=ClaudeSpec)
    health: HealthSpec = field(default_factory=HealthSpec)
    watchdog: WatchdogSpec = field(default_factory=WatchdogSpec)
    restart: RestartSpec = field(default_factory=RestartSpec)
    autonomous: AutonomousSpec = field(default_factory=AutonomousSpec)
    apptainer: ApptainerSpec = field(default_factory=ApptainerSpec)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    listen: list[ListenPort] = field(default_factory=list)
    extensions: Dict[str, Any] = field(default_factory=dict)
    # ``RemoteSpec`` deleted in WI-6 (handoff §6, 2026-05-20). spec.host
    # is now the only mechanism for cross-host placement; SSH dispatch
    # via the old ``spec.remote.{host,hops,user,key,...}`` block has
    # been retired together with ``runtimes/ssh_remote.py``.
    skills: SkillsSpec = field(default_factory=SkillsSpec)
    context_management: ContextManagementConfig = field(
        default_factory=ContextManagementConfig
    )
    # startup_commands run as SHELL commands inside the container before
    # the claude SDK starts. startup_prompts (separate field) carries
    # the claude mission. No fallback between the two.
    startup_commands: list[StartupCommand] = field(default_factory=list)
    # v3-realign: ``startup_prompts`` is separate from ``startup_commands``
    # (§3). startup_commands are SHELL commands run BEFORE claude starts;
    # startup_prompts are TEXT fed to claude as the first user message(s).
    startup_prompts: list[str] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    multiplexer: str = "tmux"  # "tmux" (default) or "screen"
    hosts_spec: HostsSpec = field(default_factory=HostsSpec)
    scheduling: SchedulingSpec = field(default_factory=SchedulingSpec)
    config_path: str = ""
    # Declarative bind-mounts: list of {"src": <host>, "dst": <ctr>, "mode": "rw"|"ro"}.
    mounts: list[dict] = field(default_factory=list)
    # Container user. "" → image's USER (typically `agent`); "host" → host
    # operator's UID:GID; "<uid>:<gid>" → explicit numeric. Pair with
    # spec.mounts + spec.env.HOME for host-shaped paths + ownership.
    user: str = ""
    # Inbound A2A endpoint (HTTP /v1/turn + AgentCard).
    a2a: A2ASpec = field(default_factory=A2ASpec)
    # v3 ``kind`` discriminator: "Agent" (SDK runner) or "AgentProxy"
    # (HTTP forwarder — see :class:`ProxySpec`). Validator rejects any
    # other value. Loader populates from raw["kind"].
    kind: str = "Agent"
    # ProxySpec is only meaningful when ``kind == AgentProxy``.
    # Stored as ``Any`` here so this module stays import-cycle-free with
    # ``_proxy_types``; the actual type is ``ProxySpec | None``.
    proxy: Any = None
    # ADR-0006: spec.to_home — directory whose contents are mirrored
    # into the agent's container ``$HOME`` (= ``runtime/<name>/home/``
    # on the host) on every start. Every path under ``to_home/``
    # lands at the same relative path inside ``$HOME``.
    # Default: ``./to_home`` next to ``spec.yaml`` (auto-discovered
    # when this field is empty).
    to_home: str = "./to_home"

    def __post_init__(self) -> None:
        if not self.screen_name:
            self.screen_name = f"cld-{self.name}"

    @property
    def expanded_workdir(self) -> str:
        if self.workdir:
            return str(Path(self.workdir).expanduser())
        # Per-agent default workspace — lives under sac's user-state
        # tree so multiple agents stay isolated, mounts at /work
        # inside the container, persists across restarts. Created
        # lazily by the runtime adapter (apptainer bind target dir
        # auto-created by apptainer if missing).
        return str(
            Path.home()
            / ".scitex"
            / "agent-container"
            / "runtime"
            / "agents"
            / self.name
        )
