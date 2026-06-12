"""Resolve a parsed ProviderSpec union to a runtime-ready ResolvedProvider.

Operator directive 2026-06-08: the SDK env-injection path
(``runtimes/_apptainer_provider.provider_env_flags``) used to branch
on whether ``provider`` was a dict or a registered string; the new
runtime surface consumes ONE typed shape — :class:`ResolvedProvider`
— and never re-sniffs at the operator boundary.

This module owns the resolution rules:

* Model precedence chain (highest wins):
    1. ``CustomProvider.model`` — explicit in the spec.
    2. ``RegistryProvider.model_override`` — Form B override.
    3. Registry entry's ``default_model``.
    4. ``ClaudeSpec.model`` — back-compat fallback (existing
       behaviour for legacy specs).
    5. None → :class:`ProviderResolutionError`. A provider override
       MUST land a model id, else the SDK falls back to the built-in
       Anthropic default which doesn't exist on the override backend
       and surfaces as a baffling 404 mid-turn.

* Endpoint shape:
    * :class:`DirectEndpoint` → ``base_url`` flows through as-is to
      :attr:`ResolvedProvider.base_url`.
    * :class:`TunneledEndpoint` → ``base_url`` is None at resolve
      time; the runtime side (which holds the live local port the
      tunnel manager bound) recomputes the URL via
      :func:`with_tunneled_base_url`. The resolver still returns a
      :class:`ResolvedProvider`, but with the ``tunnel`` field
      populated so the caller can route through the manager.

* Auth env name:
    * :class:`CustomProvider.auth_token_env` direct.
    * :class:`RegistryProvider` → registry entry's ``auth_token_env``.

* Label:
    * :class:`CustomProvider.label` direct.
    * :class:`RegistryProvider` → registry entry's ``label``.
"""

from __future__ import annotations

from typing import Any

from ._provider_types import (
    CustomProvider,
    DirectEndpoint,
    ProviderSpec,
    RegistryProvider,
    ResolvedProvider,
    TunneledEndpoint,
)


class ProviderResolutionError(RuntimeError):
    """Raised when a ProviderSpec cannot be resolved to a complete record.

    Surfaces with the agent-facing context (which field is empty, what
    the model precedence chain looked at) so the operator sees how to
    fix their spec without re-reading sac source.
    """


def _registry_entry(name: str, registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Look up a name in the merged registry or raise.

    The validator already rejects unknown names at spec-load time, so
    a miss here is a defensive guard against a hand-built AgentConfig
    that bypasses the YAML path. The error names the available
    providers so the operator can pick from the right set.
    """
    entry = registry.get(name)
    if entry is None:
        known = ", ".join(sorted(registry)) or "(empty registry)"
        raise ProviderResolutionError(
            f"provider '{name}' is not registered. Known providers: "
            f"{known}. Add an entry to PROVIDERS or drop a YAML into "
            "~/.scitex/agent-container/providers.d/."
        )
    return entry


def resolve_provider_spec(
    spec: ProviderSpec,
    registry: dict[str, dict[str, Any]],
    *,
    claude_model_fallback: str = "",
) -> ResolvedProvider:
    """Return the :class:`ResolvedProvider` for a parsed provider spec.

    Args:
        spec: A :class:`RegistryProvider` or :class:`CustomProvider`
            (the public :data:`ProviderSpec` union).
        registry: The merged provider registry (built-in + overlay).
            Pass :func:`_provider_registry_d.load_merged_registry` to
            include operator overlays.
        claude_model_fallback: The ``ClaudeSpec.model`` value, used as
            step 4 of the model precedence chain. Default empty string
            means "no fallback" — step 5 (loud error) then fires when
            no prior step lands a model id.

    Returns:
        A :class:`ResolvedProvider` with every field populated. For a
        :class:`TunneledEndpoint` the ``base_url`` is the empty string
        — callers MUST overlay the live local-bound URL via
        :func:`with_tunneled_base_url` after the tunnel manager binds
        a port. The ``tunnel`` field is populated in that case so the
        runtime can route through the manager.

    Raises:
        ProviderResolutionError: Model precedence chain bottomed out
            (no model id from any source); a RegistryProvider names
            an unknown backend; or a custom provider carries an
            unrecognized endpoint shape (only possible from a
            hand-built AgentConfig).
    """
    if isinstance(spec, RegistryProvider):
        entry = _registry_entry(spec.name, registry)
        endpoint_raw = entry.get("endpoint")
        label = entry.get("label") or spec.name
        auth_env = entry.get("auth_token_env") or ""
        # Model precedence: override (Form B) → registry default →
        # ClaudeSpec.model. The validator rejects an "anthropic"
        # sentinel before it reaches here (RegistryProvider with no
        # endpoint isn't constructed by the parser), but the chain
        # still tolerates the case for hand-built configs.
        model = (
            spec.model_override
            or entry.get("default_model")
            or claude_model_fallback
            or ""
        )
        if not model:
            raise ProviderResolutionError(
                f"provider '{spec.name}' could not resolve a model id: "
                "neither RegistryProvider.model_override, the registry "
                f"default_model, nor spec.claude.model is set. Pick one "
                "(e.g. 'provider: {name: " + spec.name + ", model: ...}')."
            )
        base_url = ""
        tunnel = None
        if isinstance(endpoint_raw, dict):
            if "base_url" in endpoint_raw:
                base_url = endpoint_raw.get("base_url") or ""
            elif "tunnel" in endpoint_raw:
                # The registry stores the tunnel as a dict; we don't
                # construct a TunnelSpec here because the lifecycle
                # wiring lives on the runtime side. Surface the dict
                # via a TunneledEndpoint cast in the caller's
                # ResolvedProvider when needed. The tunnel field on
                # ResolvedProvider always holds a TunnelSpec dataclass
                # — see :func:`tunnel_spec_from_dict` below for the
                # conversion.
                tunnel = _tunnel_spec_from_dict(endpoint_raw["tunnel"])
        return ResolvedProvider(
            base_url=base_url,
            model=model,
            auth_token_env=auth_env,
            label=label,
            tunnel=tunnel,
            allowed_tools=[],
        )

    if isinstance(spec, CustomProvider):
        # CustomProvider carries the endpoint as a typed sub-union;
        # no dict-shape sniffing required.
        base_url = ""
        tunnel = None
        endpoint = spec.endpoint
        if isinstance(endpoint, DirectEndpoint):
            base_url = endpoint.base_url
        elif isinstance(endpoint, TunneledEndpoint):
            tunnel = endpoint.tunnel
        else:  # pragma: no cover — guarded by the sealed union
            raise ProviderResolutionError(
                f"custom provider '{spec.label}' carries an unrecognized "
                f"endpoint shape: {type(endpoint).__name__}"
            )
        # CustomProvider.model is REQUIRED by the validator; the
        # fallback chain is honored here for hand-built configs.
        model = spec.model or claude_model_fallback
        if not model:
            raise ProviderResolutionError(
                f"custom provider '{spec.label}' has no model id "
                "(CustomProvider.model is empty and ClaudeSpec.model is "
                "empty). Set 'spec.claude.provider.model: <id>'."
            )
        return ResolvedProvider(
            base_url=base_url,
            model=model,
            auth_token_env=spec.auth_token_env,
            label=spec.label,
            tunnel=tunnel,
            allowed_tools=list(spec.allowed_tools),
        )

    raise ProviderResolutionError(
        f"unrecognized provider shape: {type(spec).__name__}. "
        "Expected RegistryProvider or CustomProvider."
    )


def _tunnel_spec_from_dict(raw: dict[str, Any]):
    """Coerce a registry-shape tunnel dict into a :class:`TunnelSpec`.

    Mirrors the :func:`_parsers._claude` parser's TunnelSpec
    construction so registry overlays and inlined operator blocks
    feed the runtime through the same dataclass shape. Defensive
    typing: missing optional knobs default to the dataclass values.
    """
    from ._tunnel_types import TunnelSpec

    return TunnelSpec(
        jump_host=str(raw.get("jump_host") or ""),
        target_host=str(raw.get("target_host") or ""),
        remote_port=int(raw.get("remote_port") or 0),
        local_port=int(raw.get("local_port") or 0),
        wait_timeout_s=int(raw.get("wait_timeout_s") or 30),
        respawn_backoff_s=int(raw.get("respawn_backoff_s") or 2),
        ssh_opts=list(raw.get("ssh_opts") or []),
    )


def with_tunneled_base_url(
    resolved: ResolvedProvider, local_port: int
) -> ResolvedProvider:
    """Return a copy of ``resolved`` with the live tunnel base_url filled in.

    Called by the runtime side after :class:`TunnelManager` binds a
    port. Keeps the resolver free of subprocess concerns (it only
    knows shapes) while still emitting ONE typed
    :class:`ResolvedProvider` for the env-flag path to consume.
    """
    if resolved.tunnel is None:
        # Defensive: the runtime should only call this on a tunneled
        # provider. Loud error so a mis-wired call site is obvious
        # rather than silently overwriting a direct base_url.
        raise ProviderResolutionError(
            f"with_tunneled_base_url called on a non-tunneled "
            f"ResolvedProvider (label={resolved.label!r}); only call "
            "when ResolvedProvider.tunnel is not None."
        )
    if local_port <= 0:
        raise ProviderResolutionError(
            f"with_tunneled_base_url given non-positive local_port="
            f"{local_port}; the tunnel manager must bind a port first."
        )
    return ResolvedProvider(
        base_url=f"http://localhost:{local_port}",
        model=resolved.model,
        auth_token_env=resolved.auth_token_env,
        label=resolved.label,
        tunnel=resolved.tunnel,
        allowed_tools=list(resolved.allowed_tools),
    )


__all__ = [
    "ProviderResolutionError",
    "resolve_provider_spec",
    "with_tunneled_base_url",
]
