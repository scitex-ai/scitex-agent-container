"""Probe declared routes before launch without sending conversation content."""

from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

from .._logging import get_logger

logger = get_logger(__name__)
_ERROR_FIELDS = (
    "last_status",
    "last_status_at",
    "last_error_code",
    "last_error_reason",
    "last_error_message",
    "last_error_reset_at",
    "failure_reason",
)


def probe_spec(plan, compiled: dict) -> dict:
    protocol, url = plan.endpoint.protocol, plan.endpoint.url
    headers = {
        "User-Agent": "scitex-agent-container/availability-probe",
        "X-SciTeX-Agent-ID": plan.agent_name,
        "X-SciTeX-Session-ID": plan.session_id,
    }
    custom = next(iter(compiled["providers"].values()), {})
    headers.update(custom.get("extra_headers", {}))
    provider = compiled["model"]["provider"]
    if protocol.startswith("hermes-native:"):
        from ._availability import provider_route

        provider = protocol.split(":", 1)[1]
        route = provider_route(provider, plan.engine.model_id)
        protocol, url = route.protocol, route.endpoint_url
    return {
        "url": url,
        "protocol": protocol,
        "model": plan.engine.model_id,
        "provider": provider,
        "session_id": plan.session_id,
        "headers": headers,
        "model_config": compiled["model"],
    }


def probe_key(spec: dict, token: str, *, timeout_s: float = 20) -> dict:
    """Adapt the local availability result to Hermes' persistent pool state."""
    from ._availability import probe_provider_key

    result = probe_provider_key(
        spec["provider"],
        spec["model"],
        token,
        endpoint_url=spec["url"],
        protocol=spec["protocol"],
        extra_headers=spec["headers"],
        session_id=spec["session_id"],
        timeout_s=timeout_s,
    )
    now = time.time()
    status = result.status
    logger.info(
        "Provider availability: provider=%s model=%s available=%s status=%s code=%s",
        spec["provider"],
        spec["model"],
        result.available,
        status.kind,
        status.code,
    )
    if result.available is True:
        return {"last_status": "ok", "last_status_at": now}
    if result.available is None:
        # Unknown is absence of evidence: skip temporarily, never reject.
        return {
            "last_status": "exhausted",
            "last_status_at": now,
            "last_error_code": None,
            "last_error_reason": status.kind,
            "last_error_message": result.check.detail,
            "last_error_reset_at": now + 60,
            "failure_reason": "transient",
        }
    code = status.code if status.kind == "http" else None
    detail = result.check.detail.lower()
    billing = code == 402 or any(
        word in detail
        for word in ("insufficient credits", "credits exhausted", "out of credits")
    )
    reason = (
        "auth"
        if code == 401
        else "billing"
        if billing
        else ("rate_limit" if code == 429 else "transient")
    )
    return {
        "last_status": "dead" if code == 401 else "exhausted",
        "last_status_at": now,
        "last_error_code": code,
        "last_error_reason": status.kind + ":" + str(status.code),
        "last_error_reset_at": result.reset_at
        or now + (3600 if billing or code in (403, 429) else 60),
        "failure_reason": reason,
    }


def preflight_pools(rendered: dict, pools: dict, profiles: list[Path]) -> None:
    """Select the first proven route, preserving rejected keys and quota resets."""
    if not pools:
        return
    from ._hermes_failover import materialize_pools

    stores = []
    for profile in profiles:
        profile.mkdir(parents=True, exist_ok=True)
        materialize_pools(profile, pools)
        stores.append(
            json.loads((profile / "auth.json").read_text())["credential_pool"]
        )
    now = time.time()
    verified = None
    updated = {}
    routes = [rendered["model"], *rendered["fallback_providers"]]
    for route in routes:
        provider = route["provider"]
        pool = pools[provider]
        rows = []
        healthy = False
        for declared in pool.credentials:
            previous = [
                row
                for store in stores
                for row in store.get(provider, [])
                if row.get("id") == declared["id"]
            ]
            row = {
                **max(
                    previous,
                    key=lambda item: item.get("last_status_at") or 0,
                    default={},
                ),
                **declared,
            }
            reset = row.get("last_error_reset_at")
            if not isinstance(reset, (int, float)):
                reset = (row.get("last_status_at") or 0) + 3600
            skip = row.get("last_status") == "dead" or (
                row.get("last_status") == "exhausted" and reset > now
            )
            if not skip:
                result = probe_key(pool.probe, row["access_token"])
                for key in _ERROR_FIELDS:
                    row.pop(key, None)
                row.update(result)
                healthy = healthy or row["last_status"] == "ok"
                logger.info(
                    "Availability probe %s / %s: %s (HTTP %s)",
                    provider,
                    row["label"],
                    row["last_status"],
                    row.get("last_error_code", 200),
                )
            elif skip:
                logger.info(
                    "Availability probe skips %s / %s: %s (HTTP %s)",
                    provider,
                    row["label"],
                    row["last_status"],
                    row.get("last_error_code"),
                )
            rows.append(row)
        updated[provider] = replace(pool, credentials=rows)
        if healthy and verified is None:
            verified = provider
    # Write both home backings from the same observed health state.
    for profile in profiles:
        materialize_pools(profile, updated)
    if verified is None:
        raise RuntimeError(
            "No declared Hermes account passed its availability probe; "
            "rejected keys remain configured and quota resets are preserved"
        )
    if verified != rendered["model"]["provider"]:
        logger.warning(
            "Primary accounts unavailable; launching with verified provider %s",
            verified,
        )
        rendered["model"] = dict(pools[verified].probe["model_config"])
        rendered["fallback_providers"] = [
            {
                "provider": route["provider"],
                "model": pools[route["provider"]].probe["model"],
            }
            for route in routes
            if route["provider"] != verified
        ]
    pools.update(updated)
