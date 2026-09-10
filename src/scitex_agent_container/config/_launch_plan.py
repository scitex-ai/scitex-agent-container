"""Pure, immutable engine/harness selection for the v4 pilot.

No fleet library, environment, credentials, or filesystem is read here.
Legacy lifecycle adapters continue to use v3 until explicitly migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    url: str
    auth_kind: str
    auth_env: str


@dataclass(frozen=True)
class ResolvedEngine:
    key: str
    model_id: str
    endpoints: tuple[Endpoint, ...]
    context_window_tokens: int | None
    reasoning_effort: str | None


@dataclass(frozen=True)
class LaunchPlan:
    harness: str
    launch_mode: str
    container_backend: str
    engine: ResolvedEngine
    endpoint: Endpoint


_ALIASES = {"anthropic": "claude-code", "claude": "claude-code"}
_PROTOCOLS = {
    "claude-code": ("anthropic-messages",),
    "codex": ("openai-responses",),
    "hermes": ("openai-chat-completions", "openai-responses"),
    "pi": ("openai-responses", "anthropic-messages"),
}
_PATHS = {
    "openai-responses": "/responses",
    "openai-chat-completions": "/chat/completions",
    "anthropic-messages": "/messages",
}


def _mapping(value: object, path: str) -> Mapping:
    if not isinstance(value, Mapping):
        raise ValueError(f"{path} must be a mapping")
    return value


def _keys(value: Mapping, allowed: set[str], path: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{path}: unknown fields {sorted(unknown, key=str)}")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    return value.strip()


def _endpoint(protocol: str, value: object, path: str) -> Endpoint:
    if protocol not in _PATHS:
        raise ValueError(f"{path}: unsupported protocol {protocol!r}")
    data = _mapping(value, path)
    _keys(data, {"url", "auth"}, path)
    url = _text(data.get("url"), f"{path}.url")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or not parsed.path.endswith(_PATHS[protocol])
    ):
        raise ValueError(
            f"{path}.url must be an HTTP endpoint ending in {_PATHS[protocol]} without userinfo, query or fragment"
        )
    auth = _mapping(data.get("auth"), f"{path}.auth")
    _keys(auth, {"kind", "env"}, f"{path}.auth")
    kind = auth.get("kind")
    if kind not in ("bearer", "api-key", "none"):
        raise ValueError(f"{path}.auth.kind must be bearer, api-key or none")
    env = "" if kind == "none" else _text(auth.get("env"), f"{path}.auth.env")
    if kind == "none" and "env" in auth:
        raise ValueError(f"{path}: unauthenticated endpoints must not name an auth env")
    if env and (not env.isidentifier() or not env.isascii()):
        raise ValueError(f"{path}.auth.env must name an environment variable")
    return Endpoint(protocol, url, kind, env)


def compile_launch_plan(
    spec: Mapping, *, engine: str | None = None, harness: str | None = None
) -> LaunchPlan:
    """Compile the selection section of a self-contained spec, without I/O.

    This validates every engine entry, including inactive ones. Other agent
    sections are validated by their owners, not silently claimed here.
    Protocol preference is deterministic and part of the harness adapter
    contract; it never substitutes a different model or inference engine.
    """
    spec = _mapping(spec, "spec")
    family = _text(
        harness if harness is not None else spec.get("harness"), "spec.harness"
    )
    family = _ALIASES.get(family, family)
    if family not in _PROTOCOLS:
        raise ValueError(f"unsupported harness {family!r}")
    mode = spec.get("launch_mode")
    if mode not in ("tui", "headless"):
        raise ValueError("spec.launch_mode must be tui or headless")
    container = _mapping(spec.get("container"), "spec.container")
    if container.get("backend") != "apptainer":
        raise ValueError("spec.container.backend must be apptainer")
    key = _text(engine if engine is not None else spec.get("engine"), "spec.engine")
    canonical_entries = spec.get("available_engines")
    legacy_entries = spec.get("engines")
    if canonical_entries is not None and legacy_entries is not None:
        if canonical_entries != legacy_entries:
            raise ValueError("spec.available_engines and spec.engines disagree")
    authored_entries = (
        canonical_entries if canonical_entries is not None else legacy_entries
    )
    entries = _mapping(authored_entries, "spec.available_engines")
    resolved = {}
    for label, value in entries.items():
        _text(label, "engine key")
        path = f"spec.available_engines.{label}"
        entry = _mapping(value, path)
        _keys(entry, {"model", "endpoints", "parameters"}, path)
        model = _text(entry.get("model"), f"{path}.model")
        endpoints = _mapping(entry.get("endpoints"), f"{path}.endpoints")
        if not endpoints:
            raise ValueError(f"{path}.endpoints must not be empty")
        endpoint_values = tuple(
            _endpoint(p, v, f"{path}.endpoints.{p}") for p, v in endpoints.items()
        )
        params = _mapping(entry.get("parameters", {}), f"{path}.parameters")
        _keys(
            params, {"context_window_tokens", "reasoning_effort"}, f"{path}.parameters"
        )
        context = params.get("context_window_tokens")
        if context is not None and (type(context) is not int or context <= 0):
            raise ValueError(
                f"{path}.parameters.context_window_tokens must be a positive integer"
            )
        effort = params.get("reasoning_effort")
        if effort is not None:
            effort = _text(effort, f"{path}.parameters.reasoning_effort")
        resolved[label] = ResolvedEngine(label, model, endpoint_values, context, effort)
    if key not in resolved:
        raise ValueError(f"unknown engine {key!r}; available: {', '.join(resolved)}")
    selected = resolved[key]
    for protocol in _PROTOCOLS[family]:
        endpoint = next((e for e in selected.endpoints if e.protocol == protocol), None)
        if endpoint is not None:
            return LaunchPlan(family, mode, "apptainer", selected, endpoint)
    raise ValueError(
        f"harness {family!r} cannot use engine {key!r}: requires one of {_PROTOCOLS[family]}"
    )
