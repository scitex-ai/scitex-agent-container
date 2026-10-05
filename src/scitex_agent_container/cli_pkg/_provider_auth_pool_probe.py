"""Validate the declared cascade without changing live credential state."""

from copy import deepcopy

from scitex_genai.availability import probe_provider_key

from ..config._engine_types import apply_engine
from ..config._hermes_config import compile_hermes_config
from ..config._hermes_failover import HermesFailoverSpec
from ..runtimes._apptainer_provider import ProviderEnvError
from ..runtimes._hermes_failover import configure_failover
from ..runtimes._hermes_profile import _launch_plan
from ._provider_auth_probe import (
    OK,
    PROBE_FAILED,
    UNREACHABLE,
    UNRESOLVED,
    ProviderAuthVerdict,
    probe_provider_auth,
)


def probe_declared_provider_auth(config, *, timeout: float) -> ProviderAuthVerdict:
    """Try only declared keys, retaining the strict authentication control."""
    plan = _launch_plan(config, launch_mode="tui")
    rendered = compile_hermes_config(plan, workdir=str(config.workdir))
    try:
        _, pools = configure_failover(config, rendered)
    except (ProviderEnvError, ValueError) as exc:
        return ProviderAuthVerdict(UNRESOLVED, str(exc))

    attempts = []
    unknown = False
    failure = None
    engines = [config.engine_key, *config.hermes_failover.engines]
    for engine, pool in zip(engines, pools.values()):
        route = deepcopy(config)
        if engine != config.engine_key:
            apply_engine(route, config.engines[engine])
        route.hermes_failover = HermesFailoverSpec()
        spec = pool.probe
        for row in pool.credentials:
            result = probe_provider_key(
                spec["provider"],
                spec["model"],
                row["access_token"],
                endpoint_url=spec["url"],
                protocol=spec["protocol"],
                extra_headers=spec["headers"],
                session_id=spec["session_id"],
                timeout_s=timeout,
            )
            attempts.append(
                f"{row['label']}: {result.status.kind}:{result.status.code}"
            )
            if result.available is not True:
                unknown = unknown or result.available is None
                continue
            route.claude.provider.auth_token_env = row["label"]
            route.claude.provider.base_url = spec["url"]
            route.claude.provider.extra_headers = dict(spec["headers"])
            verdict = probe_provider_auth(route, timeout=timeout)
            if verdict.state == OK:
                return ProviderAuthVerdict(
                    OK,
                    f"Declared {engine} / {row['label']} verified. " + verdict.detail,
                    verdict.status_code,
                    verdict.actual_model,
                )
            unknown = unknown or verdict.state == UNREACHABLE
            failure = verdict

    detail = "No declared account verified; " + "; ".join(attempts)
    if unknown:
        return ProviderAuthVerdict(UNREACHABLE, detail + "; an outcome remains unknown")
    return ProviderAuthVerdict(
        failure.state if failure is not None else PROBE_FAILED,
        detail + ("; " + failure.detail if failure is not None else ""),
        failure.status_code if failure is not None else None,
        failure.actual_model if failure is not None else None,
    )
