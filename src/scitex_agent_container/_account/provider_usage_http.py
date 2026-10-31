"""Fixed read-only provider metadata routes, with no renewal, retry or redirect.

CommandCode routes/units: official1.58.0 CLI (aab2bec800371953…).
Go route: reviewed opencode-zen provider (829502d68fde46e2…).
Codex route: existing fleet read-only usage reader. Subscription quota does
not establish billed costs. Parent process enforces the absolute deadline.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from .provider_usage_projection import project

_URLS = {
    "openai": (("quota", "https://chatgpt.com/backend-api/wham/usage"),),
    "opencode-go": (("quota", "https://opencode.ai/zen/go/v1/usage"),),
    "commandcode": (
        ("credits", "https://api.commandcode.ai/alpha/billing/credits"),
        ("cost", "https://api.commandcode.ai/alpha/usage/summary"),
    ),
}
_MAX_BODY = 1_048_576


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _codex_headers(path):
    """Read the explicitly selected existing login without copying or refreshing."""
    if path is None:
        return None, "subscription-auth-unavailable"
    try:
        data = json.loads(path.read_text())
        tokens = data.get("tokens") if isinstance(data, dict) else None
        if not isinstance(tokens, dict):
            return None, "subscription-auth-unavailable"
        access, account = tokens.get("access_token"), tokens.get("account_id")
        if not isinstance(access, str) or not access or not isinstance(account, str) or not account:
            return None, "subscription-auth-unavailable"
        parts = access.split(".")
        if len(parts) == 3:
            claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
            expiry = claims.get("exp") if isinstance(claims, dict) else None
            if type(expiry) in (int, float) and expiry <= time.time():
                return None, "credential-expired-refresh-not-performed"
        return {"Authorization": "Bearer " + access, "ChatGPT-Account-Id": account}, None
    except (OSError, ValueError, TypeError):
        return None, "subscription-auth-unreadable"


def _read_json(response, deadline):
    """Bound retained bytes and check the same deadline between body reads."""
    chunks, size = [], 0
    while size <= _MAX_BODY:
        if time.monotonic() >= deadline:
            raise TimeoutError
        reader = getattr(response, "read1", response.read)
        chunk = reader(min(65_536, _MAX_BODY + 1 - size))
        if not chunk:
            raw = json.loads(b"".join(chunks))
            if not isinstance(raw, dict):
                raise ValueError
            return raw
        size += len(chunk)
        chunks.append(chunk)
    raise ValueError


def fetch(target, deadline, *, opener=None, now=None):
    """Query one owned account; safe partial metrics survive another route failing."""
    observed = now or datetime.now(timezone.utc)
    if target.provider == "openai":
        headers, error = _codex_headers(target.auth_path)
        if error:
            return {"error": error}
    else:
        headers = {"Authorization": "Bearer " + target.secret}
    headers.update(Accept="application/json", **{"User-Agent": "SciTeX-account-usage"})
    open_request = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _NoRedirect(),
    ).open
    payloads, errors = {}, []
    for name, base in _URLS[target.provider]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            errors.append("metadata-deadline")
            break
        url = base
        if name == "cost":
            since = (observed - timedelta(days=30)).isoformat()
            payloads["cost_since"] = since
            url += "?" + urllib.parse.urlencode({"since": since})
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with open_request(request, timeout=remaining) as response:
                payloads[name] = _read_json(response, deadline)
        except urllib.error.HTTPError as error:
            # 403 and transport refusal establish no credential-invalid verdict.
            errors.append("authentication-refused" if error.code == 401 else "http-" + str(error.code))
            error.close()
        except (OSError, ValueError, TimeoutError, urllib.error.URLError):
            errors.append("metadata-unavailable")
    out = project(target.provider, payloads, observed)
    out["error"] = errors[0] if errors else None
    return out
