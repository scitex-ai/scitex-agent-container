"""Compile a neutral launch plan into an isolated Hermes profile."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._hermes_compression import HermesCompressionSpec
from ._hermes_run_budget import DEFAULT_HERMES_RUN_BUDGET_SECONDS
from ._launch_plan import LaunchPlan

AGENT_ID_HEADER = "X-SciTeX-Agent-ID"
SESSION_ID_HEADER = "X-SciTeX-Session-ID"


def _api_root(endpoint_url: str, protocol: str) -> str:
    suffix = {
        "openai-chat-completions": "/chat/completions",
        "openai-responses": "/responses",
    }[protocol]
    parsed = urlsplit(endpoint_url)
    path = parsed.path.removesuffix(suffix).rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def compile_hermes_config(
    plan: LaunchPlan,
    *,
    workdir: str,
    run_budget_seconds: int | None = DEFAULT_HERMES_RUN_BUDGET_SECONDS,
    approval_mode: str = "off",
    compression: HermesCompressionSpec | None = None,
    background_review: bool = False,
    system_prompt: str | None = None,
) -> dict[str, Any]:
    """Return a credential-free Hermes configuration derived from ``plan``."""
    if plan.harness != "hermes":
        raise ValueError(f"Hermes compiler received harness {plan.harness!r}")
    if plan.agent_name is None:
        raise ValueError("Hermes compiler requires an agent-bound launch plan")
    if plan.launch_mode not in {"headless", "tui"}:
        raise ValueError("Hermes requires launch_mode 'headless' or 'tui'")
    if plan.endpoint.protocol not in {
        "openai-chat-completions",
        "openai-responses",
    }:
        raise ValueError(
            f"unsupported Hermes endpoint protocol {plan.endpoint.protocol!r}"
        )
    if plan.endpoint.auth_kind == "none":
        key_env = ""
    else:
        key_env = plan.endpoint.auth_env
    if run_budget_seconds is not None and (
        type(run_budget_seconds) is not int or run_budget_seconds <= 0
    ):
        raise ValueError("run_budget_seconds must be a positive integer")
    if approval_mode not in {"manual", "smart", "off"}:
        raise ValueError("approval_mode must be manual, smart, or off")
    if type(background_review) is not bool:
        raise ValueError("background_review must be a boolean")
    compression = compression or HermesCompressionSpec()
    workspace = str(PurePosixPath(workdir))
    if not workspace.startswith("/"):
        raise ValueError("workdir must be an absolute container path")

    provider_key = f"sac-{plan.engine.key}"
    model = plan.engine.model_id
    api_mode = {
        "openai-chat-completions": "chat_completions",
        "openai-responses": "responses",
    }[plan.endpoint.protocol]
    model_config: dict[str, Any] = {}
    if plan.engine.context_window_tokens is not None:
        model_config["context_length"] = plan.engine.context_window_tokens
    if plan.engine.client_abandonment_seconds is not None:
        model_config["timeout_seconds"] = plan.engine.client_abandonment_seconds
        model_config["stale_timeout_seconds"] = plan.engine.client_abandonment_seconds
    provider: dict[str, Any] = {
        "name": f"SAC {plan.engine.key}",
        "base_url": _api_root(plan.endpoint.url, plan.endpoint.protocol),
        "key_env": key_env,
        "transport": api_mode,
        "model": model,
        "default_model": model,
        "models": {model: model_config},
        "extra_headers": {
            AGENT_ID_HEADER: plan.agent_name,
            SESSION_ID_HEADER: f"sac:{plan.agent_name}",
        },
    }
    agent: dict[str, Any] = {
        # SAC's autonomous loop has its own independent safety cap.  Hermes'
        # TUI defaults an omitted value to 500, so emit its unlimited sentinel.
        "max_turns": "none",
        # Hermes subtracts disabled toolsets after expanding ``hermes-cli``.
        # Naming its one-tool ``delegation`` toolset removes delegate_task
        # completely instead of relying on prompt compliance.
        "disabled_toolsets": [] if plan.may_spawn else ["delegation"],
    }
    if run_budget_seconds is not None:
        agent["run_budget_seconds"] = run_budget_seconds
    if plan.engine.reasoning_effort is not None:
        agent["reasoning_effort"] = plan.engine.reasoning_effort
    if system_prompt is not None:
        if not system_prompt.strip():
            raise ValueError("Hermes system_prompt must contain non-whitespace text")
        agent["system_prompt"] = system_prompt
    return {
        "model": {
            "default": model,
            # Hermes' provider resolver reserves bare names for its bundled
            # registry.  SAC-generated entries live in ``providers:`` and
            # therefore must be selected through the named-custom identity.
            "provider": f"custom:{provider_key}",
            "api_mode": api_mode,
        },
        "providers": {provider_key: provider},
        "fallback_providers": [],
        "toolsets": ["hermes-cli"],
        "agent": agent,
        "delegation": {
            "max_concurrent_children": plan.delegation.max_concurrent_children,
            # SAC permits one bounded fan-out from the owning agent. Children
            # remain leaves so concurrency cannot multiply by tree depth.
            "max_spawn_depth": 1,
            "orchestrator_enabled": False,
            "worktree_isolation": plan.delegation.worktree_isolation,
        },
        "approvals": {"mode": approval_mode},
        "compression": {
            "enabled": True,
            "threshold": compression.threshold,
            **(
                {"threshold_tokens": compression.threshold_tokens}
                if compression.threshold_tokens is not None
                else {}
            ),
            "target_ratio": compression.target_ratio,
            "tail_mode": compression.tail_mode,
            "in_place": compression.in_place,
        },
        "display": {"busy_input_mode": "steer"},
        "terminal": {
            "backend": "local",
            "cwd": workspace,
            "auto_source_bashrc": False,
        },
        "auxiliary": {
            "title_generation": {"enabled": False},
            "background_review": {"enabled": background_review},
        },
    }


__all__ = ["AGENT_ID_HEADER", "SESSION_ID_HEADER", "compile_hermes_config"]
