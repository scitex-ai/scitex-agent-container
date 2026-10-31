"""Fleet-wide API-key liveness sweep, independent of TUI and Hermes cron.

Operator order 2026-10-16: TUI pattern detection is unstable and does not
scale to new providers. This module answers two questions the existing
machinery leaves open:

* Which key slots does each agent consume? (the key -> agent map, read
  from the declared Hermes failover pools in every agent spec)
* Is each slot alive right now? (asked of the provider backend, answered
  in four values — never two)

Verdicts are four-valued on purpose. The 2026-10-15 infra-lead stop is
the reason: commandcode answered HTTP 400 ``insufficient credits`` for a
perfectly VALID key with a zero balance. A two-valued probe (up / down)
cannot distinguish "the key is dead, rotate it" from "the key is valid
but the balance is empty, top it up" — and the old code lumped both into
"no signal", which is how a billing event became a dead session with no
failover. The four verdicts:

* ``alive``      — the backend authenticated the key and reports balance.
* ``exhausted``  — the key authenticates but the allowance is empty
  (HTTP 400 ``insufficient credits`` / zero credit balance). Needs
  another account or a top-up, never a timed retry.
* ``dead``       — the backend authoritatively rejects the key
  (401 / 403). Needs rotation, never a retry.
* ``unknown``    — timeout, 5xx, transport failure. Absence of evidence:
  never takes a slot out of service.
* ``unresolved`` — the slot names an env var with no value on this host.
  A deployment fact, not a provider verdict.

Only the backend can settle whether a well-formed string is a working
key (see ``cli_pkg/_provider_auth_probe.py``), so the sweep asks it with
one cheap read-only request per slot — the billing/credits endpoints the
providers already expose, never a conversation turn. No renewal, no
retry, no redirect, bounded bodies and deadlines, mirroring
:mod:`._account.provider_usage_http`'s discipline.

Pure where it matters: :func:`collect_slots`,
:func:`classify_status` and :func:`sweep` take all inputs as arguments so
tests pin the verdicts with literal provider responses. The CLI verb
(``sac accounts probe-api-keys``) owns env access, the clock, the HTTP
calls and the cache write.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

__all__ = [
    "CACHE_FILENAME",
    "KEY_ALIAS_RE",
    "LIVENESS_URLS",
    "classify_status",
    "collect_slots",
    "sweep",
    "write_cache",
]

#: Where the sweep records its verdicts (read by the boot picker, the
#: listen key-health endpoint, and the operator).
CACHE_FILENAME = "api-key-liveness.json"

#: Env names that name API-key slots, shared with the usage inventory so
#: the sweep and the quota cache always mean the same thing by "a key".
KEY_ALIAS_RE = re.compile(
    r"^(COMMANDCODE_API_KEY(?:_[A-Za-z0-9]+)?|OPENCODE_GO_API_KEY(?:_[A-Za-z0-9]+)?)$"
)

#: One cheap read-only liveness URL per provider. CommandCode settles
#: liveness AND balance on the credits endpoint; opencode-go settles it
#: on the usage endpoint.
LIVENESS_URLS = {
    "commandcode": "https://api.commandcode.ai/alpha/billing/credits",
    "opencode-go": "https://opencode.ai/zen/go/v1/usage",
}

_MAX_BODY = 65_536

_VALID_VERDICTS = ("alive", "exhausted", "dead", "unknown", "unresolved")


def _provider_for_alias(alias: str) -> str | None:
    if alias.startswith("COMMANDCODE"):
        return "commandcode"
    if alias.startswith("OPENCODE_GO"):
        return "opencode-go"
    return None


def collect_slots(specs: list[dict]) -> dict[str, dict]:
    """Map key-slot env names to their consumers from agent specs.

    Reads the declared Hermes failover pools
    (``available_harnesses.hermes.failover.accounts``: engine key ->
    env-name list) plus each engine's own
    ``provider.auth_token_env``. Returns
    ``{env_name: {"provider": ..., "agents": [...], "engines": [...]}}``
    with sorted, de-duplicated lists. Specs that declare no pools
    contribute nothing — an agent without failover has no slots to map.
    """
    slots: dict[str, dict] = {}
    for document in specs:
        spec = document.get("spec", document)
        agent = document.get("agent_name") or document.get("metadata", {}).get("name") or spec.get("name") or "unknown"
        harnesses = spec.get("available_harnesses") or {}
        hermes = harnesses.get("hermes") or {}
        failover = hermes.get("failover") or {}
        accounts = failover.get("accounts") or {}
        engines = spec.get("available_engines") or {}
        names: dict[str, set[str]] = {}
        for engine_key, env_names in accounts.items():
            for env_name in env_names or []:
                names.setdefault(env_name, set()).add(engine_key)
        for engine_key, engine in engines.items():
            if not isinstance(engine, dict):
                continue
            provider = (engine or {}).get("provider") or {}
            if not isinstance(provider, dict):
                continue
            env_name = provider.get("auth_token_env")
            if env_name:
                names.setdefault(env_name, set()).add(engine_key)
        for env_name, engine_keys in names.items():
            if not isinstance(env_name, str) or not KEY_ALIAS_RE.fullmatch(env_name):
                continue
            slot = slots.setdefault(
                env_name,
                {"provider": _provider_for_alias(env_name), "agents": [], "engines": []},
            )
            if agent not in slot["agents"]:
                slot["agents"].append(agent)
            for key in sorted(engine_keys):
                if key not in slot["engines"]:
                    slot["engines"].append(key)
    for slot in slots.values():
        slot["agents"] = sorted(slot["agents"])
        slot["engines"] = sorted(slot["engines"])
    return slots


def classify_status(
    provider: str, http_status: int | None, body: str = "", *, now: float | None = None
) -> tuple[str, str]:
    """Reduce one provider answer to a (verdict, reason) pair.

    ``http_status`` is None when the request never got an answer
    (timeout / DNS / refused) — always ``unknown``. A 200 with a zero
    credit balance, or a 400 carrying the ``insufficient credits``
    phrasing, is ``exhausted``: the key is valid, the allowance is not.
    401 / 403 is ``dead``. Anything else unanswered-shaped (5xx,
    unexpected 4xx) is ``unknown`` — a network blip is not a dead key.
    """
    text = (body or "").lower()
    if http_status is None:
        return "unknown", "no-answer"
    if http_status in (401, 403):
        return "dead", f"http-{http_status}"
    if "insufficient credits" in text or "insufficient balance" in text:
        return "exhausted", f"http-{http_status}-billing-exhausted"
    if http_status == 200:
        capped = capped_windows(provider, body, now=now)
        if capped:
            return "exhausted", "http-200-quota-" + ",".join(row["name"] for row in capped)
        if provider == "commandcode" and _zero_balance(body):
            return "exhausted", "http-200-zero-balance"
        return "alive", "http-200"
    if 500 <= http_status <= 599:
        return "unknown", f"http-{http_status}"
    return "unknown", f"http-{http_status}-unshaped"


def capped_windows(provider: str, body: str, *, now: float | None = None) -> list[dict]:
    """Only current, provider-measured quota windows can establish a cap."""
    from datetime import datetime, timezone
    from .provider_usage_projection import project

    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    observed = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    payloads = {"credits" if provider == "commandcode" else "quota": data}
    snapshot = project(provider, payloads, observed)
    return [row for row in snapshot["windows"] if row["state"] == "known" and row["remaining"] == 0]


def _zero_balance(body: str) -> bool:
    """True when a 200 credits payload reports no remaining balance."""
    try:
        data = json.loads(body or "{}")
    except (ValueError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    balances = data.get("balances")
    candidates: list[float] = []
    credits = data.get("credits")
    if isinstance(credits, dict):
        for field in ("monthlyCredits", "purchasedCredits", "freeCredits"):
            value = credits.get(field)
            if type(value) in (int, float) and value >= 0:
                candidates.append(float(value))
    if isinstance(balances, dict):
        for value in balances.values():
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                candidates.append(float(value))
    for key in ("balance", "credits", "remaining"):
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            candidates.append(float(value))
    return bool(candidates) and all(v <= 0 for v in candidates)


def _probe_once(
    url: str, token: str, timeout_s: float, opener=None
) -> tuple[int | None, str]:
    """One bounded GET; returns (status, body-head). Never raises."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "application/json",
            "User-Agent": "scitex-agent-container/key-liveness",
        },
        method="GET",
    )
    open_request = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
    ).open
    try:
        with open_request(request, timeout=timeout_s) as response:
            raw = response.read(_MAX_BODY + 1)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as error:
        try:
            raw = error.read(_MAX_BODY + 1)
        except (OSError, ValueError):  # stx-allow: fallback (reason: an unreadable error body must not fail the sweep — the status alone still classifies)
            raw = b""
        finally:
            error.close()
        return error.code, raw[:_MAX_BODY].decode("utf-8", "replace")
    except (OSError, ValueError, TimeoutError, urllib.error.URLError):
        return None, ""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    return status, raw[:_MAX_BODY].decode("utf-8", "replace")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


def sweep(
    slots: dict[str, dict],
    secrets: dict[str, str],
    *,
    timeout_s: float = 15.0,
    opener=None,
    now: float | None = None,
) -> dict:
    """Probe every slot once; return the cache document (pure of clock/IO).

    Slots with no secret resolve ``unresolved`` without touching the
    network. Providers without a liveness URL resolve ``unknown`` with
    reason ``no-liveness-route`` rather than guessing at an endpoint.
    """
    checked_at = time.time() if now is None else now
    verdicts: dict[str, dict] = {}
    for alias in sorted(slots):
        slot = slots[alias]
        token = secrets.get(alias, "")
        if not token or not token.strip():
            verdicts[alias] = {
                "verdict": "unresolved",
                "reason": "no-value-on-host",
                "provider": slot.get("provider"),
                "agents": slot.get("agents", []),
                "engines": slot.get("engines", []),
            }
            continue
        url = LIVENESS_URLS.get(slot.get("provider") or "")
        if not url:
            verdicts[alias] = {
                "verdict": "unknown",
                "reason": "no-liveness-route",
                "provider": slot.get("provider"),
                "agents": slot.get("agents", []),
                "engines": slot.get("engines", []),
            }
            continue
        status, body = _probe_once(url, token.strip(), timeout_s, opener=opener)
        verdict, reason = classify_status(slot.get("provider") or "", status, body, now=checked_at)
        verdicts[alias] = {
            "verdict": verdict,
            "reason": reason,
            "http_status": status,
            "provider": slot.get("provider"),
            "agents": slot.get("agents", []),
            "engines": slot.get("engines", []),
            "checked_at": checked_at,
            "capped_windows": capped_windows(slot.get("provider") or "", body, now=checked_at),
        }
    return {"checked_at": checked_at, "slots": verdicts}


def write_cache(path: str | Path, doc: dict) -> Path:
    """Atomically write the sweep document; return the final path."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target
