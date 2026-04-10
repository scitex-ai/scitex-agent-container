"""YAML config loading and validation for agent definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


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
class RemoteSpec:
    host: str = ""  # SSH host (hostname or IP)
    user: str = ""  # SSH user
    key: str = ""  # Path to SSH key (optional)
    port: int = 22  # SSH port
    timeout: int = 60  # SSH command timeout in seconds
    login_shell: bool = True  # Use bash -l -c (needed for PATH on most hosts)

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
    remote: RemoteSpec = field(default_factory=RemoteSpec)
    skills: SkillsSpec = field(default_factory=SkillsSpec)
    startup_commands: list[StartupCommand] = field(default_factory=list)
    config_path: str = ""

    def __post_init__(self) -> None:
        if not self.screen_name:
            self.screen_name = f"cld-{self.name}"

    @property
    def expanded_workdir(self) -> str:
        return str(Path(self.workdir).expanduser())


def _get_nested(data: dict, key: str, default: Any = None) -> Any:
    """Traverse a dot-separated key path in a nested dict."""
    keys = key.split(".")
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current


def load_config(path: str | Path) -> AgentConfig:
    """Load and validate a YAML config, returning an AgentConfig."""
    path = Path(path).resolve()
    with open(path) as f:
        raw = yaml.safe_load(f)

    errors = _validate_raw(raw, str(path))
    if errors:
        raise ValueError(
            f"Config validation failed for {path}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    metadata = raw.get("metadata", {})
    spec = raw.get("spec", {})

    # Container spec
    container_raw = spec.get("container", {}) or {}
    container = ContainerSpec(
        runtime=container_raw.get("runtime", "none"),
        image=container_raw.get("image", "scitex-agent-container:latest"),
        volumes=container_raw.get("volumes", []) or [],
        network=container_raw.get("network", "host"),
    )

    # Claude spec
    claude_raw = spec.get("claude", {}) or {}
    claude = ClaudeSpec(
        channels=claude_raw.get("channels", []) or [],
        flags=claude_raw.get("flags", []) or [],
        session=claude_raw.get("session", "new"),
    )

    # Health spec
    health_raw = spec.get("health", {}) or {}
    health = HealthSpec(
        enabled=health_raw.get("enabled", False),
        interval=health_raw.get("interval", 30),
        timeout=health_raw.get("timeout", 5),
        method=health_raw.get("method", "screen-alive"),
    )

    # Watchdog spec
    watchdog_raw = spec.get("watchdog", {}) or {}
    watchdog_responses = watchdog_raw.get("responses", {}) or {}
    watchdog = WatchdogSpec(
        enabled=watchdog_raw.get("enabled", False),
        interval=float(watchdog_raw.get("interval", 1.5)),
        resp_y_n=str(watchdog_responses.get("y_n", "1")),
        resp_y_y_n=str(watchdog_responses.get("y_y_n", "2")),
        resp_waiting=str(watchdog_responses.get("waiting", "/speak-and-call")),
    )

    # Restart spec
    restart_raw = spec.get("restart", {}) or {}
    backoff_raw = restart_raw.get("backoff", {}) or {}
    restart = RestartSpec(
        policy=restart_raw.get("policy", "never"),
        max_retries=restart_raw.get("max_retries", 3),
        backoff_initial=backoff_raw.get("initial", 30),
        backoff_max=backoff_raw.get("max", 300),
        backoff_multiplier=backoff_raw.get("multiplier", 2),
    )

    # Screen name
    screen_raw = spec.get("screen", {}) or {}
    screen_name = screen_raw.get("name", "")

    # Hooks
    hooks_raw = spec.get("hooks", {}) or {}
    hooks = {}
    for key in ("pre_start", "post_start", "pre_stop", "post_stop"):
        hooks[key] = hooks_raw.get(key, []) or []

    # Telegram spec
    telegram_raw = spec.get("telegram", {}) or {}
    telegram = TelegramSpec(
        bot_token_env=telegram_raw.get(
            "bot_token_env", "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
        ),
        allowed_users=[str(u) for u in (telegram_raw.get("allowed_users", []) or [])],
        auto_connect=telegram_raw.get("auto_connect", True),
        greeting=telegram_raw.get("greeting", ""),
    )

    # Skills spec
    skills_raw = spec.get("skills", {}) or {}
    skills = SkillsSpec(
        required=skills_raw.get("required", []) or [],
        available=skills_raw.get("available", []) or [],
    )

    # Remote spec
    remote_raw = spec.get("remote", {}) or {}
    remote = RemoteSpec(
        host=remote_raw.get("host", ""),
        user=remote_raw.get("user", ""),
        key=remote_raw.get("key", ""),
        port=int(remote_raw.get("port", 22)),
        login_shell=remote_raw.get("login_shell", True),
    )

    # Startup commands
    startup_raw = spec.get("startup_commands", []) or []
    startup_commands = [
        StartupCommand(
            delay=int(item.get("delay", 0)),
            command=item.get("command", ""),
        )
        for item in startup_raw
        if isinstance(item, dict) and item.get("command")
    ]

    return AgentConfig(
        name=metadata["name"],
        runtime=spec.get("runtime", "claude-code"),
        model=spec.get("model", "sonnet"),
        workdir=spec.get("workdir", "~/proj"),
        venv=spec.get("venv", ""),
        env=spec.get("env", {}) or {},
        screen_name=screen_name,
        labels=metadata.get("labels", {}) or {},
        container=container,
        claude=claude,
        health=health,
        watchdog=watchdog,
        restart=restart,
        hooks=hooks,
        telegram=telegram,
        remote=remote,
        skills=skills,
        startup_commands=startup_commands,
        config_path=str(path),
    )


def resolve_config(name_or_path: str) -> str:
    """Resolve agent name or path to a config file path."""
    from pathlib import Path

    p = Path(name_or_path)
    if "/" in name_or_path or name_or_path.endswith((".yaml", ".yml")):
        if p.exists():
            return str(p)
        raise FileNotFoundError(f"Config file not found: {name_or_path}")
    user_dir = Path.home() / ".scitex" / "agent-container" / "agents"
    for ext in (".yaml", ".yml"):
        candidate = user_dir / f"{name_or_path}{ext}"
        if candidate.exists():
            return str(candidate)
        # Subdirectory convention: agents/<name>/<name>.yaml
        candidate = user_dir / name_or_path / f"{name_or_path}{ext}"
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(
        f"Agent '{name_or_path}' not found in ~/.scitex/agent-container/agents/\n"
        f"  Create: cp templates/... "
        f"~/.scitex/agent-container/agents/{name_or_path}.yaml"
    )


def _validate_raw(raw: dict, path: str) -> list[str]:
    """Validate raw YAML dict. Returns list of error strings (empty means valid)."""
    errors: list[str] = []

    if not isinstance(raw, dict):
        return [f"Config file is not a YAML mapping: {path}"]

    # apiVersion
    api_version = raw.get("apiVersion")
    if api_version != "cld-agent/v1":
        errors.append(f"apiVersion must be 'cld-agent/v1', got '{api_version}'")

    # kind
    kind = raw.get("kind")
    if kind != "Agent":
        errors.append(f"kind must be 'Agent', got '{kind}'")

    # metadata.name
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata is required and must be a mapping")
    elif not metadata.get("name"):
        errors.append("metadata.name is required")

    # spec
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        errors.append("spec is required and must be a mapping")
    else:
        # spec.runtime
        runtime = spec.get("runtime")
        valid_runtimes = ("claude-code", "cursor", "aider")
        if runtime and runtime not in valid_runtimes:
            errors.append(
                f"spec.runtime must be one of {valid_runtimes}, got '{runtime}'"
            )

        # container.runtime
        container = spec.get("container", {}) or {}
        cr = container.get("runtime")
        if cr and cr not in ("none", "docker", "apptainer"):
            errors.append(
                f"spec.container.runtime must be none|docker|apptainer, got '{cr}'"
            )

        # container.network
        network = container.get("network")
        if network and network not in ("host", "bridge", "none"):
            errors.append(
                f"spec.container.network must be host|bridge|none, got '{network}'"
            )

        # restart.policy
        restart = spec.get("restart", {}) or {}
        policy = restart.get("policy")
        if policy and policy not in ("never", "on-failure", "always"):
            errors.append(
                f"spec.restart.policy must be never|on-failure|always, got '{policy}'"
            )

        # health.method
        health = spec.get("health", {}) or {}
        method = health.get("method")
        if method and method not in ("screen-alive",):
            errors.append(f"spec.health.method must be 'screen-alive', got '{method}'")

    return errors


def validate_config(path: str | Path) -> list[str]:
    """Validate a config file and return list of errors (empty = valid)."""
    path = Path(path).resolve()
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"File not found: {path}"]
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    return _validate_raw(raw, str(path))
