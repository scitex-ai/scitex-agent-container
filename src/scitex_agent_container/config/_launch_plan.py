"""Pure, immutable engine/harness selection for the v4 pilot.

No fleet library, environment, credentials, or filesystem is read here.
Legacy lifecycle adapters continue to use v3 until explicitly migrated.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from urllib.parse import urlsplit

from ._delegation_types import (
    DEFAULT_MAX_CONCURRENT_CHILDREN,
    MAX_CONCURRENT_CHILDREN,
)
from ._provider_types import is_credential_header


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    url: str
    auth_kind: str
    auth_env: str
    extra_headers: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class ResolvedEngine:
    key: str
    model_id: str
    endpoints: tuple[Endpoint, ...]
    context_window_tokens: int | None
    reasoning_effort: str | None
    upstream_deadline_seconds: int | None = None
    client_abandonment_seconds: int | None = None


@dataclass(frozen=True)
class DelegationPolicy:
    max_concurrent_children: int = DEFAULT_MAX_CONCURRENT_CHILDREN
    worktree_isolation: bool = True


@dataclass(frozen=True)
class LaunchPlan:
    harness: str
    launch_mode: str
    container_backend: str
    engine: ResolvedEngine
    endpoint: Endpoint
    may_spawn: bool = True
    delegation: DelegationPolicy = DelegationPolicy()
    agent_name: str | None = None
    session_id: str | None = None


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


def _delegation_policy(spec: Mapping) -> tuple[bool, DelegationPolicy]:
    lineage_value = spec.get("lineage")
    lineage = (
        {} if lineage_value is None else _mapping(lineage_value, "spec.lineage")
    )
    may_spawn = lineage.get("may_spawn", True)
    if not isinstance(may_spawn, bool):
        raise ValueError("spec.lineage.may_spawn must be a boolean")

    delegation_value = spec.get("delegation")
    raw = (
        {}
        if delegation_value is None
        else _mapping(delegation_value, "spec.delegation")
    )
    _keys(
        raw,
        {"max_concurrent_children", "worktree_isolation"},
        "spec.delegation",
    )
    maximum = raw.get(
        "max_concurrent_children", DEFAULT_MAX_CONCURRENT_CHILDREN
    )
    if (
        type(maximum) is not int
        or maximum <= 0
        or maximum > MAX_CONCURRENT_CHILDREN
    ):
        raise ValueError(
            "spec.delegation.max_concurrent_children must be an integer between "
            f"1 and {MAX_CONCURRENT_CHILDREN}"
        )
    isolation = raw.get("worktree_isolation", True)
    if not isinstance(isolation, bool):
        raise ValueError("spec.delegation.worktree_isolation must be a boolean")
    return may_spawn, DelegationPolicy(maximum, isolation)


def _endpoint(protocol: str, value: object, path: str) -> Endpoint:
    if protocol not in _PATHS:
        raise ValueError(f"{path}: unsupported protocol {protocol!r}")
    data = _mapping(value, path)
    _keys(data, {"url", "auth", "extra_headers"}, path)
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
    raw_headers = _mapping(data.get("extra_headers", {}), f"{path}.extra_headers")
    headers: list[tuple[str, str]] = []
    seen_names: set[str] = set()
    for raw_name, raw_value in raw_headers.items():
        name = _text(raw_name, f"{path}.extra_headers key")
        value = _text(raw_value, f"{path}.extra_headers.{name}")
        if any(character in name for character in "\r\n:"):
            raise ValueError(f"{path}.extra_headers contains invalid name {name!r}")
        if is_credential_header(name):
            raise ValueError(
                f"{path}.extra_headers must not contain credential header "
                f"{name!r}; declare authentication through auth.env"
            )
        if "\r" in value or "\n" in value:
            raise ValueError(
                f"{path}.extra_headers.{name} must not contain a newline"
            )
        folded = name.casefold()
        if folded in seen_names:
            raise ValueError(
                f"{path}.extra_headers repeats case-insensitive header {name!r}"
            )
        seen_names.add(folded)
        headers.append((name, value))
    return Endpoint(protocol, url, kind, env, tuple(headers))


def compile_launch_plan(
    spec: Mapping,
    *,
    engine: str | None = None,
    harness: str | None = None,
    agent_name: str | None = None,
    session_id: str | None = None,
) -> LaunchPlan:
    """Compile the selection section of a self-contained spec, without I/O.

    This validates every engine entry, including inactive ones. Other agent
    sections are validated by their owners, not silently claimed here.
    Protocol preference is deterministic and part of the harness adapter
    contract; it never substitutes a different model or inference engine.
    """
    spec = _mapping(spec, "spec")
    if agent_name is not None:
        agent_name = _text(agent_name, "agent_name")
        if not all(
            character in "abcdefghijklmnopqrstuvwxyz0123456789-_"
            for character in agent_name
        ):
            raise ValueError(
                "agent_name must use lowercase letters, digits, '-' and '_' only"
            )
    if session_id is not None:
        session_id = _text(session_id, "session_id")
        if "\r" in session_id or "\n" in session_id:
            raise ValueError("session_id must not contain a newline")
    elif agent_name is not None:
        session_id = f"sac:{agent_name}"
    may_spawn, delegation = _delegation_policy(spec)
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
        _keys(entry, {"model", "endpoints", "parameters", "timeouts"}, path)
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
        timeout_values = _mapping(entry.get("timeouts", {}), f"{path}.timeouts")
        _keys(
            timeout_values,
            {"upstream_deadline_seconds", "client_abandonment_seconds"},
            f"{path}.timeouts",
        )
        upstream_deadline = timeout_values.get("upstream_deadline_seconds")
        client_abandonment = timeout_values.get("client_abandonment_seconds")
        for timeout_name, timeout_value in (
            ("upstream_deadline_seconds", upstream_deadline),
            ("client_abandonment_seconds", client_abandonment),
        ):
            if timeout_value is not None and (
                type(timeout_value) is not int or timeout_value <= 0
            ):
                raise ValueError(
                    f"{path}.timeouts.{timeout_name} must be a positive integer"
                )
        if (upstream_deadline is None) != (client_abandonment is None):
            raise ValueError(
                f"{path}.timeouts must declare upstream_deadline_seconds and "
                "client_abandonment_seconds together"
            )
        if (
            upstream_deadline is not None
            and client_abandonment <= upstream_deadline
        ):
            raise ValueError(
                f"{path}.timeouts.client_abandonment_seconds must be greater than "
                "upstream_deadline_seconds"
            )
        resolved[label] = ResolvedEngine(
            label,
            model,
            endpoint_values,
            context,
            effort,
            upstream_deadline,
            client_abandonment,
        )
    if key not in resolved:
        raise ValueError(f"unknown engine {key!r}; available: {', '.join(resolved)}")
    selected = resolved[key]
    for protocol in _PROTOCOLS[family]:
        endpoint = next((e for e in selected.endpoints if e.protocol == protocol), None)
        if endpoint is not None:
            return LaunchPlan(
                family,
                mode,
                "apptainer",
                selected,
                endpoint,
                may_spawn=may_spawn,
                delegation=delegation,
                agent_name=agent_name,
                session_id=session_id,
            )
    raise ValueError(
        f"harness {family!r} cannot use engine {key!r}: requires one of {_PROTOCOLS[family]}"
    )
