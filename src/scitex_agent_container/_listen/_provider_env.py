"""Authorize and resolve the one provider secret a brokered lifecycle call needs.

The host listener may be launched by systemd or a non-interactive shell that
has no provider key in its process environment. Agent specs name the approved
secret source via ``provider.auth_token_env``.  A spec is untrusted input, so
that name is never sufficient authority to read the shared host pool.  The
selected ``(engine, endpoint, env-name)`` must first match this module's
host-owned allowlist.  Only then is exactly one value resolved and propagated.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.responses import JSONResponse

from ..config import AgentConfig, load_config
from ..config._qwen_gateway import (
    qwen_gateway_token_env,
    qwen_gateway_url,
)
from ..config._resolve import resolve_with_prefix
from ..runtimes._secret_pool import PoolRead, read_pool

# This is host policy, not spec data.  An inline POST may copy these strings,
# but it cannot use a different endpoint or env name to turn the listener into
# a shared-secret oracle.  Additions require a code/config deployment on the
# host and a regression test; never derive this set from the submitted spec or
# from whatever variable names happen to exist in the pool.
_STATIC_AUTHORIZED_PROVIDER_SECRETS = frozenset(
    {
        (
            "opencode-go-deepseek-v4.1-flash",
            "https://opencode.ai/zen/go/v1",
            "OPENCODE_GO_API_KEY",
        ),
    }
)


def _authorized_provider_secrets() -> frozenset[tuple[str, str, str]]:
    """Exact provider tuples authorized by host policy at call time."""
    return _STATIC_AUTHORIZED_PROVIDER_SECRETS | {
        ("qwen38-27b", qwen_gateway_url(), qwen_gateway_token_env())
    }


class ProviderPreflightError(RuntimeError):
    """Safe, value-free refusal raised before a lifecycle child is spawned."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(category)


def _provider_tuple(config: AgentConfig) -> tuple[str, str, str] | None:
    provider = getattr(getattr(config, "claude", None), "provider", None)
    if provider is None:
        return None
    return (
        str(getattr(config, "engine_key", "") or "").strip(),
        str(getattr(provider, "base_url", "") or "").strip(),
        str(getattr(provider, "auth_token_env", "") or "").strip(),
    )


def provider_secret_env(
    config: AgentConfig,
    child_env: Mapping[str, str],
    *,
    pool: PoolRead | None = None,
) -> dict[str, str]:
    """Return the authorized provider key or raise a safe preflight refusal.

    Authorization happens before both inherited-env lookup and pool lookup, so
    changing ``auth_token_env`` or ``base_url`` in an inline spec cannot expose
    a host secret.  For an authorized tuple, a non-empty inherited value wins;
    otherwise one verified pool read supplies that exact key.  No fallback key
    is selected.
    """
    selected = _provider_tuple(config)
    if selected is None:
        return {}
    if selected not in _authorized_provider_secrets():
        raise ProviderPreflightError("provider_tuple_unauthorized")
    env_name = selected[2]
    inherited = str(child_env.get(env_name) or "")
    if inherited:
        return {env_name: inherited}
    observed = pool if pool is not None else read_pool()
    if not observed.trusted:
        raise ProviderPreflightError("provider_pool_untrusted")
    value = str(observed.env.get(env_name) or "")
    if not value:
        raise ProviderPreflightError("provider_key_unavailable")
    return {env_name: value}


def provider_secret_env_for_agent(
    name: str,
    child_env: Mapping[str, str],
) -> dict[str, str]:
    """Resolve ``name`` and return its declared provider-key overlay.

    Spec lookup/load failures deliberately return no overlay: no pool lookup
    occurs, so no secret can escape, and the child owns the canonical spec
    diagnostic.  A successfully loaded provider spec is always preflighted.
    """
    try:
        config = load_config(resolve_with_prefix(name))
    except (
        Exception
    ):  # stx-allow: fallback (the canonical child start reports spec failures)
        return {}
    return provider_secret_env(config, child_env)


def provider_preflight_refusal(
    name: str, error: ProviderPreflightError
) -> JSONResponse:
    """Render a safe refusal containing category only, never secret material."""
    status = 403 if error.category == "provider_tuple_unauthorized" else 412
    return JSONResponse(
        {
            "name": name,
            "error": "provider credential preflight refused",
            "kind": error.category,
        },
        status_code=status,
    )


__all__ = [
    "ProviderPreflightError",
    "provider_preflight_refusal",
    "provider_secret_env",
    "provider_secret_env_for_agent",
]
