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
    channels: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    # Session restart strategy. One of:
    #   continue-or-new  try --continue, fall back to a fresh launch if no prior session (default)
    #   continue         always pass --continue (fails if no prior session exists)
    #   new              never pass --continue
    #   resume           pass --resume <resume_id> (explicit session ID)
    session: str = "continue-or-new"
    # Only resume if the most recent session jsonl is newer than this many minutes.
    # None = no age check (always resume if session exists).
    continue_max_age_minutes: int | None = None
    # Explicit session ID to pass to --resume. Only used when session="resume".
    resume_id: str = ""
    auto_accept: bool = True


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


@dataclass
class RestartSpec:
    policy: str = "never"  # never | on-failure | always
    max_retries: int = 3
    backoff_initial: int = 30
    backoff_max: int = 300
    backoff_multiplier: int = 2


# Parsed for backward compat but not interpreted by runtime.
# Telegram setup is managed externally via hooks.
@dataclass
class TelegramSpec:
    bot_token_env: str = "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
    allowed_users: list[str] = field(default_factory=list)
    auto_connect: bool = True
    greeting: str = ""


@dataclass
class SlurmHooks:
    """Plugin hook paths for the SLURM runtime.

    Each field is a path to a shell fragment that is *sourced* (not exec'd)
    by the sbatch wrapper. Hooks can export env vars that persist into the
    agent process — this is exactly what e.g. Lmod module loads need.

    Hook env vars (set by the wrapper before sourcing):
        SAC_AGENT_ID, SAC_JOB_ID, SAC_WORKDIR, SAC_LOG_FILE, SAC_PHASE.

    sac ships no default hooks; external orchestrators (orochi, etc.)
    provide their own scripts and reference them from agent YAML.
    """

    pre_submit: str = ""
    pre_agent: str = ""
    walltime_signal: str = ""
    post_agent: str = ""
    attach: str = ""


@dataclass
class OrochiSpec:
    enabled: bool = False
    hosts: list[str] = field(default_factory=list)
    port: int = 8559
    token_env: str = "SCITEX_OROCHI_TOKEN"
    channels: list[str] = field(default_factory=list)
    heartbeat_interval: int = 60


@dataclass
class SlurmHeartbeatSpec:
    """Compute-node heartbeat daemon for the SLURM runtime.

    On HPC clusters the host-level heartbeat pusher (systemd user timer,
    launchd plist) runs on the *login node* and cannot see tmux sessions
    living on the compute node the sbatch job landed on. Without a
    compute-node-local pusher, the hub marks the agent dead five minutes
    after the job starts (symptom: ``head-spartan`` alive in squeue but
    red on the dashboard — lead msg#15654).

    Fix: the sbatch wrapper spawns a lightweight background loop that
    invokes ``command`` every ``interval_s`` seconds on the compute node
    itself. When ``command`` is empty the loop is skipped (opt-in).

    The command is expected to be a self-contained shell invocation of a
    heartbeat pusher (e.g. ``python3 .../agent_meta.py --push``). The
    wrapper exports ``SCITEX_OROCHI_AGENT`` / ``SCITEX_OROCHI_HOSTNAME``
    via the ``pre_agent`` hook so the pushed payload registers with the
    correct fleet identity.

    Fields:
        command:   Shell command line to run each tick. Empty disables.
        interval_s: Seconds between ticks. 30 matches the login-node
                   systemd timer cadence.
        log_file:  Absolute path (with ``~`` expansion) for stderr/stdout
                   capture. Defaults to ``<logs_dir>/<jobid>.heartbeat.log``
                   when empty.
    """

    command: str = ""
    interval_s: int = 30
    log_file: str = ""


@dataclass
class SlurmSpec:
    """SLURM runtime configuration parsed from agent YAML's ``spec.slurm``."""

    partition: str = ""
    time_limit: str = "1-00:00:00"
    cpus_per_task: int = 1
    mem: str = "4G"
    nodes: int = 1
    ntasks: int = 1
    gres: str = ""
    job_name: str = ""
    signal: str = "B:USR1@3600"
    auto_resubmit: bool = True
    hold: str = "tail -f /dev/null"
    logs_dir: str = "~/slurm_logs"
    hooks: SlurmHooks = field(default_factory=SlurmHooks)
    heartbeat: SlurmHeartbeatSpec = field(default_factory=SlurmHeartbeatSpec)
    extra_directives: list[str] = field(default_factory=list)
    # ``slurm-tenant`` runtime: name of the scitex-hpc Reservation lease
    # this agent should join. Empty for the regular ``slurm`` runtime.
    # Operator must `scitex-hpc reservations book <name> ...` first.
    reservation: str = ""


@dataclass
class RemoteSpec:
    # Chain-based remote: list of SSH config aliases (new format).
    # Populated when spec.remote is a str or list[str].
    # Empty when using legacy dict format.
    hops: list = field(default_factory=list)
    host: str = ""  # SSH host (hostname or IP)
    user: str = ""  # SSH user
    key: str = ""  # Path to SSH key (optional)
    port: int = 22  # SSH port
    timeout: int = 60  # SSH command timeout in seconds
    login_shell: bool = True  # Use bash -l -c (needed for PATH on most hosts)
    no_preflight: bool = False  # Skip preflight checks (HPC with module loads)

    @property
    def is_remote(self) -> bool:
        """Return True if this agent should be deployed via SSH."""
        return bool(self.hops or self.host)


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
        ``sac start <name>``; the effective id is ``<metadata.name>-<HOST>``
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
class ReadyPattern:
    """A single regex the pane content must match for the agent to be ready."""

    regex: str = ""


@dataclass
class StartupSpec:
    """Opt-in ready-state gate for startup commands (todo#291).

    When ``ready_patterns`` is empty, legacy fire-and-hope behavior is
    preserved. Otherwise ``agent_start`` polls the tmux pane content and
    only dispatches ``commands`` once all patterns match against the tail
    of the capture AND the pane has been byte-identical for
    ``ready_idle_ticks`` consecutive polls.
    """

    ready_patterns: list[ReadyPattern] = field(default_factory=list)
    ready_idle_ticks: int = 3
    ready_poll_interval_seconds: float = 0.5
    ready_timeout_seconds: float = 60.0
    # "capture_and_fail" | "capture_and_proceed"
    on_timeout: str = "capture_and_proceed"
    commands: list[StartupCommand] = field(default_factory=list)


@dataclass
class AgentConfig:
    """Parsed agent configuration from a YAML definition file."""

    name: str
    runtime: str = "claude-code"
    model: str = "sonnet"
    workdir: str = "~/proj"
    python_venv: str = ""  # resolved venv path (post _resolve_python_venv)
    env: dict[str, str] = field(default_factory=dict)
    env_files: list[str] = field(default_factory=list)  # .env file paths (workspace-relative ok)
    screen_name: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    container: ContainerSpec = field(default_factory=ContainerSpec)
    claude: ClaudeSpec = field(default_factory=ClaudeSpec)
    health: HealthSpec = field(default_factory=HealthSpec)
    watchdog: WatchdogSpec = field(default_factory=WatchdogSpec)
    restart: RestartSpec = field(default_factory=RestartSpec)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    listen: list[ListenPort] = field(default_factory=list)
    extensions: Dict[str, Any] = field(default_factory=dict)
    telegram: TelegramSpec = field(default_factory=TelegramSpec)
    remote: RemoteSpec = field(default_factory=RemoteSpec)
    skills: SkillsSpec = field(default_factory=SkillsSpec)
    context_management: ContextManagementConfig = field(
        default_factory=ContextManagementConfig
    )
    startup_commands: list[StartupCommand] = field(default_factory=list)
    startup: "StartupSpec" = field(default_factory=lambda: StartupSpec())
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    multiplexer: str = "tmux"  # "tmux" (default) or "screen"
    hosts_spec: HostsSpec = field(default_factory=HostsSpec)
    slurm: SlurmSpec = field(default_factory=SlurmSpec)
    scheduling: SchedulingSpec = field(default_factory=SchedulingSpec)
    orochi: OrochiSpec = field(default_factory=OrochiSpec)
    config_path: str = ""

    def __post_init__(self) -> None:
        if not self.screen_name:
            self.screen_name = f"cld-{self.name}"

    @property
    def expanded_workdir(self) -> str:
        return str(Path(self.workdir).expanduser())
