"""Bounded exact-tool-call probe for a resolved inference engine.

The probe is intentionally smaller than an agent run: it sends one forced
tool call in the selected harness's wire dialect, validates the exact name and
arguments, and exits.  Credentials are accepted only as request input and are
never retained in the result.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from ._harness_lookup import canonical_harness
from ._launch_plan import Endpoint, LaunchPlan, ResolvedEngine

_TOOL_NAME = "sac_engine_probe"
_PROTOCOL_PATHS = {
    "openai-responses": "/responses",
    "anthropic-messages": "/messages",
}
_HARNESS_PROTOCOLS = {
    "anthropic": "anthropic-messages",
    "codex": "openai-responses",
    "openai": "openai-responses",
}


@dataclass(frozen=True)
class ToolProbeResult:
    """Credential-free result of one engine conformance attempt."""

    ok: bool
    protocol: str
    diagnostic: str
    returned_name: str | None = None
    returned_arguments: object = None


def _schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {"nonce": {"type": "string"}},
        "required": ["nonce"],
        "additionalProperties": False,
    }


def _endpoint_url(base_url: str, protocol: str) -> str:
    """Turn a provider API root into the selected protocol endpoint."""
    base = str(base_url or "").strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("engine provider.base_url must be an absolute HTTP URL")
    wanted = _PROTOCOL_PATHS[protocol]
    path = parsed.path.rstrip("/")
    if path.endswith(wanted):
        return base
    other_endpoints = ("/responses", "/messages", "/chat/completions")
    if any(path.endswith(suffix) for suffix in other_endpoints):
        raise ValueError(
            f"provider.base_url {base!r} names a different protocol endpoint; "
            f"{protocol} requires a URL ending in {wanted}"
        )
    if path.endswith("/v1"):
        return f"{base}{wanted}"
    return f"{base}/v1{wanted}"


def build_probe_plan(config: Any, *, harness: str | None = None) -> LaunchPlan:
    """Adapt a current resolved ``AgentConfig`` to the probe transport shape.

    Engine selection remains owned by the v3 loader and engine library.  This
    adapter only chooses the wire protocol and concrete endpoint after that
    selection has been folded onto ``config``.
    """
    selected_harness = canonical_harness(harness or getattr(config, "harness", ""))
    if selected_harness not in _HARNESS_PROTOCOLS:
        name = harness or getattr(config, "harness", "") or "<unset>"
        raise ValueError(
            f"engine-check does not support harness {name!r}; exact probes are "
            "available for claude-code/anthropic, codex, and openai"
        )
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None)
    base_url = str(getattr(provider, "base_url", "") or "").strip()
    auth_env = str(getattr(provider, "auth_token_env", "") or "").strip()
    if not base_url or not auth_env:
        raise ValueError(
            "engine-check requires a provider-backed engine with base_url and "
            "auth_token_env; OAuth-only engines do not expose a probe credential"
        )
    protocol = _HARNESS_PROTOCOLS[selected_harness]
    endpoint = Endpoint(
        protocol=protocol,
        url=_endpoint_url(base_url, protocol),
        auth_kind="api-key" if protocol == "anthropic-messages" else "bearer",
        auth_env=auth_env,
    )
    model = str(getattr(config, "model", "") or getattr(claude, "model", "")).strip()
    if not model:
        raise ValueError("engine-check requires the selected engine to name a model")
    engine = ResolvedEngine(
        key=str(getattr(config, "engine_key", "") or model),
        model_id=model,
        endpoints=(endpoint,),
        context_window_tokens=getattr(config, "max_context_tokens", None),
        reasoning_effort=(
            str(getattr(config, "reasoning_effort", "") or "").strip() or None
        ),
        upstream_deadline_seconds=getattr(config, "upstream_deadline_seconds", None),
        client_abandonment_seconds=getattr(config, "client_abandonment_seconds", None),
    )
    return LaunchPlan(
        harness=selected_harness,
        launch_mode="headless",
        container_backend="apptainer",
        engine=engine,
        endpoint=endpoint,
        agent_name=str(getattr(config, "name", "") or "") or None,
    )


def build_tool_probe_request(plan: LaunchPlan, nonce: str) -> dict[str, Any]:
    """Build one forced, strict tool call in the endpoint's native dialect."""
    prompt = f"Call {_TOOL_NAME} exactly once with nonce exactly {nonce}."
    if plan.endpoint.protocol == "openai-responses":
        return {
            "model": plan.engine.model_id,
            "input": prompt,
            "tools": [
                {
                    "type": "function",
                    "name": _TOOL_NAME,
                    "description": "Return the supplied conformance nonce.",
                    "parameters": _schema(),
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "function", "name": _TOOL_NAME},
            "parallel_tool_calls": False,
            "max_output_tokens": 512,
            "stream": False,
        }
    if plan.endpoint.protocol == "anthropic-messages":
        return {
            "model": plan.engine.model_id,
            "messages": [{"role": "user", "content": prompt}],
            "tools": [
                {
                    "name": _TOOL_NAME,
                    "description": "Return the supplied conformance nonce.",
                    "input_schema": _schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": _TOOL_NAME},
            "max_tokens": 512,
            "stream": False,
        }
    raise ValueError(f"tool probe does not support {plan.endpoint.protocol!r}")


def validate_tool_probe_response(
    protocol: str, body: object, nonce: str
) -> ToolProbeResult:
    """Require exactly one call with the declared name and exact arguments."""
    if not isinstance(body, dict):
        return ToolProbeResult(False, protocol, "response body is not an object")
    if protocol == "openai-responses":
        output = body.get("output", [])
        if not isinstance(output, list):
            output = []
        calls = [
            item
            for item in output
            if isinstance(item, dict) and item.get("type") == "function_call"
        ]
        if len(calls) != 1:
            return ToolProbeResult(
                False, protocol, f"expected one function call, received {len(calls)}"
            )
        name = calls[0].get("name")
        arguments = calls[0].get("arguments")
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return ToolProbeResult(
                False, protocol, "function arguments are not JSON", name, arguments
            )
    elif protocol == "anthropic-messages":
        content = body.get("content", [])
        if not isinstance(content, list):
            content = []
        calls = [
            item
            for item in content
            if isinstance(item, dict) and item.get("type") == "tool_use"
        ]
        if len(calls) != 1:
            return ToolProbeResult(
                False, protocol, f"expected one tool use, received {len(calls)}"
            )
        name = calls[0].get("name")
        arguments = calls[0].get("input")
    else:
        raise ValueError(f"tool probe does not support {protocol!r}")
    expected = {"nonce": nonce}
    if name != _TOOL_NAME:
        return ToolProbeResult(
            False, protocol, "tool name does not match", name, arguments
        )
    if arguments != expected:
        return ToolProbeResult(
            False, protocol, "tool arguments do not match", name, arguments
        )
    return ToolProbeResult(True, protocol, "exact tool call received", name, arguments)


def _headers(endpoint: Endpoint, key: str) -> dict[str, str]:
    headers = {"content-type": "application/json"}
    if endpoint.auth_kind == "bearer":
        headers["authorization"] = f"Bearer {key}"
    elif endpoint.auth_kind == "api-key":
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"
    return headers


def probe_engine_tools(
    plan: LaunchPlan,
    *,
    key: str = "",
    timeout_s: float = 30.0,
    nonce: str | None = None,
    opener: Callable[..., Any] = urlopen,
) -> ToolProbeResult:
    """Make one bounded conformance request without exposing the credential."""
    if plan.endpoint.auth_kind != "none" and not key:
        raise ValueError(f"{plan.endpoint.auth_env} is required for the tool probe")
    nonce = nonce or secrets.token_hex(12)
    payload = build_tool_probe_request(plan, nonce)
    request = Request(
        plan.endpoint.url,
        data=json.dumps(payload).encode(),
        headers=_headers(plan.endpoint, key),
        method="POST",
    )
    with opener(request, timeout=timeout_s) as response:
        body = json.loads(response.read())
    return validate_tool_probe_response(plan.endpoint.protocol, body, nonce)


__all__ = [
    "ToolProbeResult",
    "build_probe_plan",
    "build_tool_probe_request",
    "probe_engine_tools",
    "validate_tool_probe_response",
]
