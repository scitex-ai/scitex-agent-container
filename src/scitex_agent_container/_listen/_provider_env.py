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
from ..config._provider_preflight_proof import (
    PROVIDER_PREFLIGHT_PROOF_ENV,
    absent_provider_preflight_proof,
    provider_preflight_proof,
)
from ..config._provider_secret_registry import REGISTERED_PROVIDER_SECRET_NAMES
from ..config._qwen_gateway import (
    QwenGatewayTokenEnvError,
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

# The listener is a privilege boundary, not a general-purpose environment
# forwarder. Carry only operational context required by the child ``sac``
# control process; provider credentials are added separately after exact tuple
# authorization. This positive policy excludes the unbounded family of
# interpreter/loader/tool hooks (BASH_ENV, PYTHONUSERBASE, LD_PRELOAD,
# JAVA_TOOL_OPTIONS, SSH_ASKPASS, ...).
_SAFE_CHILD_ENV_NAMES = frozenset(
    {
        "HOME",
        "PATH",
        "PWD",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "TERM",
        "TMPDIR",
        "TZ",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "SSH_AUTH_SOCK",
        "CUDA_VISIBLE_DEVICES",
        "ROCR_VISIBLE_DEVICES",
        "SAC_SECRETS_ENVRC",
        "SAC_ENGINES_FILE",
        "SAC_QWEN_GATEWAY_URL",
        "SAC_QWEN_GATEWAY_TOKEN_ENV",
        "SAC_ENGINE_PROBE",
        "SAC_ENGINE_MAX_CONTEXT_TOKENS",
        "SAC_ENGINE_REASONING_EFFORT",
        "SAC_ENGINE",
        "SAC_NAME",
        "SAC_INSTANCE_UUID",
        "SAC_BUILD_NO_NICE",
        "SAC_EVENT_LOG",
        "SCITEX_AGENT_CONTAINER_YAML_DIRS",
        "SCITEX_AGENT_CONTAINER_CONFIG",
        "SCITEX_AGENT_CONTAINER_RUNTIME_DIR",
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
        "SCITEX_AGENT_CONTAINER_MODEL",
        "SCITEX_DIR",
        "SCITEX_STORE_DSN",
        "SCITEX_CARDS_DB",
        "PGPASSFILE",
        "PGHOST",
        "PGPORT",
        "PGDATABASE",
    }
)
_SAFE_CHILD_ENV_PREFIXES = ("XDG_", "LC_", "SLURM_")


def _authorized_provider_secrets() -> frozenset[tuple[str, str, str]]:
    """Exact provider tuples authorized by host policy at call time."""
    try:
        qwen_token_env = qwen_gateway_token_env()
    except QwenGatewayTokenEnvError as exc:
        raise ProviderPreflightError("qwen_token_env_unregistered") from exc
    return _STATIC_AUTHORIZED_PROVIDER_SECRETS | {
        ("qwen38-27b", qwen_gateway_url(), qwen_token_env)
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


def _operational_child_env(child_env: Mapping[str, str]) -> dict[str, str]:
    """Return positive-policy control-plane env without provider credentials."""
    return {
        str(name): str(value)
        for name, value in child_env.items()
        if (
            name in _SAFE_CHILD_ENV_NAMES
            or name.startswith(_SAFE_CHILD_ENV_PREFIXES)
        )
        and name not in REGISTERED_PROVIDER_SECRET_NAMES
        and name != PROVIDER_PREFLIGHT_PROOF_ENV
    }


def provider_child_env(
    config: AgentConfig,
    child_env: Mapping[str, str],
    *,
    pool: PoolRead | None = None,
) -> dict[str, str]:
    """Build broker child env from positive policy plus one selected secret."""
    prepared = _operational_child_env(child_env)
    prepared.update(provider_secret_env(config, child_env, pool=pool))
    prepared[PROVIDER_PREFLIGHT_PROOF_ENV] = provider_preflight_proof(config)
    return prepared


def provider_secret_values(child_env: Mapping[str, str]) -> tuple[str, ...]:
    """Return selected provider values only, longest first, for redaction."""
    values = {
        str(child_env.get(name) or "")
        for name in REGISTERED_PROVIDER_SECRET_NAMES
    }.difference({""})
    return tuple(sorted(values, key=len, reverse=True))


def redact_provider_secrets(text: str | None, child_env: Mapping[str, str]) -> str:
    """Remove propagated provider values from child output before persistence."""
    redacted = str(text or "")
    for value in provider_secret_values(child_env):
        redacted = redacted.replace(value, "[REDACTED]")
    return redacted


def provider_child_env_for_agent(
    name: str,
    child_env: Mapping[str, str],
) -> dict[str, str]:
    """Resolve ``name`` and return its complete preflighted child environment.

    A lookup/load failure carries an ABSENT proof and no provider credential.
    If a spec is created or repaired before the child load, the child observes
    a proof mismatch and refuses before lifecycle side effects.
    """
    try:
        config = load_config(resolve_with_prefix(name))
    except QwenGatewayTokenEnvError as exc:
        raise ProviderPreflightError("qwen_token_env_unregistered") from exc
    except Exception:  # stx-allow: fallback (an absent/invalid spec is bound as ABSENT; the child retains the canonical diagnostic if it remains unreadable)
        prepared = _operational_child_env(child_env)
        prepared[PROVIDER_PREFLIGHT_PROOF_ENV] = absent_provider_preflight_proof(name)
        return prepared
    return provider_child_env(config, child_env)


def provider_secret_env_for_agent(
    name: str,
    child_env: Mapping[str, str],
) -> dict[str, str]:
    """Compatibility alias for the now-complete broker child environment."""
    return provider_child_env_for_agent(name, child_env)


def provider_preflight_refusal(
    name: str, error: ProviderPreflightError
) -> JSONResponse:
    """Render a safe refusal containing category only, never secret material."""
    status = (
        403
        if error.category
        in {"provider_tuple_unauthorized", "qwen_token_env_unregistered"}
        else 412
    )
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
    "provider_child_env",
    "provider_child_env_for_agent",
    "provider_preflight_refusal",
    "provider_secret_values",
    "redact_provider_secrets",
    "provider_secret_env",
    "provider_secret_env_for_agent",
]
