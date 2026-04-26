"""Shared spec parsers used by both v1 and v2 config loaders."""

from __future__ import annotations

import re
from typing import Any

from ._types import (
    ClaudeSpec,
    ContainerSpec,
    ContextManagementConfig,
    HealthSpec,
    HostsSpec,
    ListenPort,
    ReadyPattern,
    RemoteSpec,
    RestartSpec,
    SkillsSpec,
    SlurmHooks,
    SlurmSpec,
    StartupCommand,
    StartupSpec,
    TelegramSpec,
    WatchdogSpec,
)


def parse_hosts_spec(spec: dict) -> "HostsSpec":
    """Parse ``spec.host`` / ``spec.hosts`` (mutually exclusive).

    Returns a ``HostsSpec``. Validation of mutual exclusion + value types
    happens in ``_validation.py``; this parser just normalizes shapes:

    * ``host: <str>``    → ``host=str, hosts=""``
    * ``host: [list]``   → ``host=list, hosts=""``
    * ``host:`` (None)   → ``host="", hosts=""``  (local singleton)
    * ``hosts: "all"``   → ``host="", hosts="all"``
    * ``hosts: [list]``  → ``host="", hosts=list``
    """
    host_raw = spec.get("host", None) if "host" in spec else None
    hosts_raw = spec.get("hosts", None) if "hosts" in spec else None

    host: str | list[str] = ""
    hosts: str | list[str] = ""

    if host_raw is not None:
        if isinstance(host_raw, list):
            host = [str(h) for h in host_raw]
        elif isinstance(host_raw, str):
            host = host_raw
        # any other type is caught by the validator; treat as empty here
    if hosts_raw is not None:
        if isinstance(hosts_raw, list):
            hosts = [str(h) for h in hosts_raw]
        elif isinstance(hosts_raw, str):
            hosts = hosts_raw

    return HostsSpec(host=host, hosts=hosts)


# All known hook keys. Unknown keys in the YAML are ignored (forward-compat).
HOOK_KEYS = (
    "pre_start",
    "post_start",
    "pre_stop",
    "post_stop",
    "on_compact",
    "on_restart",
    "on_diff",
)


def parse_container(spec: dict) -> ContainerSpec:
    raw = spec.get("container", {}) or {}
    return ContainerSpec(
        runtime=raw.get("runtime", "none"),
        image=raw.get("image", "scitex-agent-container:latest"),
        volumes=raw.get("volumes", []) or [],
        network=raw.get("network", "host"),
        mount_host_claude=bool(raw.get("mount_host_claude", False)),
    )


def parse_claude(spec: dict) -> ClaudeSpec:
    raw = spec.get("claude", {}) or {}
    # Top-level `session:` takes precedence over `claude.session` for
    # ergonomics (it's the primary knob agents care about). Falls back to
    # the nested field for backward compat, then the default.
    session = spec.get("session")
    if session is None:
        session = raw.get("session", "continue-or-new")
    continue_max_age = raw.get("continue_max_age_minutes")
    if continue_max_age is not None:
        try:
            continue_max_age = int(continue_max_age)
        except (TypeError, ValueError):
            continue_max_age = None
    return ClaudeSpec(
        channels=raw.get("channels", []) or [],
        flags=raw.get("flags", []) or [],
        session=session,
        continue_max_age_minutes=continue_max_age,
        resume_id=str(raw.get("resume_id", "") or ""),
        auto_accept=raw.get("auto_accept", True),
    )


def parse_health(spec: dict) -> HealthSpec:
    raw = spec.get("health", {}) or {}
    return HealthSpec(
        enabled=raw.get("enabled", False),
        interval=raw.get("interval", 30),
        timeout=raw.get("timeout", 5),
        method=raw.get("method", "multiplexer-alive"),
    )


def parse_watchdog(spec: dict) -> WatchdogSpec:
    raw = spec.get("watchdog", {}) or {}
    responses = raw.get("responses", {}) or {}
    return WatchdogSpec(
        enabled=raw.get("enabled", False),
        interval=float(raw.get("interval", 1.5)),
        resp_y_n=str(responses.get("y_n", "1")),
        resp_y_y_n=str(responses.get("y_y_n", "2")),
        resp_waiting=str(responses.get("waiting", "/speak-and-call")),
    )


def parse_restart(spec: dict) -> RestartSpec:
    raw = spec.get("restart", {}) or {}
    backoff = raw.get("backoff", {}) or {}
    return RestartSpec(
        policy=raw.get("policy", "never"),
        max_retries=raw.get("max_retries", 3),
        backoff_initial=backoff.get("initial", 30),
        backoff_max=backoff.get("max", 300),
        backoff_multiplier=backoff.get("multiplier", 2),
    )


def parse_telegram(spec: dict) -> TelegramSpec:
    raw = spec.get("telegram", {}) or {}
    return TelegramSpec(
        bot_token_env=raw.get(
            "bot_token_env", "SCITEX_AGENT_CONTAINER_TELEGRAM_BOT_TOKEN"
        ),
        allowed_users=[str(u) for u in (raw.get("allowed_users", []) or [])],
        auto_connect=raw.get("auto_connect", True),
        greeting=raw.get("greeting", ""),
    )


def parse_slurm(spec: dict) -> SlurmSpec:
    """Parse ``spec.slurm`` block for the SLURM runtime.

    Returns a default ``SlurmSpec`` if the block is absent — the spec is
    still required at runtime when ``spec.runtime == 'slurm'``, but an
    empty block is legal YAML and just means "use all defaults".
    """
    raw = spec.get("slurm", {}) or {}
    hooks_raw = raw.get("hooks", {}) or {}
    hooks = SlurmHooks(
        pre_submit=str(hooks_raw.get("pre_submit", "") or ""),
        pre_agent=str(hooks_raw.get("pre_agent", "") or ""),
        walltime_signal=str(hooks_raw.get("walltime_signal", "") or ""),
        post_agent=str(hooks_raw.get("post_agent", "") or ""),
        attach=str(hooks_raw.get("attach", "") or ""),
    )
    return SlurmSpec(
        partition=str(raw.get("partition", "") or ""),
        time_limit=str(raw.get("time_limit", "1-00:00:00") or "1-00:00:00"),
        cpus_per_task=int(raw.get("cpus_per_task", 1) or 1),
        mem=str(raw.get("mem", "4G") or "4G"),
        nodes=int(raw.get("nodes", 1) or 1),
        ntasks=int(raw.get("ntasks", 1) or 1),
        gres=str(raw.get("gres", "") or ""),
        job_name=str(raw.get("job_name", "") or ""),
        signal=str(raw.get("signal", "B:USR1@3600") or "B:USR1@3600"),
        auto_resubmit=bool(raw.get("auto_resubmit", True)),
        hold=str(raw.get("hold", "tail -f /dev/null") or "tail -f /dev/null"),
        logs_dir=str(raw.get("logs_dir", "~/slurm_logs") or "~/slurm_logs"),
        hooks=hooks,
        extra_directives=[str(d) for d in (raw.get("extra_directives") or [])],
    )


def parse_skills(spec: dict) -> SkillsSpec:
    raw = spec.get("skills", {}) or {}
    mode = (raw.get("injection_mode") or "at-import").strip()
    if mode not in {"block", "at-import"}:
        mode = "at-import"
    valid_strategies = {"skill-id", "tag", "filename"}
    match_by = raw.get("match_by")
    if match_by is None:
        match_by_value = ["skill-id", "tag"]
    else:
        match_by_value = [s for s in match_by if s in valid_strategies]
        if not match_by_value:
            match_by_value = ["skill-id", "tag"]
    style = (raw.get("match_style") or "exact").strip()
    if style not in {"exact", "partial"}:
        style = "exact"
    return SkillsSpec(
        required=raw.get("required", []) or [],
        available=raw.get("available", []) or [],
        injection_mode=mode,
        match_by=match_by_value,
        match_style=style,
    )


def parse_context_management(spec: dict) -> ContextManagementConfig:
    raw = spec.get("context_management", {}) or {}
    try:
        trigger = float(raw.get("trigger_at_percent", 70.0))
    except (TypeError, ValueError):
        trigger = 70.0
    strategy = str(raw.get("strategy", "noop") or "noop")
    if strategy not in ("compact", "restart", "noop"):
        strategy = "noop"
    try:
        warn_n = int(raw.get("warn_before_n_checks", 0))
    except (TypeError, ValueError):
        warn_n = 0
    try:
        interval = int(raw.get("check_interval_seconds", 300))
    except (TypeError, ValueError):
        interval = 300
    state_file = str(
        raw.get("state_file", "~/.scitex/agent-container/state/<agent>.json")
    )
    return ContextManagementConfig(
        trigger_at_percent=trigger,
        strategy=strategy,
        warn_before_n_checks=max(0, warn_n),
        check_interval_seconds=max(1, interval),
        state_file=state_file,
    )


def parse_remote(spec: dict) -> RemoteSpec:
    raw = spec.get("remote", {}) or {}
    return RemoteSpec(
        host=raw.get("host", ""),
        user=raw.get("user", ""),
        key=raw.get("key", ""),
        port=int(raw.get("port", 22)),
        login_shell=raw.get("login_shell", True),
        no_preflight=raw.get("no_preflight", False),
    )


def parse_hooks(spec: dict) -> dict[str, list[str]]:
    raw = spec.get("hooks", {}) or {}
    return {key: list(raw.get(key, []) or []) for key in HOOK_KEYS}


def parse_listen(spec: dict) -> list[ListenPort]:
    """Parse ``spec.listen`` port/socket declarations.

    Container does NOT bind these — declarations only. Entries that
    fail validation (missing port for tcp/udp, missing path for unix)
    are silently dropped so a malformed side-entry can't break startup.
    """
    raw = spec.get("listen", []) or []
    out: list[ListenPort] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        proto = str(item.get("proto", "tcp") or "tcp")
        try:
            port = int(item.get("port", 0) or 0)
        except (TypeError, ValueError):
            port = 0
        path = str(item.get("path", "") or "")
        if proto in ("tcp", "udp") and port <= 0:
            continue
        if proto == "unix" and not path:
            continue
        out.append(
            ListenPort(
                port=port,
                proto=proto,
                path=path,
                name=str(item.get("name", "") or ""),
                owner=str(item.get("owner", "") or ""),
            )
        )
    return out


def parse_extensions(spec: dict) -> dict:
    """Return ``spec.extensions`` verbatim (opaque pass-through)."""
    raw = spec.get("extensions", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def parse_startup_commands(spec: dict) -> list[StartupCommand]:
    raw = spec.get("startup_commands", []) or []
    return [
        StartupCommand(
            delay=int(item.get("delay", 0)),
            command=item.get("command", ""),
        )
        for item in raw
        if isinstance(item, dict) and item.get("command")
    ]


def _parse_command_list(raw: Any) -> list[StartupCommand]:
    out: list[StartupCommand] = []
    for item in raw or []:
        if isinstance(item, str):
            if item:
                out.append(StartupCommand(delay=0, command=item))
        elif isinstance(item, dict) and item.get("command"):
            try:
                delay = int(item.get("delay", 0))
            except (TypeError, ValueError):
                delay = 0
            out.append(StartupCommand(delay=delay, command=str(item["command"])))
    return out


def parse_startup(spec: dict) -> StartupSpec:
    """Parse the opt-in ``spec.startup`` block (todo#291).

    Missing or malformed → empty ``StartupSpec`` (legacy behavior). When
    ``spec.startup.commands`` is absent we shadow the legacy top-level
    ``spec.startup_commands`` so an operator can add a ready gate without
    moving their existing command list.
    """
    raw = spec.get("startup")
    if not isinstance(raw, dict):
        legacy = parse_startup_commands(spec)
        return StartupSpec(commands=legacy)

    patterns_raw = raw.get("ready_patterns", []) or []
    patterns: list[ReadyPattern] = []
    for item in patterns_raw:
        if isinstance(item, str):
            patterns.append(ReadyPattern(regex=item))
        elif isinstance(item, dict) and item.get("regex"):
            patterns.append(ReadyPattern(regex=str(item["regex"])))

    try:
        idle_ticks = max(1, int(raw.get("ready_idle_ticks", 3)))
    except (TypeError, ValueError):
        idle_ticks = 3
    try:
        poll_interval = max(0.05, float(raw.get("ready_poll_interval_seconds", 0.5)))
    except (TypeError, ValueError):
        poll_interval = 0.5
    try:
        timeout = max(1.0, float(raw.get("ready_timeout_seconds", 60.0)))
    except (TypeError, ValueError):
        timeout = 60.0

    on_timeout = str(
        raw.get("on_timeout", "capture_and_proceed") or "capture_and_proceed"
    )
    if on_timeout not in ("capture_and_fail", "capture_and_proceed"):
        on_timeout = "capture_and_proceed"

    commands = _parse_command_list(raw.get("commands"))
    if not commands:
        commands = parse_startup_commands(spec)

    return StartupSpec(
        ready_patterns=patterns,
        ready_idle_ticks=idle_ticks,
        ready_poll_interval_seconds=poll_interval,
        ready_timeout_seconds=timeout,
        on_timeout=on_timeout,
        commands=commands,
    )


def get_nested(data: dict, key: str, default: Any = None) -> Any:
    """Traverse a dot-separated key path in a nested dict."""
    keys = key.split(".")
    current = data
    for k in keys:
        if not isinstance(current, dict) or k not in current:
            return default
        current = current[k]
    return current


# Model name mapping for auto-derived SCITEX_AGENT_CONTAINER_MODEL env var
MODEL_DISPLAY_NAMES: dict[str, str] = {
    "opus": "Claude Opus",
    "opus[1m]": "Claude Opus (1M)",
    "sonnet": "Claude Sonnet",
    "sonnet[1m]": "Claude Sonnet (1M)",
    "haiku": "Claude Haiku",
}


def interpolate_metadata(value: str, metadata: dict) -> str:
    """Replace ${metadata.*} references in a string value."""

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return metadata.get("name", m.group(0))
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            labels = metadata.get("labels", {}) or {}
            return labels.get(label, m.group(0))
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, value)


def interpolate_mcp_servers(mcp_raw: dict, metadata: dict) -> dict[str, dict]:
    """Deep-interpolate ${metadata.*} in mcp_servers env values."""
    result: dict[str, dict] = {}
    for server_name, server_def in (mcp_raw or {}).items():
        entry = dict(server_def)
        if "env" in entry and isinstance(entry["env"], dict):
            entry["env"] = {
                k: interpolate_metadata(str(v), metadata)
                for k, v in entry["env"].items()
            }
        if "args" in entry and isinstance(entry["args"], list):
            entry["args"] = [
                interpolate_metadata(str(a), metadata) for a in entry["args"]
            ]
        result[server_name] = entry
    return result
