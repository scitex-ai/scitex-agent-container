"""Dataclass definitions for agent configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

# Phase-3 ACL dataclasses (kept in a sibling module under the per-file
# line cap; re-exported here for the :class:`AgentConfig` field defaults).
from ._acl_types import CommsSpec, LineageSpec  # noqa: E402,F401

# ApptainerSpec extracted to a sibling module (per-file line cap);
# re-exported here so ``from ...config._types import ApptainerSpec`` resolves.
from ._apptainer_spec import ApptainerSpec  # noqa: E402,F401
from ._harness_types import DEFAULT_AGENT_HARNESS, AgentHarness
from ._provider_types import ProviderSpec


@dataclass
class ContainerSpec:
    runtime: str = "none"  # none | apptainer (see VALID_CONTAINER_RUNTIMES)
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
    # Session continuity strategy. One of:
    #   fresh        never pass ``-c``/``--continue`` — every launch is an
    #                independent session (DEFAULT). This is the right
    #                behaviour for experiment trials, where each capsule
    #                run must be hermetic and must not inherit the prior
    #                conversation.
    #   continue     resume the latest session for this cwd (TUI: ``claude
    #                -c``; SDK: auto-resume the persisted session_id). The
    #                right behaviour for LONG-LIVED coordinator agents
    #                (lead/head/worker/telegrammer/project-maintainer/...)
    #                that must keep their working memory across restarts.
    #   resume       pass --resume <resume_id> (explicit session ID).
    # Aliases accepted at load time (parse_claude):
    #   new-session, new  -> fresh   (back-compat: the pre-2026-06 names)
    #   continue-or-new   -> continue
    # NOTE the default flipped from ``continue`` to ``fresh`` (2026-06-22,
    # "fresh by default, opt-in continue"): a spec that omits ``session``
    # now starts fresh. Coordinator/long-lived roles are mapped back to
    # ``continue`` BY ROLE so omitting the field keeps those agents
    # continuous WITHOUT editing every deployed spec, while experiment
    # capsules (non-coordinator roles) get fresh. See
    # ``config/_session_continuity.py`` (``role_wants_continuity`` /
    # ``default_session_for_role`` / ``_CONTINUITY_ROLES``) for the
    # role-default, applied in ``config/_loaders.py`` after ``parse_claude``;
    # and ``runtimes/_apptainer_inner_argv._tui_runner_argv`` for where the
    # resolved mode becomes ``claude -c`` (continue) or no flag (fresh).
    session: str = "fresh"
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
    # Explicit credentials-file designation (host path to a
    # ``.credentials.json``). When set, the runtime BIND-MOUNTS that
    # exact file WRITABLE at the in-container
    # ``$HOME/.claude/.credentials.json`` — single source of truth, no
    # copy. The in-container ``claude`` reads/refreshes that file
    # directly, so an OAuth refresh persists straight back to the
    # designated file and never desyncs from / corrupts a shared
    # ``~/.claude/.credentials.json``. Designating different files for
    # different agents is how multiple accounts run side by side
    # (rotate by pointing agents at different credential files).
    #
    # Precedence: when set, this file-mount REPLACES the account /
    # host dir-bind auth path (no ``CLAUDE_CONFIG_DIR`` redirect) — the
    # designated file IS the agent's credentials. Mutually exclusive
    # with ``provider`` (an API-key backend needs no OAuth file).
    #
    # Caveat (documented in ``_apptainer_auth``): a single-file bind is
    # on the file's inode; a host-side atomic tmp+rename on the source
    # orphans the bind. In-container ``claude`` refresh that writes
    # in-place is safe; designate a path NOT concurrently atomic-renamed
    # by host-side ``sac accounts``/watch-live tooling.
    credentials_file: str = ""
    # Account POOL: a list of host paths to ``.credentials.json`` files
    # (one per saved account). When non-empty, the start pre-flight picks
    # ONE of them QUOTA-CONDITIONAL — token-fresh, avoiding 5h-blocked
    # and 7d-near-capped accounts, load-balanced across the fleet per
    # agent name (see ``_creds.pick_healthy_account`` +
    # ``_lifecycle._start_preflight._rotate_to_healthy_account``) — and
    # binds the PICKED file exactly as if it had been named in the singular
    # ``credentials_file`` field. Each entry's ACCOUNT SLUG is its parent
    # directory name (the fleet layout is
    # ``~/.scitex/agent-container/accounts/<slug>/.credentials.json``), and
    # that slug is the account name the quota-aware picker keys off. The
    # singular ``credentials_file`` remains supported and is treated as a
    # 1-element pool (pick returns it) for back-compat. Fail-loud: when NO
    # listed entry has a usable (non-expired) snapshot the start aborts with
    # ``_creds.NoHealthyAccountError``. Mutually exclusive with ``provider``
    # (an API-key backend needs no OAuth).
    credentials_files: list[str] = field(default_factory=list)
    # Vendor-agnostic backend override (see :class:`ProviderSpec`).
    # When set, the SDK session runs against an Anthropic-SDK-compatible
    # backend (DeepSeek, a gateway, ...) on an API key instead of
    # Anthropic OAuth. ``None`` = default Anthropic backend. Mutually
    # exclusive with ``account`` (an API-key backend needs no OAuth).
    provider: ProviderSpec | None = None


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
    # Inode-hygiene opt-in (sac-runtime-state-hygiene incident): when
    # True AND ``policy == "never"``, a CLEAN terminal ``sac agents stop``
    # prunes this agent's runtime dir + overlay so ephemeral capsules
    # don't accumulate one-per-run forever. EXPLICIT opt-in is required
    # (default False) — ``policy`` itself DEFAULTS to "never", so a
    # persistent coordinator that merely omits a ``restart:`` block must
    # NOT be pruned; only specs that deliberately set this flag are.
    prune_on_stop: bool = False


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
    ``"fleet-hub"``) to identify the plugin that actually listens.
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
    # Launch-mode selector. Default ``"tui"`` (interactive in-apptainer
    # TUI — operator directive 2026-06-15). ``"claude-agent-sdk"`` =
    # headless SDK runner; legacy ``"apptainer"`` maps to the SDK runner.
    runtime: str = "tui"
    # HARNESS: which agent SDK runs the session ("anthropic" = the
    # claude-agent-sdk | "openai"). Carries ``spec.harness`` or its
    # DEPRECATED alias ``spec.provider`` — see ``config._harness_types``.
    harness: AgentHarness = DEFAULT_AGENT_HARNESS
    # Provenance, NOT a spec field: reached only through the alias.
    harness_key_is_legacy: bool = False
    # spec.access REMOVED 2026-06-23 — host access + cwd are the single
    # source of truth in apptainer.binds + spec.workdir. There is no posture
    # enum: a "full" agent declares ``- /home/<user>:/home/<user>:rw``; a
    # capsule declares only the binds it wants. build_run_argv emits exactly
    # the spec's binds + ``--pwd <workdir>`` and nothing implicit. A spec
    # still carrying ``access:`` is rejected loud (config/_validation.py).
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
    # Opt-OUT switches (No-Surprise: see what an agent gets via `sac agents
    # explain`, then turn specific items off). Each entry is a substring matched
    # against a materialized hook COMMAND (e.g. "report_to_lead_on_stop" drops
    # that Stop hook) or a skill @-import path. Empty = nothing excluded.
    exclude_hooks: list[str] = field(default_factory=list)
    exclude_skills: list[str] = field(default_factory=list)
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
    # Phase-3 capsule-isolation: per-spec ACL policy + spawn gating.
    # Defaults preserve pre-Phase-3 behaviour (everything allow / true).
    comms: CommsSpec = field(default_factory=CommsSpec)
    lineage: LineageSpec = field(default_factory=LineageSpec)
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
    # spec.to_home_layers — which to_home CASCADE layers this agent inherits,
    # named explicitly so the spec states what will be merged into it instead
    # of leaving it to be discovered on disk. Valid names are the cascade's own
    # (``user-shared``, ``project-shared``, ``per-agent``); order is fixed by
    # precedence, not by how they are listed here.
    #
    # ``None`` (key absent) means "inherit whatever is on disk" — today's
    # implicit behaviour, kept so this field can land without changing a single
    # existing agent. It is NOT the end state: measured 2026-08-09, ALL 102
    # registered specs are in exactly this position, so refusing an undeclared
    # spec today would strip every hook from every agent at once. The migration
    # declares them first, and enforcement comes after that. Until then an
    # absent value is warned about, never refused.
    to_home_layers: "list[str] | None" = None

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
