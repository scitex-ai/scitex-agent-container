"""Dataclass definitions for agent configuration."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ContainerSpec:
    runtime: str = "none"  # none | docker | apptainer
    image: str = "scitex-agent-container:latest"
    volumes: list[str] = field(default_factory=list)
    network: str = "host"


@dataclass
class ClaudeSpec:
    channels: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    session: str = "new"
    auto_accept: bool = True


@dataclass
class HealthSpec:
    enabled: bool = False
    interval: int = 30
    timeout: int = 5
    method: str = "screen-alive"


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
class OrochiSpec:
    enabled: bool = False
    hosts: list[str] = field(default_factory=list)
    port: int = 8559
    token_env: str = "SCITEX_OROCHI_TOKEN"
    channels: list[str] = field(default_factory=list)
    heartbeat_interval: int = 60


@dataclass
class RemoteSpec:
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
        return bool(self.host)


@dataclass
class SkillsSpec:
    required: list[str] = field(default_factory=list)  # Auto-loaded at startup
    available: list[str] = field(default_factory=list)  # Available but not auto-loaded


@dataclass
class StartupCommand:
    delay: int = 0  # seconds after startup
    command: str = ""


@dataclass
class AgentConfig:
    """Parsed agent configuration from a YAML definition file."""

    name: str
    runtime: str = "claude-code"
    model: str = "sonnet"
    workdir: str = "~/proj"
    venv: str = ""  # path to virtualenv (e.g. ~/.venv); activates before claude
    env: dict[str, str] = field(default_factory=dict)
    screen_name: str = ""
    labels: dict[str, str] = field(default_factory=dict)
    container: ContainerSpec = field(default_factory=ContainerSpec)
    claude: ClaudeSpec = field(default_factory=ClaudeSpec)
    health: HealthSpec = field(default_factory=HealthSpec)
    watchdog: WatchdogSpec = field(default_factory=WatchdogSpec)
    restart: RestartSpec = field(default_factory=RestartSpec)
    hooks: dict[str, list[str]] = field(default_factory=dict)
    telegram: TelegramSpec = field(default_factory=TelegramSpec)
    orochi: OrochiSpec = field(default_factory=OrochiSpec)
    remote: RemoteSpec = field(default_factory=RemoteSpec)
    skills: SkillsSpec = field(default_factory=SkillsSpec)
    startup_commands: list[StartupCommand] = field(default_factory=list)
    mcp_servers: dict[str, dict] = field(default_factory=dict)
    multiplexer: str = "tmux"  # "tmux" (default) or "screen"
    config_path: str = ""

    def __post_init__(self) -> None:
        if not self.screen_name:
            self.screen_name = f"cld-{self.name}"

    @property
    def expanded_workdir(self) -> str:
        return str(Path(self.workdir).expanduser())
