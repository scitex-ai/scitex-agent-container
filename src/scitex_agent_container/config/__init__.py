"""YAML config loading and validation for agent definitions.

Public API:
    AgentConfig, load_config, validate_config, resolve_config
    ContainerSpec, ClaudeSpec, HealthSpec, WatchdogSpec, RestartSpec,
    SkillsSpec, StartupCommand

``RemoteSpec`` and the ``spec.remote`` field were deleted in WI-6
(handoff §6, 2026-05-20). Cross-host placement is via ``spec.host``;
the old SSH-dispatch path is retired.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._host import resolve_hostname, substitute_hostnames
from ._loaders import compose_effective_name, load_v3
from ._provider_types import ProviderSpec
from ._proxy_types import ProxySpec
from ._resolve import resolve_config
from ._types import (
    AgentConfig,
    ClaudeSpec,
    ContainerSpec,
    ContextManagementConfig,
    HealthSpec,
    HookSpec,
    HostsSpec,
    ListenPort,
    RestartSpec,
    SchedulingSpec,
    SkillsSpec,
    StartupCommand,
    WatchdogSpec,
)
from ._validation import validate_config, validate_raw

__all__ = [
    "AgentConfig",
    "ClaudeSpec",
    "ContainerSpec",
    "ContextManagementConfig",
    "HealthSpec",
    "HookSpec",
    "HostsSpec",
    "ListenPort",
    "ProviderSpec",
    "ProxySpec",
    "RestartSpec",
    "SchedulingSpec",
    "SkillsSpec",
    "StartupCommand",
    "WatchdogSpec",
    "compose_effective_name",
    "load_config",
    "resolve_config",
    "resolve_hostname",
    "substitute_hostnames",
    "validate_config",
]


def load_config(path: str | Path) -> AgentConfig:
    """Load and validate a YAML config, returning an AgentConfig.

    Only ``scitex-agent-container/v3`` is accepted. Older apiVersions
    (v1, v2) raise loud validation errors — no backward compatibility.
    """
    path = Path(path).resolve()
    with open(path) as f:
        raw = yaml.safe_load(f)

    errors = validate_raw(raw, str(path))
    if errors:
        raise ValueError(
            f"Config validation failed for {path}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    config = load_v3(raw, path)
    _warn_if_assigned_account_missing(config)
    _warn_if_startup_prompt_long(config)
    return config


def _warn_if_assigned_account_missing(config: AgentConfig) -> None:
    """Soft-WARN (never fail) when ``spec.claude.account`` names an
    account whose snapshot dir is absent at load time.

    Accounts may be created later or live on another host, so a missing
    snapshot is not a hard error — but surfacing it at load time catches
    typos before the agent silently falls back to the host live file at
    start. Best-effort: any resolution hiccup is swallowed.
    """
    acct = getattr(getattr(config, "claude", None), "account", "") or ""
    if not acct:
        return
    # stx-allow: fallback (reason: store-path resolution is best-effort
    # advisory only; a hiccup must never break config loading.)
    try:
        import warnings

        from .._state.account_store import _store_path

        store = _store_path(None, Path.home())
        if not (store / acct).is_dir():
            warnings.warn(
                f"spec.claude.account='{acct}' has no saved-account "
                f"snapshot at {store / acct}; the agent will fall back "
                "to the host live ~/.claude/.credentials.json at start. "
                "Create it with `sac account save {acct}` (on the host "
                "that holds those credentials), or ignore if the account "
                "is provisioned on the target host.",
                stacklevel=2,
            )
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        pass


# A startup_prompt is a per-boot KICK, not durable context. Past these sizes it
# is almost certainly role/rules/workflow PROSE that belongs in CLAUDE.md +
# skills (see _warn_if_startup_prompt_long). Generous so a real boot-kick never
# trips them; the trimmed proj-scitex-dev kick (~430 chars, 1 line) clears both.
_STARTUP_PROMPT_WARN_CHARS = 600
_STARTUP_PROMPT_WARN_LINES = 8


def _warn_if_startup_prompt_long(config: AgentConfig) -> None:
    """Soft-WARN (never fail) when a ``spec.startup_prompts`` entry is long.

    startup_prompts are pasted into the agent as a per-boot user TURN — replayed
    once at start, not persistent context. Durable ROLE / SCOPE / RULES /
    WORKFLOW prose therefore does NOT belong there: it bloats every boot, stale-
    replays on restart, and (multi-line) stresses the TUI paste-submit path. Such
    prose belongs in the auto-loaded ``$HOME/.claude/CLAUDE.md`` (role + skill
    ``@``-imports) and reusable rules in ``.claude/skills/`` — claude re-reads
    those EVERY session. Keep startup_prompts to a short boot-KICK (what to DO on
    start). Best-effort: any hiccup must never break config loading.
    """
    try:
        import warnings

        for idx, prompt in enumerate(getattr(config, "startup_prompts", []) or []):
            text = str(prompt)
            n_chars = len(text)
            n_lines = text.count("\n") + 1
            if n_chars <= _STARTUP_PROMPT_WARN_CHARS and (
                n_lines <= _STARTUP_PROMPT_WARN_LINES
            ):
                continue
            warnings.warn(
                f"spec.startup_prompts[{idx}] for agent "
                f"'{getattr(config, 'name', '?')}' is long "
                f"({n_chars} chars, {n_lines} lines). startup_prompts are pasted "
                "as a per-boot user turn — durable ROLE / RULES / WORKFLOW prose "
                "belongs in the auto-loaded $HOME/.claude/CLAUDE.md (role + skill "
                "@-imports) and reusable rules in .claude/skills/, which claude "
                "re-reads every session. Keep startup_prompts to a short boot-KICK "
                "(what to DO on start); move the prose to CLAUDE.md + skills.",
                stacklevel=2,
            )
    except Exception:  # stx-allow: fallback (reason: advisory only; never break load)
        pass
