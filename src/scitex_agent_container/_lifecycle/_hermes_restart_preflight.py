"""Verify a Hermes successor in memory before replacing its live process."""

from __future__ import annotations

import json
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

import scitex_logging as slogging

logger = slogging.getLogger(__name__)
_CACHE_TTL_S = 90.0
_CACHE = {}
_CACHE_LOCK = threading.RLock()
_CONTROL_TOKEN = "sac-preflight-control-not-a-valid-key"
_ERROR_FIELDS = (
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
    "failure_reason",
)


@dataclass(repr=False)
class PreparedHermesRoute:
    """Transient successor proof; credentials never appear in its repr."""

    plan: object = field(repr=False)
    selection: dict = field(repr=False)
    env: dict = field(repr=False)
    pools: dict = field(repr=False)
    provider_key: str = field(repr=False)
    binding: tuple = field(repr=False)


def _binding(config):
    return (
        config.name,
        str(config.config_path),
        config.engine_key,
        config.model,
        config.runtime,
        config.harness,
        config.claude.session,
        deepcopy(config.claude.provider),
        deepcopy(config.engines),
        # ``hermes_goals`` exists only where the generalized goal spec has
        # landed; older trees pin the judge inline. Absence is a stable
        # binding value either way.
        deepcopy(getattr(config, "hermes_goals", None)),
        _source_stamp(config.config_path),
    )


def _source_stamp(path):
    if not path:
        return None
    stat = Path(path).stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def assert_prepared_source_current(prepared):
    """Refuse a changed authored spec while the old process is still alive."""
    if (
        prepared is not None
        and _source_stamp(prepared.binding[1]) != prepared.binding[-1]
    ):
        from ._restart_preflight import RestartPreflightAbort

        raise RestartPreflightAbort(
            "Hermes successor spec changed during credential preflight; "
            "the current process has been left running"
        )


def attach_prepared_route(config, prepared):
    """Carry the exact verified successor into its same-process start leg."""
    if prepared is None:
        return
    if prepared.binding != _binding(config):
        from ._restart_preflight import RestartPreflightAbort

        raise RestartPreflightAbort(
            "Hermes successor configuration changed after credential preflight; "
            "refusing to consume a proof for a different route"
        )
    config._hermes_prepared_route = prepared


def consume_prepared_route(config, *, launch_mode):
    """Return a matching proof once; ordinary starts retain their own preflight."""
    prepared = getattr(config, "_hermes_prepared_route", None)
    if prepared is None:
        return None
    if prepared.binding != _binding(config) or prepared.plan.launch_mode != launch_mode:
        from ._restart_preflight import RestartPreflightAbort

        raise RestartPreflightAbort(
            "Hermes prepared successor route no longer matches the launch"
        )
    del config._hermes_prepared_route
    return prepared


def same_goal_model(left, right):
    """Recognize only the established Muse/CommandCode wire-name alias."""
    if left == right:
        return True
    bare = str(left).removeprefix("meta/")
    return bare.startswith("muse-spark-") and {left, right} == {bare, "meta/" + bare}


def _read_previous(profiles):
    """Read cooldowns without creating or changing a running agent profile."""
    stores = []
    for profile in profiles:
        path = profile / "auth.json"
        if not path.is_file():
            continue
        store = json.loads(path.read_text())
        pools = store.get("credential_pool", {}) if isinstance(store, dict) else None
        if not isinstance(pools, dict):
            raise ValueError("Hermes existing credential pool is malformed")
        for rows in pools.values():
            if not isinstance(rows, list) or any(
                not isinstance(row, dict) for row in rows
            ):
                raise ValueError("Hermes existing credential pool rows are malformed")
        stores.append(pools)
    return stores


def _cached_probe(spec, token, probe, now):
    # Public vendor endpoints ignore SAC's local identity headers. Preserve
    # every header for arbitrary gateways where identity can affect admission.
    public = urlsplit(spec["url"]).hostname in {"opencode.ai", "api.commandcode.ai"}
    headers = tuple(
        sorted(
            (name, value)
            for name, value in spec.get("headers", {}).items()
            if not public
            or name.lower()
            not in {"x-scitex-agent-id", "x-scitex-session-id", "x-opencode-session"}
        )
    )
    identity = (spec["url"], spec["protocol"], spec["model"], headers, token)
    with _CACHE_LOCK:
        stamp = now()
        for key, (observed_at, _) in tuple(_CACHE.items()):
            if stamp - observed_at >= _CACHE_TTL_S:
                del _CACHE[key]
        cached = _CACHE.get(identity)
        if cached is not None and stamp - cached[0] < _CACHE_TTL_S:
            return deepcopy(cached[1])
        try:
            result = probe(spec, token)
        except Exception:
            # Upstream exception strings can echo an Authorization header.
            # Cache only bounded metadata so one broken secondary route does
            # not repeatedly block each agent in a bulk operation.
            result = _unavailable(stamp, "probe_unavailable")
        if not isinstance(result, dict) or result.get("last_status") not in {
            "ok",
            "dead",
            "exhausted",
        }:
            result = _unavailable(stamp, "probe_unavailable")
        _CACHE[identity] = (now(), deepcopy(result))
        return result


def _unavailable(now, reason):
    return {
        "last_status": "exhausted",
        "last_status_at": now,
        "last_error_code": None,
        "last_error_reason": reason,
        "last_error_reset_at": now + 60,
        "failure_reason": reason,
    }


def _previous_row(declared, previous, provider):
    old = [
        row
        for store in previous
        for row in store.get(provider, ())
        if row.get("id") == declared["id"]
        and row.get("access_token") == declared["access_token"]
    ]
    return {
        **max(old, key=lambda item: item.get("last_status_at") or 0, default={}),
        **declared,
    }


def _control_unavailable_row(declared, previous, provider, now):
    row = _previous_row(declared, previous, provider)
    if row.get("last_status") not in {"dead", "exhausted"}:
        row.update(_unavailable(now, "control_unverified"))
    return row


def prepare_hermes_successor(
    config, *, probe=None, resolver=None, pool_reader=None, profiles=None, now=time.time
):
    """Prove one declared route; never write profiles, stop, or reinstall."""
    if config.harness != "hermes":
        return None
    from dataclasses import replace

    from ..config._hermes_config import compile_hermes_config
    from ..runtimes._apptainer_provider import (
        ProviderEnvError,
        resolve_provider_api_key,
    )
    from ..runtimes._hermes_failover import configure_failover
    from ..runtimes._hermes_key_probe import probe_key, probe_spec
    from ..runtimes._hermes_profile import _launch_plan
    from ..runtimes._secret_pool import read_pool
    from ..runtimes._to_home_overlay import resolve_overlay_upper_home
    from ..runtimes.tui_session import state_dir_for_config
    from ._restart_preflight import RestartPreflightAbort

    probe = probe or probe_key
    resolver = resolver or resolve_provider_api_key
    pool_reader = pool_reader or read_pool
    canonical = None

    def resolve(route):
        nonlocal canonical
        try:
            return resolver(route)
        except ProviderEnvError:
            if canonical is None:
                canonical = pool_reader()
            value = canonical.env.get(route.claude.provider.auth_token_env, "")
            if value:
                return value
            raise

    successor = deepcopy(config)
    if not successor.hermes_failover.accounts and not successor.hermes_failover.engines:
        provider = successor.claude.provider
        if provider is None:
            raise RestartPreflightAbort("Hermes successor has no resolved provider")
        successor.hermes_failover.accounts = {
            successor.engine_key: [provider.auth_token_env]
        }
    mode = "tui" if successor.runtime == "tui" else "headless"
    plan = _launch_plan(successor, launch_mode=mode)
    selection = compile_hermes_config(plan, workdir=str(successor.workdir))
    env, pools = configure_failover(
        successor, selection, credential_resolver=resolve, allow_unresolved_routes=True
    )
    if profiles is None:
        state_dir = state_dir_for_config(successor)
        profiles = [state_dir / "home" / ".hermes"]
        upper = resolve_overlay_upper_home(successor)
        if upper is not None:
            profiles.append(upper / ".hermes")
    previous = _read_previous(profiles)
    routes = [
        route
        for route in [selection["model"], *selection["fallback_providers"]]
        if route["provider"] in pools
    ]
    selection["fallback_providers"] = [
        route for route in selection["fallback_providers"] if route["provider"] in pools
    ]
    verified = None
    healthy_routes = set()
    for route in routes:
        provider = route["provider"]
        pool = pools[provider]
        spec = pool.probe or probe_spec(plan, selection)
        control = _cached_probe(spec, _CONTROL_TOKEN, probe, now)
        if control.get("last_error_code") not in {401, 403}:
            pools[provider] = replace(
                pool,
                credentials=[
                    _control_unavailable_row(row, previous, provider, now())
                    for row in pool.credentials
                ],
            )
            logger.warning(
                "Hermes provider %s is unavailable: invalid credential control "
                "was not verified; another declared route must prove access",
                provider,
            )
            continue
        rows = []
        healthy = False
        for declared in pool.credentials:
            row = _previous_row(declared, previous, provider)
            reset = row.get("last_error_reset_at")
            if not isinstance(reset, (int, float)):
                reset = (row.get("last_status_at") or 0) + 3600
            skip = row.get("last_status") == "dead" or (
                row.get("last_status") == "exhausted" and reset > now()
            )
            if not skip:
                result = _cached_probe(spec, row["access_token"], probe, now)
                for key in _ERROR_FIELDS:
                    row.pop(key, None)
                row.update(result)
            healthy |= row.get("last_status") == "ok"
            rows.append(row)
        pools[provider] = replace(pool, credentials=rows)
        if healthy:
            healthy_routes.add(provider)
        if healthy and verified is None:
            verified = provider
    if verified is None:
        raise RestartPreflightAbort(
            "No declared Hermes credential currently proves inference access; "
            "invalid credential controls and unavailable routes cannot prove access. "
            "the current process and its profile have been left running unchanged"
        )
    if verified != selection["model"]["provider"]:
        next_model = pools[verified].probe["model"]
        if getattr(
            getattr(config, "hermes_goals", None), "judge_engine", ""
        ) and not same_goal_model(config.model, next_model):
            raise RestartPreflightAbort(
                "Hermes fallback would change the authored goal judge model; "
                "the current process has been left running"
            )
        selection["model"] = dict(pools[verified].probe["model_config"])
        selection["fallback_providers"] = [
            {
                "provider": route["provider"],
                "model": pools[route["provider"]].probe["model"],
            }
            for route in routes
            if route["provider"] != verified and route["provider"] in healthy_routes
        ]
    else:
        selection["fallback_providers"] = [
            route
            for route in selection["fallback_providers"]
            if route["provider"] in healthy_routes
        ]
    selection["sac_managed_model"] = True
    provider_key = next(
        row["access_token"]
        for row in pools[verified].credentials
        if row.get("last_status") == "ok"
    )
    logger.info(
        "Hermes successor preflight verified provider %s; "
        "profile and environment will be reused",
        verified,
    )
    return PreparedHermesRoute(
        plan, selection, env, pools, provider_key, _binding(config)
    )


def reset_probe_cache():
    """Drop short-lived in-process health observations for isolated tests."""
    with _CACHE_LOCK:
        _CACHE.clear()
