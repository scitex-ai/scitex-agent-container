"""Resolve the one provider secret a brokered agent start declares.

The host listener may be launched by systemd or a non-interactive shell that
has no provider key in its process environment. Agent specs name the approved
secret source via ``provider.auth_token_env``; the listener reads the canonical
SAC secret pool and propagates only that declared variable to the child
``sac agents start`` process. Missing remains missing so the normal start gate
fails closed. Secret values are never logged or serialized.
"""

from __future__ import annotations

from collections.abc import Mapping

from ..config import AgentConfig, load_config
from ..config._resolve import resolve_with_prefix
from ..runtimes._secret_pool import PoolRead, read_pool


def provider_secret_env(
    config: AgentConfig,
    child_env: Mapping[str, str],
    *,
    pool: PoolRead | None = None,
) -> dict[str, str]:
    """Return only the declared provider key, or an empty mapping.

    An explicit value already inherited by the child wins. Otherwise the
    canonical pool is read once. An absent or unreadable pool never invents a
    value and never selects another provider; the downstream provider guard
    then refuses the start with its existing missing-key diagnostic.
    """
    provider = getattr(getattr(config, "claude", None), "provider", None)
    env_name = str(getattr(provider, "auth_token_env", "") or "").strip()
    if not env_name:
        return {}
    inherited = str(child_env.get(env_name) or "")
    if inherited:
        return {env_name: inherited}
    observed = pool if pool is not None else read_pool()
    value = str(observed.env.get(env_name) or "")
    return {env_name: value} if value else {}


def provider_secret_env_for_agent(
    name: str,
    child_env: Mapping[str, str],
) -> dict[str, str]:
    """Resolve ``name`` and return its declared provider-key overlay.

    Spec lookup/load failures deliberately return no overlay: the child start
    owns those diagnostics and must still fail through the canonical path.
    """
    try:
        config = load_config(resolve_with_prefix(name))
    except Exception:  # stx-allow: fallback (the canonical child start reports spec failures)
        return {}
    return provider_secret_env(config, child_env)


__all__ = ["provider_secret_env", "provider_secret_env_for_agent"]
