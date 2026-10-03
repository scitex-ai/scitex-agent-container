"""Deliver declared accounts to Hermes' persistent pools, never to restart logic."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass, replace
from pathlib import Path

from .._logging import get_logger
from ..config._engine_types import apply_engine
from ..config._hermes_config import compile_hermes_config
from ._apptainer_provider import resolve_provider_api_key


@dataclass
class DeclaredPool:
    credentials: list[dict]
    suppressed_sources: list[str]


def resolve_primary_key(config, resolver):
    """An explicit account pool must not depend on an unpooled base alias."""
    names = config.hermes_failover.accounts.get(config.engine_key)
    if not names:
        return resolver(config)
    credential_config = deepcopy(config)
    credential_config.claude.provider.auth_token_env = names[0]
    return resolver(credential_config)


def spread_declared_accounts(agent: str, provider: str, rows: list[dict]) -> list[dict]:
    """Spread the initial native pool position using public identity only.

    Hermes owns subsequent rotation and model-scoped cooldowns. A restart must
    not clear those states or seed every fleet member at the same first key.
    """
    if len(rows) < 2:
        return rows
    offset = int.from_bytes(
        hashlib.sha256((agent + "\0" + provider).encode()).digest()[:8], "big"
    ) % len(rows)
    ordered = rows[offset:] + rows[:offset]
    return [{**row, "priority": priority} for priority, row in enumerate(ordered)]


def configure_failover(
    config, rendered: dict
) -> tuple[dict[str, str], dict[str, DeclaredPool]]:
    """Compile only declared routes; unresolved routes fail before profile writes."""
    policy = config.hermes_failover
    if not policy.accounts and not policy.engines:
        return {}, {}
    logger = get_logger(__name__)
    from ._hermes_profile import _launch_plan

    primary_plan = _launch_plan(
        config, launch_mode="tui" if config.runtime == "tui" else "headless"
    )
    keys = [config.engine_key, *policy.engines]
    if len(set(keys)) != len(keys):
        raise ValueError("Hermes failover must not repeat the primary engine")
    unused = set(policy.accounts) - set(keys)
    if unused:
        raise ValueError(
            f"Hermes failover accounts name unused engines: {', '.join(sorted(unused))}"
        )
    env: dict[str, str] = {}
    pools: dict[str, DeclaredPool] = {}
    for index, key in enumerate(keys):
        route = deepcopy(config)
        if index:
            if key not in config.engines:
                raise ValueError(f"Hermes failover engine {key!r} is not available")
            apply_engine(route, config.engines[key])
        if route.harness != "hermes" or route.claude.provider is None:
            raise ValueError(
                f"Hermes failover engine {key!r} cannot run in this Hermes session"
            )
        plan = replace(
            _launch_plan(route, launch_mode=primary_plan.launch_mode),
            session_id=primary_plan.session_id,
        )
        compiled = compile_hermes_config(plan, workdir=str(config.workdir))
        provider = compiled["model"]["provider"]
        if index:
            rendered["providers"].update(compiled["providers"])
            rendered["fallback_providers"].append(
                {"provider": provider, "model": route.model}
            )
        names = policy.accounts.get(key, [route.claude.provider.auth_token_env])
        base_url = next(iter(compiled["providers"].values()), {}).get("base_url")
        seen: set[str] = set()
        rows = []
        for name in names:
            credential_config = deepcopy(route)
            credential_config.claude.provider.auth_token_env = name
            token = resolve_provider_api_key(credential_config).strip()
            if not token or any(c in token for c in "\r\n"):
                raise ValueError(
                    f"Hermes failover credential {name} is empty or contains a newline"
                )
            if token in seen:
                logger.warning(
                    "Hermes failover engine %s: duplicate credential alias %s omitted",
                    key,
                    name,
                )
                continue
            seen.add(token)
            env[name] = token
            row = {
                "id": "sac-"
                + hashlib.sha256((provider + "\0" + token).encode()).hexdigest()[:20],
                "label": name,
                "source": "manual:sac:" + name,
                "auth_type": "api_key",
                "priority": len(rows),
                "access_token": token,
            }
            if base_url:
                row["base_url"] = base_url
            rows.append(row)
        if provider in pools:
            raise ValueError(
                f"Hermes failover repeats provider pool {provider!r}; use one account list"
            )
        auth_env = route.claude.provider.auth_token_env
        seeded_envs = {
            auth_env,
            *names,
            *(name for name in os.environ if name.startswith(auth_env + "_")),
        }
        suppressed = ["env:" + name for name in sorted(seeded_envs)]
        if compiled["providers"]:
            suppressed.extend(
                "config:" + p["name"] for p in compiled["providers"].values()
            )
            suppressed.append("model_config")
        pools[provider] = DeclaredPool(
            spread_declared_accounts(config.name, provider, rows), suppressed
        )
        # Custom routes resolve their key_env before attaching the pool. Native
        # routes use the pool directly; keep their unpooled base alias absent.
        if compiled["providers"]:
            env[route.claude.provider.auth_token_env] = rows[0]["access_token"]
    rendered["credential_pool_strategies"] = {
        provider: "round_robin" if len(pool.credentials) > 1 else "fill_first"
        for provider, pool in pools.items()
    }
    logger.info(
        "Hermes failover configured: %s; account labels: %s",
        " -> ".join(keys),
        "; ".join(
            f"{p}: {', '.join(r['label'] for r in pool.credentials)}"
            for p, pool in pools.items()
        ),
    )
    return env, pools


def materialize_pools(profile: Path, pools: dict[str, DeclaredPool]) -> None:
    """Preserve cooldowns for unchanged keys, and remove retired pool members."""
    if not pools:
        return
    path = profile / "auth.json"
    store = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(store, dict) or not isinstance(
        store.get("credential_pool", {}), dict
    ):
        raise ValueError(
            "Hermes auth.json has an invalid credential_pool; refusing to overwrite it"
        )
    existing = store.setdefault("credential_pool", {})
    suppressed = store.setdefault("suppressed_sources", {})
    if not isinstance(suppressed, dict):
        raise ValueError("Hermes auth.json has invalid suppressed_sources")
    store.setdefault("version", 1)
    for provider, pool in pools.items():
        rows = pool.credentials
        previous = existing.get(provider, [])
        if not isinstance(previous, list) or any(
            not isinstance(row, dict) for row in previous
        ):
            raise ValueError(f"Hermes credential pool {provider!r} is malformed")
        by_id = {row.get("id"): row for row in previous}

        def old_state(row):
            if row["id"] in by_id:
                return by_id[row["id"]]
            fingerprint = (
                "sha256:"
                + hashlib.sha256(row["access_token"].encode()).hexdigest()[:16]
            )
            matches = [
                old
                for old in previous
                if old.get("access_token") == row["access_token"]
                or old.get("secret_fingerprint") == fingerprint
            ]
            return max(
                matches, key=lambda old: old.get("last_status_at") or 0, default={}
            )

        existing[provider] = [{**old_state(row), **row} for row in rows]
        previous_suppressed = suppressed.get(provider, [])
        if not isinstance(previous_suppressed, list):
            raise ValueError(
                f"Hermes suppressed sources for {provider!r} are malformed"
            )
        suppressed[provider] = sorted(
            set(previous_suppressed) | set(pool.suppressed_sources)
        )
    fd, temporary = tempfile.mkstemp(prefix=".auth-", dir=profile)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(store, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
