"""Lifecycle glue between agent_start / agent_stop and the TunnelManager.

Operator directive 2026-06-08: an agent whose
``spec.claude.provider`` resolves to a :class:`TunneledEndpoint`
needs the local ``ssh -L`` ProxyJump forward up BEFORE the runtime
constructs its env flags (so ``ANTHROPIC_BASE_URL`` reflects the
live local bind), and torn down AFTER ``runtime.stop`` (so a final
heartbeat going through the dying tunnel can still complete).

Lives in its own module to:

* keep ``_start.py`` under the 512-line cap, and
* expose a single test-friendly seam for the injection (``tunnel_manager_factory``)
  so the start path stays one straight-line read.

The runtime env flags read the bound port off the AgentConfig
via the synthetic attribute :attr:`_TUNNEL_LOCAL_PORT_ATTR`. The
runtime's ``_apptainer_provider.provider_env_flags`` accepts the
port through a kwarg so the runtime stays state-free; this module
is where the bound port surfaces from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .._network._tunnel_manager import TunnelManager, TunnelUpError
from ..config import AgentConfig
from ..config._provider_types import CustomProvider, RegistryProvider
from ..config._tunnel_types import TunnelSpec
from . import _tunnels

# Attribute name on AgentConfig where the bound local port is stashed so
# downstream consumers can read it without re-querying the registry.
_TUNNEL_LOCAL_PORT_ATTR = "_sac_tunnel_local_port"


def _resolve_tunnel_spec(config: AgentConfig) -> Optional[TunnelSpec]:
    """Return the :class:`TunnelSpec` for this agent's provider, or ``None``.

    Handles both shapes of the sealed provider union:

    * :class:`CustomProvider` with a :class:`TunneledEndpoint` endpoint.
    * :class:`RegistryProvider` resolving to a registry entry whose
      ``endpoint`` carries a ``tunnel`` block (e.g. a
      ``providers.d/qwen-spartan.yaml`` overlay).

    Returns ``None`` for non-tunneled providers or no provider at all.
    """
    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None) if claude is not None else None
    if provider is None:
        return None
    if isinstance(provider, CustomProvider):
        from ..config._provider_types import TunneledEndpoint

        if isinstance(provider.endpoint, TunneledEndpoint):
            return provider.endpoint.tunnel
        return None
    if isinstance(provider, RegistryProvider):
        from ..config._provider_registry_d import load_merged_registry
        from ..config._provider_resolve import _tunnel_spec_from_dict

        registry = load_merged_registry()
        entry = registry.get(provider.name)
        if entry is None:
            return None
        endpoint = entry.get("endpoint")
        if isinstance(endpoint, dict) and "tunnel" in endpoint:
            return _tunnel_spec_from_dict(endpoint["tunnel"])
    return None


def _default_state_dir() -> Path:
    """Return the sac state-dir on this host. Mirrors the layout used elsewhere."""
    return Path.home() / ".scitex" / "agent-container"


def _default_tunnel_manager_factory(
    spec: TunnelSpec, agent_name: str, state_dir: Path
) -> TunnelManager:
    """Construct a real :class:`TunnelManager` with the production supervisor cmd."""
    return TunnelManager(spec=spec, agent_name=agent_name, state_dir=state_dir)


def maybe_start_provider_tunnel(
    config: AgentConfig,
    *,
    tunnel_manager_factory: Optional[
        Callable[[TunnelSpec, str, Path], TunnelManager]
    ] = None,
    state_dir: Optional[Path] = None,
) -> Optional[int]:
    """If the provider is tunneled, bring the tunnel up; return bound local_port.

    Returns ``None`` when the agent's provider is not tunneled (the
    normal case). For a tunneled provider:

    1. Resolve the :class:`TunnelSpec` from the provider union.
    2. Build a :class:`TunnelManager` via the (injectable) factory.
    3. Call ``mgr.up()``; on success stash the bound port on the
       AgentConfig (so the runtime can read it without re-resolving)
       and register the manager so :func:`stop_provider_tunnel` can
       find it.
    4. On :class:`TunnelUpError`, re-raise as :class:`RuntimeError`
       with the original message preserved.

    Args:
        config: The agent config; the resolved provider lives at
            ``config.claude.provider``.
        tunnel_manager_factory: OPTIONAL injectable factory. Default
            produces a real :class:`TunnelManager`. Tests pass a
            factory that hands back a fake manager whose ``up()``
            opens a listening socket without spawning ssh.
        state_dir: OPTIONAL sac state directory. Default is
            ``~/.scitex/agent-container``.

    Raises:
        RuntimeError: When the tunnel fails to come up; sac aborts
            the start instead of building the runtime against an
            empty ``ANTHROPIC_BASE_URL``.
    """
    tunnel = _resolve_tunnel_spec(config)
    if tunnel is None:
        return None
    factory = tunnel_manager_factory or _default_tunnel_manager_factory
    sdir = state_dir if state_dir is not None else _default_state_dir()
    mgr = factory(tunnel, config.name, sdir)
    try:
        port = mgr.up()
    except TunnelUpError as exc:
        # Re-raise as RuntimeError so the start path's standard error
        # surface (RuntimeError → non-zero CLI exit + clear stderr)
        # handles it. The TunnelUpError chain is preserved via __cause__.
        raise RuntimeError(
            f"agent '{config.name}': provider tunnel failed to come up: {exc}"
        ) from exc
    setattr(config, _TUNNEL_LOCAL_PORT_ATTR, port)
    _tunnels.register(config.name, mgr)
    return port


def stop_provider_tunnel(config: AgentConfig) -> None:
    """Tear down the agent's provider tunnel if one is active. Idempotent.

    Looks up the manager in the process-local registry first
    (covers the same-process start+stop flow). If absent (cross-process
    stop, e.g. operator running ``sac agents stop`` from a different
    shell than the one that started the agent), constructs a fresh
    manager and lets it read the pidfile to find the supervisor.
    """
    tunnel = _resolve_tunnel_spec(config)
    if tunnel is None:
        return
    mgr = _tunnels.get(config.name)
    if mgr is None:
        # Cross-process recovery — pidfile carries the supervisor pid.
        mgr = TunnelManager(
            spec=tunnel,
            agent_name=config.name,
            state_dir=_default_state_dir(),
        )
    try:
        mgr.down()
    finally:
        _tunnels.discard(config.name)


def get_tunnel_local_port(config: Any) -> Optional[int]:
    """Read the bound local port off the AgentConfig (set by start_provider_tunnel).

    Accepts ``Any`` so test stubs (SimpleNamespace, real configs) all
    work. Returns ``None`` when no tunnel is active on this agent.
    """
    return getattr(config, _TUNNEL_LOCAL_PORT_ATTR, None)


__all__ = [
    "maybe_start_provider_tunnel",
    "stop_provider_tunnel",
    "get_tunnel_local_port",
]
