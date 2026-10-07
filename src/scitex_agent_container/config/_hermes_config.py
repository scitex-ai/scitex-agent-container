"""Compile a neutral launch plan into an isolated Hermes profile."""

from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from ._hermes_compression import HermesCompressionSpec
from ._hermes_goals import HermesGoalSpec
from ._hermes_run_budget import DEFAULT_HERMES_RUN_BUDGET_SECONDS
from ._launch_plan import LaunchPlan

AGENT_ID_HEADER = "X-SciTeX-Agent-ID"
SESSION_ID_HEADER = "X-SciTeX-Session-ID"
AGENT_ID_TEMPLATE = "${sac:agent_id}"
SESSION_ID_TEMPLATE = "${sac:session_id}"


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
    max_turns: int | None = None,
    goals: HermesGoalSpec | None = None,
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
    if plan.session_id is None:
        raise ValueError("Hermes compiler requires a session-bound launch plan")
    if plan.launch_mode not in {"headless", "tui"}:
        raise ValueError("Hermes requires launch_mode 'headless' or 'tui'")
    native_provider = ""
    if plan.endpoint.protocol.startswith("hermes-native:"):
        native_provider = plan.endpoint.protocol.split(":", 1)[1].strip()
        if not native_provider:
            raise ValueError("hermes-native endpoint names no provider")
    elif plan.endpoint.protocol not in {
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
    if max_turns is not None and (type(max_turns) is not int or max_turns <= 0):
        raise ValueError("max_turns must be a positive integer")
    goals = goals or HermesGoalSpec()
    if goals.max_turns is not None and (
        type(goals.max_turns) is not int or goals.max_turns <= 0
    ):
        raise ValueError("goals.max_turns must be a positive integer")
    if goals.judge_engine and goals.judge_engine != plan.engine.key:
        raise ValueError("Hermes goals.judge_engine must be the selected agent engine")
    compression = compression or HermesCompressionSpec()
    workspace = str(PurePosixPath(workdir))
    if not workspace.startswith("/"):
        raise ValueError("workdir must be an absolute container path")

    provider_key = f"sac-{plan.engine.key}"
    model = plan.engine.model_id
    session_id = plan.session_id
    extra_headers = {
        name: value.replace(AGENT_ID_TEMPLATE, plan.agent_name).replace(
            SESSION_ID_TEMPLATE, session_id
        )
        for name, value in plan.endpoint.extra_headers
    }
    extra_headers.update(
        {
            AGENT_ID_HEADER: plan.agent_name,
            SESSION_ID_HEADER: session_id,
        }
    )
    api_mode = {
        "openai-chat-completions": "chat_completions",
        "openai-responses": "responses",
    }.get(plan.endpoint.protocol)
    model_config: dict[str, Any] = {}
    if plan.engine.context_window_tokens is not None:
        model_config["context_length"] = plan.engine.context_window_tokens
    if plan.engine.client_abandonment_seconds is not None:
        model_config["timeout_seconds"] = plan.engine.client_abandonment_seconds
        model_config["stale_timeout_seconds"] = plan.engine.client_abandonment_seconds
    if native_provider:
        # Native Hermes provider: Hermes owns endpoint/protocol/session
        # handling (e.g. opencode-go's x-opencode-session). SAC only names
        # the provider + model; the key resolves from the agent env.
        model_block: dict[str, Any] = {
            "default": model,
            "provider": native_provider,
        }
        providers_block: dict[str, Any] = {}
    else:
        assert api_mode is not None
        provider: dict[str, Any] = {
            "name": f"SAC {plan.engine.key}",
            "base_url": _api_root(plan.endpoint.url, plan.endpoint.protocol),
            "key_env": key_env,
            "transport": api_mode,
            "model": model,
            "default_model": model,
            "models": {model: model_config},
            "extra_headers": extra_headers,
        }
        model_block = {
            "default": model,
            # Hermes' provider resolver reserves bare names for its bundled
            # registry.  SAC-generated entries live in ``providers:`` and
            # therefore must be selected through the named-custom identity.
            "provider": f"custom:{provider_key}",
            "api_mode": api_mode,
            # Auxiliary judge calls (goal_judge) build their own client and
            # do NOT inherit the provider entry's extra_headers — without
            # x-opencode-session OpenCode Go answers 400 MissingSessionID
            # and every goal pauses after 5 transport failures. Verified live.
            "extra_headers": {
                "x-opencode-session": session_id,
            },
        }
        providers_block = {provider_key: provider}
    agent: dict[str, Any] = {
        # SAC's autonomous loop has its own independent safety cap.  Hermes'
        # TUI defaults an omitted value to 500, so emit its unlimited sentinel.
        "max_turns": "none" if max_turns is None else max_turns,
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
    auxiliary: dict[str, Any] = {
        "title_generation": {"enabled": False},
        "background_review": {"enabled": background_review},
        # Operator rule: the goal judge runs on the same Muse route as
        # the agent itself (muse-spark-1.3-contributor via the primary
        # custom provider). An undefined goal_judge falls back to Hermes
        # defaults, which left fleet agents' goal loops dying on
        # unreachable-judge pauses — the definition (spec + generated
        # profile) must show everything, no implicit behavior.
        "goal_judge": {
            # Same lane the agent itself runs on (native name or the
            # named-custom identity — never a Hermes default).
            # Operator order 2026-10-06: judge model is
            # meta-muse-spark-1.3-contributor, always xhigh.
            "provider": model_block["provider"],
            "model": "meta-muse-spark-1.3-contributor",
            "reasoning_effort": "xhigh",
            "timeout": 60,
            "max_tokens": 4096,
        },
    }
    if goals.judge_engine:
        # Genuine pinned-Hermes API: `main` resolves the live primary provider
        # and its pool; explicit model avoids a provider's cheap aux default.
        auxiliary["goal_judge"] = {"provider": "main", "model": model}
    return {
        "model": model_block,
        "providers": providers_block,
        "fallback_providers": [],
        # Operator order 2026-10-06: the Hermes default goal budget (20
        # turns) stalls fleet agents mid-work. The SAC autonomous loop is
        # the spend backstop; the goal budget must not be the tighter one.
        # Operator CCT 3820: every running agent gets goals.max_turns 99999
        # on the running config immediately.
        "goals": {"max_turns": 99999},
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
        # Operator decision 2026-09-19 (direct, twice): the leads run on Muse Spark
        # 1.3 **contributor**, i.e. a tier that trains on prompts and completions, and
        # the operator accepted that explicitly ("すべて保存学習してくれて良いのでメタの方を使っていきましょう").
        # Hermes refuses to select a data-training tier in a non-interactive run unless
        # this is acknowledged, and its acknowledgement is a config key, not a flag
        # (hermes_cli/main.py:1099). Without it every unattended Muse start wedges on an
        # invisible "Use this model for this invocation? [y/N]" prompt -- which is exactly
        # what happened at 00:29 on compute-03. Emitted here so the decision is made once,
        # in the generated config, for every agent that selects such a tier.
        "security": {"allow_data_training_tiers_noninteractive": True},
        "terminal": {
            "backend": "local",
            "cwd": workspace,
            "auto_source_bashrc": False,
            # CCT rail: the agent's own bot token + id must reach its Bash
            # tool env. Hermes scrubs provider credentials from tool children
            # but CCT_BOT_TOKEN/CCT_AGENT_ID are NOT blocklisted, so listing
            # them in terminal.env_passthrough forwards the inherited values.
            "env_passthrough": ["CCT_BOT_TOKEN", "CCT_AGENT_ID"],
        },
        "auxiliary": auxiliary,
        **(
            {"goals": {"max_turns": goals.max_turns}}
            if goals.max_turns is not None
            else {}
        ),
    }


__all__ = [
    "AGENT_ID_HEADER",
    "AGENT_ID_TEMPLATE",
    "SESSION_ID_HEADER",
    "SESSION_ID_TEMPLATE",
    "compile_hermes_config",
]
