#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tiny synthetic key probes for declared Hermes routes.

Sends one minimal, non-conversational inference request per declared key and
reports whether the backend accepted the credential — without touching live
credential state or conversation content. Unknown stays unknown: timeouts and
connection failures report ``available=None`` (skip temporarily), never a
rejection. Only an authoritative HTTP rejection (401/403) or an explicit
quota/billing signal marks a key down.

This module vendors the probe SAC needs locally (httpx is already a hard
dependency) so preflight does not depend on an unreleased
``scitex_genai.availability`` API.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

#: Native Hermes providers whose OpenAI-compatible surface SAC knows from its
#: own evidence (usage endpoints, gateway replay). Hermes owns the endpoint,
#: protocol and session handling for these; the probe only borrows the
#: chat-completions surface for a synthetic credential check.
_NATIVE_API_ROOTS = {
    "opencode-go": "https://opencode.ai/zen/go/v1",
}

_BILLING_WORDS = (
    "insufficient credits",
    "credits exhausted",
    "out of credits",
    "billing",
    "quota exceeded",
    "usage limit exceeded",
)


@dataclass(frozen=True)
class ProbeStatus:
    """Provider-native status: ``kind`` is ``http``/``timeout``/``network``."""

    kind: str
    code: int | None = None


@dataclass(frozen=True)
class ProbeCheck:
    """Human detail backing the verdict (usually the backend message)."""

    detail: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Outcome of one synthetic key probe.

    ``available`` is True (backend accepted the key), False (backend
    rejected it or reported quota), or None (unknown — timeout or
    connection failure; skip temporarily, never reject).
    """

    available: bool | None
    status: ProbeStatus = field(default_factory=lambda: ProbeStatus("network"))
    check: ProbeCheck = field(default_factory=ProbeCheck)
    reset_at: float | None = None

    def to_dict(self) -> dict:
        """JSON-serializable form for diagnostic logging."""
        return {
            "available": self.available,
            "status": {"kind": self.status.kind, "code": self.status.code},
            "detail": self.check.detail,
            "reset_at": self.reset_at,
        }


@dataclass(frozen=True)
class ProviderRoute:
    """Resolved synthetic-probe target for one provider + model."""

    protocol: str
    endpoint_url: str


def provider_route(provider: str, model_id: str) -> ProviderRoute:
    """Resolve the synthetic-probe target for a Hermes-native provider.

    Raises
    ------
    ValueError
        The provider has no known probe surface — the caller must treat the
        route as unknown (skip temporarily), not as rejected.
    """
    del model_id  # The native surface is per-provider, not per-model.
    root = _NATIVE_API_ROOTS.get(provider)
    if root is None:
        raise ValueError(
            f"No known synthetic-probe surface for native provider {provider!r}"
        )
    return ProviderRoute(
        protocol="openai-chat-completions",
        endpoint_url=f"{root}/chat/completions",
    )


def _request_body(protocol: str, model: str) -> dict:
    if protocol == "openai-responses":
        return {
            "model": model,
            "input": "SAC availability probe. Reply OK.",
            # opencode-go rejects max_output_tokens < 16 with a 400 that
            # the gate would misread as a dead key (measured 2026-10-09).
            "max_output_tokens": 16,
            "stream": False,
        }
    return {
        "model": model,
        "messages": [
            {"role": "user", "content": "SAC availability probe. Reply OK."}
        ],
        "max_tokens": 1,
        "stream": False,
    }


def _reset_at(response: httpx.Response, body_detail: str) -> float | None:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            reset = error.get("reset_at") or error.get("resetAt")
            if isinstance(reset, (int, float)) and reset > 0:
                return float(reset)
        for key in ("reset_at", "resetAt", "retry_after"):
            reset = payload.get(key)
            if isinstance(reset, (int, float)) and reset > 0:
                base = 0.0 if key != "retry_after" else time.time()
                value = float(reset)
                return value if key != "retry_after" else base + value
    retry_after = response.headers.get("retry-after")
    if retry_after:
        try:
            return time.time() + float(retry_after)
        except ValueError:
            pass
    del body_detail
    return None


def probe_provider_key(
    provider: str,
    model: str,
    token: str,
    *,
    endpoint_url: str,
    protocol: str,
    extra_headers: dict | None = None,
    session_id: str = "",
    timeout_s: float = 20,
) -> ProbeResult:
    """Send one synthetic request; report whether the key authenticates."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "scitex-agent-container/availability-probe",
    }
    if session_id:
        headers["X-SciTeX-Session-ID"] = session_id
    for key, value in (extra_headers or {}).items():
        headers[str(key)] = str(value)
    try:
        response = httpx.post(
            endpoint_url,
            json=_request_body(protocol, model),
            headers=headers,
            timeout=timeout_s,
        )
    except httpx.TimeoutException as exc:
        return ProbeResult(
            available=None,
            status=ProbeStatus("timeout"),
            check=ProbeCheck(f"probe timed out after {timeout_s}s: {exc}"),
        )
    except httpx.HTTPError as exc:
        return ProbeResult(
            available=None,
            status=ProbeStatus("network"),
            check=ProbeCheck(f"probe could not reach {provider}: {exc}"),
        )
    code = response.status_code
    if 200 <= code < 300:
        return ProbeResult(
            available=True,
            status=ProbeStatus("http", code),
            check=ProbeCheck("backend accepted the key"),
        )
    try:
        payload = response.json()
    except ValueError:
        payload = None
    detail = ""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                detail = message
        elif isinstance(error, str) and error:
            detail = error
        elif isinstance(payload.get("message"), str):
            detail = payload["message"]
    if not detail:
        detail = f"HTTP {code}"
    lowered = detail.lower()
    billing = code == 402 or any(word in lowered for word in _BILLING_WORDS)
    return ProbeResult(
        available=False,
        status=ProbeStatus("http", code),
        check=ProbeCheck(detail),
        reset_at=_reset_at(response, detail)
        if (billing or code in (403, 429))
        else None,
    )
