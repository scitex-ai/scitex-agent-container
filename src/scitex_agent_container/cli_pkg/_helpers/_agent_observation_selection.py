"""Selected-account metadata; secrets and credential locators never leave RAM."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from pathlib import Path

from ._agent_observation_io import mount_source, read_bytes, read_json

_PLANS = {"free", "plus", "pro", "team", "business", "enterprise", "edu"}
_ALIAS = re.compile(r"(?:OPENCODE_GO|COMMANDCODE)_API_KEY(?:_[A-Za-z0-9]+)?\Z")


def cached_usage(snapshot, *, home=None, now=None):
    """Passive declared-alias usage, explicitly weaker than live capacity."""
    from datetime import datetime, timezone

    from ..._account.provider_usage_inventory import _cached

    provider = (
        snapshot.get("provider") or (snapshot.get("claude") or {}).get("provider") or {}
    )
    alias = provider.get("auth_token_env") if isinstance(provider, dict) else None
    if not isinstance(alias, str) or not _ALIAS.fullmatch(alias):
        return {"state": "unknown"}
    name = "opencode-go" if alias.startswith("OPENCODE_GO") else "commandcode"
    root = Path.home() if home is None else Path(home)
    cached = _cached(
        root / ".scitex/cache/account-usage" / f"{name}-{alias}.json",
        now or datetime.now(timezone.utc),
    )
    if cached is None:
        return {"state": "unknown"}
    return {
        "state": "cache-only",
        "binding": "declared-alias",
        "live_capacity": "unknown",
        **cached,
    }


def _group(value, salt, provider):
    return (
        provider + ":" + hmac.new(salt, value.encode(), hashlib.sha256).hexdigest()[:16]
    )


def subscription_metadata(payload, salt):
    """Project only plan and an opaque observation-scoped account group."""
    token = (payload.get("tokens") or {}).get("id_token")
    if not isinstance(token, str):
        raise ValueError("subscription-metadata-unknown")
    encoded = token.split(".")
    if len(encoded) != 3:
        raise ValueError("subscription-metadata-unknown")
    claims = json.loads(
        base64.urlsafe_b64decode(encoded[1] + "=" * (-len(encoded[1]) % 4))
    )
    auth = claims.get("https://api.openai.com/auth") or {}
    account = auth.get("chatgpt_account_id")
    if not isinstance(account, str) or not account:
        raise ValueError("subscription-account-unknown")
    plan = str(auth.get("chatgpt_plan_type") or "").lower()
    return {
        "provider": "openai",
        "authentication": "subscription",
        "account_group": _group(account, salt, "openai"),
        "plan": plan if plan in _PLANS else "unknown",
        "source": "selected-auth-metadata",
        "verification": "metadata-only",
    }


def selected_account(snapshot, process, environment, salt):
    """Read the selected process, never the observer's default credentials."""
    harness = str(snapshot.get("harness") or "").lower()
    if harness == "codex":
        home = environment.get("CODEX_HOME")
        if not home or not Path(home).is_absolute():
            raise ValueError("selected-auth-metadata-unknown")
        mounts = read_bytes(process / "mountinfo", 2 * 1024 * 1024).decode()
        host_mounts = read_bytes(Path("/proc/self/mountinfo"), 2 * 1024 * 1024).decode()
        bound = mount_source(mounts, home, host_mountinfo=host_mounts)
        payload = read_json(bound / "auth.json", private=True)
        result = codex_auth_selection(payload, environment, salt)
        if read_bytes(process / "mountinfo", 2 * 1024 * 1024).decode() != mounts:
            raise ValueError("selected-bind-changed")
        return result
    policy = snapshot.get("hermes_failover") or {}
    if harness == "hermes" and (
        not isinstance(policy, dict) or policy.get("accounts") or policy.get("engines")
    ):
        # A launch-time alias does not prove which persistent pool route is active.
        raise ValueError("active-pool-selection-unavailable")
    provider = (
        snapshot.get("provider") or (snapshot.get("claude") or {}).get("provider") or {}
    )
    alias = provider.get("auth_token_env") if isinstance(provider, dict) else None
    if harness == "hermes" and isinstance(alias, str) and _ALIAS.fullmatch(alias):
        secret = environment.get(alias)
        if not secret:
            raise ValueError("selected-key-unknown")
        name = "opencode-go" if alias.startswith("OPENCODE_GO") else "commandcode"
        return {
            "provider": name,
            "authentication": "api-key",
            "account_group": _group(secret, salt, name),
            "plan": "unknown",
            "source": "selected-process-environment",
        }
    raise ValueError("selected-account-unknown")


def codex_auth_selection(payload, environment, salt):
    """An ambient API key is not proof that a selected ChatGPT account uses it."""
    mode = payload.get("auth_mode")
    if mode == "chatgpt" or (
        mode is None and payload.get("tokens") and not payload.get("OPENAI_API_KEY")
    ):
        return subscription_metadata(payload, salt)
    key = payload.get("OPENAI_API_KEY")
    if mode not in {None, "apikey"} or not isinstance(key, str) or not key:
        raise ValueError("selected-auth-mode-unknown")
    ambient = environment.get("OPENAI_API_KEY")
    if ambient and ambient != key:
        raise ValueError("selected-auth-mode-ambiguous")
    return {
        "provider": "openai",
        "authentication": "api-key",
        "account_group": _group(key, salt, "openai"),
        "plan": "unknown",
        "source": "selected-auth-metadata",
        "verification": "metadata-only",
    }
