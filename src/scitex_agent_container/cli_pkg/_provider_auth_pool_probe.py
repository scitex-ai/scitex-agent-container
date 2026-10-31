"""Validate declared pools through the same non-mutating lifecycle proof."""

from .._lifecycle._hermes_restart_preflight import prepare_hermes_successor
from .._lifecycle._restart_preflight import RestartPreflightAbort
from ..runtimes._apptainer_provider import ProviderEnvError
from ..runtimes._hermes_key_probe import probe_key
from ._provider_auth_probe import (
    OK,
    PROBE_FAILED,
    UNRESOLVED,
    ProviderAuthVerdict,
)


def probe_declared_provider_auth(config, *, timeout: float) -> ProviderAuthVerdict:
    """Prove inference and an invalid-key control without a duplicate request."""
    try:
        proof = prepare_hermes_successor(
            config,
            probe=lambda spec, token: probe_key(spec, token, timeout_s=timeout),
        )
    except RestartPreflightAbort as exc:
        return ProviderAuthVerdict(PROBE_FAILED, str(exc))
    except (ProviderEnvError, ValueError):
        return ProviderAuthVerdict(
            UNRESOLVED,
            "The declared Hermes provider policy or credential pool is invalid; "
            "check the engine definitions and declared credential slot names.",
        )
    provider = proof.selection["model"]["provider"]
    labels = [
        row["label"]
        for row in proof.pools[provider].credentials
        if row.get("last_status") == "ok"
    ]
    return ProviderAuthVerdict(
        OK,
        f"Declared provider {provider!r} verified inference and rejected an "
        f"invalid credential control; usable slots: {', '.join(labels)}.",
        200,
        proof.selection["model"]["default"],
    )
