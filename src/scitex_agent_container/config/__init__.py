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

from ._delegation_types import DelegationSpec
from ._engine_types import (
    EngineDefaultError,
    EngineError,
    EngineSpec,
    UnknownEngineError,
    apply_engine,
    select_engine,
)
from ._hermes_compression import HermesCompressionSpec
from ._hermes_context_budget import (
    HermesFleetBudgetError,
    HermesFleetContextBudget,
    derive_hermes_fleet_context_budget,
)
from ._host import resolve_hostname, substitute_hostnames
from ._loaders import compose_effective_name, load_v3
from ._provider_types import ProviderSpec
from ._proxy_types import ProxySpec
from ._resolve import resolve_config
from ._types import (
    AgentConfig,
    ClaudeSpec,
    ContainerSpec,
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
    "DelegationSpec",
    "EngineDefaultError",
    "EngineError",
    "EngineSpec",
    "HealthSpec",
    "HookSpec",
    "HermesCompressionSpec",
    "HermesFleetBudgetError",
    "HermesFleetContextBudget",
    "HostsSpec",
    "ListenPort",
    "ProviderSpec",
    "ProxySpec",
    "RestartSpec",
    "SchedulingSpec",
    "SkillsSpec",
    "StartupCommand",
    "UnknownEngineError",
    "WatchdogSpec",
    "apply_engine",
    "compose_effective_name",
    "derive_hermes_fleet_context_budget",
    "load_config",
    "resolve_config",
    "resolve_hostname",
    "select_engine",
    "substitute_hostnames",
    "validate_config",
]


def load_config(path: str | Path, *, advise: bool = False) -> AgentConfig:
    """Load and validate a YAML config, returning an AgentConfig.

    Only ``scitex-agent-container/v3`` is accepted. Older apiVersions
    (v1, v2) raise loud validation errors — no backward compatibility.

    ``advise`` turns on the STYLE advisories (long startup_prompts, and
    similar authoring lints). It defaults to OFF, and that default is the
    point: these are lints about how a spec is WRITTEN, not about whether it
    LOADS, so they must not fire on every command that happens to read specs.

    `sac agents list` loads all ~110 definitions, so a load-time advisory
    printed 18 WARN lines above the table on every invocation. A warning that
    appears when you did not ask a question about spec style is one the reader
    learns to scroll past -- which also blinds them to the ones that matter.
    Advisories therefore surface where they are actionable: `sac agents check`.
    """
    path = Path(path).resolve()

    # Parsed-spec cache, keyed on (size, mtime_ns). `sac agents list` parses
    # every definition on the host and spent ~4.5s of ~9.4s doing exactly this;
    # `watch -n 10 sac agents list` re-pays it on every tick because each tick
    # is a fresh process. Any doubt is a MISS that falls through to a real
    # parse, so the cache can only change SPEED, never the answer.
    from . import _spec_cache

    raw = _spec_cache.get(path)
    if raw is None:
        with open(path) as f:
            raw = yaml.safe_load(f)
        _spec_cache.put(path, raw)

    errors = validate_raw(raw, str(path))
    if errors:
        raise ValueError(
            f"Config validation failed for {path}:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    config = load_v3(raw, path)
    if advise:
        _warn_if_assigned_account_missing(config)
        _warn_if_startup_prompt_long(config)
    return config


def _config_logger():
    """Logger for load-time advisories, routed through scitex-logging.

    scitex-logging gives the fleet-consistent coloured ``WARN:``/``ERRO:``
    stderr lines (operator directive 2026-07-10) instead of raw
    ``warnings.warn`` UserWarning spew with its ``file:line`` + source-echo
    framing. Imported lazily: the package auto-configures handlers on first
    import (~300 ms), which must not tax ``import scitex_agent_container.config``
    itself (see ``21_cli-startup-budget.md``); CLI paths have usually paid it
    already via ``cli_pkg._helpers._console``.
    """
    import scitex_logging

    return scitex_logging.getLogger(__name__)


def _warn_if_assigned_account_missing(config: AgentConfig) -> None:
    """Warn only when the selected Claude Code harness lacks its account.

    ``spec.claude.account`` remains in many migrated Hermes/Codex specs as
    legacy/manual-choice metadata. Those harnesses do not authenticate with a
    Claude Code OAuth snapshot, so warning about that inventory falsely claims
    the stored account is active. Accounts may also live on another host, so a
    missing snapshot is advisory rather than fatal. Best-effort: any resolution
    hiccup is swallowed.
    """
    if str(getattr(config, "harness", "") or "").strip().lower() != "anthropic":
        return
    acct = getattr(getattr(config, "claude", None), "account", "") or ""
    if not acct:
        return
    # stx-allow: fallback (reason: store-path resolution is best-effort
    # advisory only; a hiccup must never break config loading.)
    try:
        from .._state.account_store import _store_path

        store = _store_path(None, Path.home())
        if not (store / acct).is_dir():
            _config_logger().warning(
                f"spec.claude.account='{acct}' has no saved-account "
                f"snapshot at {store / acct}; the agent will fall back "
                "to the host live ~/.claude/.credentials.json at start. "
                f"Create it with `sac account save {acct}` (on the host "
                "that holds those credentials), or ignore if the account "
                "is provisioned on the target host."
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
        for idx, prompt in enumerate(getattr(config, "startup_prompts", []) or []):
            text = str(prompt)
            n_chars = len(text)
            n_lines = text.count("\n") + 1
            if n_chars <= _STARTUP_PROMPT_WARN_CHARS and (
                n_lines <= _STARTUP_PROMPT_WARN_LINES
            ):
                continue
            _config_logger().warning(
                f"spec.startup_prompts[{idx}] for agent "
                f"'{getattr(config, 'name', '?')}' is long "
                f"({n_chars} chars, {n_lines} lines). startup_prompts are pasted "
                "as a per-boot user turn — durable ROLE / RULES / WORKFLOW prose "
                "belongs in the auto-loaded $HOME/.claude/CLAUDE.md (role + skill "
                "@-imports) and reusable rules in .claude/skills/, which claude "
                "re-reads every session. Keep startup_prompts to a short boot-KICK "
                "(what to DO on start); move the prose to CLAUDE.md + skills."
            )
    except Exception:  # stx-allow: fallback (reason: advisory only; never break load)
        pass
