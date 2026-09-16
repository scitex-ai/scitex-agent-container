"""Ask the provider backend whether the resolved key ACTUALLY authenticates.

``spec.claude.provider.auth_token_env`` names a host env var holding the
backend API key. :func:`..runtimes._apptainer_provider.resolve_provider_api_key`
resolves it and guards the result with ``if not api_key: raise`` — an
EMPTINESS test. That guard is structurally unable to catch the most likely
misconfiguration, and on 2026-09-07 it did not::

    handyman-c03-01   auth_token_env: SAC_LOCAL_GPTOSS_KEY
        login shell:  unset
        $HOME/.env:   a 13-character PLACEHOLDER
        resolver:     returns OK, len=13        <-- never raised
        every turn:   401 Invalid API key, attempt 10/10
        heartbeat:    green

A placeholder is not empty, so the emptiness guard passes it, sac injects it,
and the agent boots into an unbroken sequence of 401s while looking alive. The
provider module's own docstring names this exact outcome as the thing it exists
to prevent: "a silent fallback would boot an agent whose every turn 401s behind
a fresh-looking heartbeat." It arrived through a placeholder rather than a
fallback, and the guard could not tell the difference.

Measured population that day: 30 specs across three hosts declared that env
var — a key named for GPT-OSS, a backend none of them still ran (they all ran
``qwen38-27b`` through the shared gateway). ``_template_handyman`` was one of
them, so every agent minted from it inherited the defect. On the one host with
no placeholder in ``$HOME/.env`` those same specs REFUSED TO START, which is
the correct behaviour and the evidence that the placeholder, not the missing
key, is what breaks the guard.

Only the backend can settle whether a well-formed string is a working key, so
this probe asks it.

WHAT MAY REJECT, following :func:`.build_cmds._check_host_route`'s doctrine in
the same command — UNKNOWN never rejects:

    key resolves to nothing        FAIL       (already fatal at start)
    backend answers 401/403        FAIL       (an authoritative rejection)
    backend answers anything else  OK         (reachable, not rejected)
    connection refused / timeout   WARN       (absence of evidence)

A gateway that is briefly down must not fail every preflight on the fleet.

Hermes chat-completions engines need stronger evidence. Some providers expose
``GET /v1/models`` without authenticating it, so Hermes is probed through the
resolved ``POST /v1/chat/completions`` path with the selected model. The real
key must succeed, the invalid control must be rejected, and a returned model
identity must equal the selection. Non-Hermes providers retain the generic
``/models`` behavior above.

THE PROBE CARRIES ITS OWN POSITIVE CONTROL. A backend that answers 200 to every
request regardless of credentials would make an "OK" verdict meaningless — the
check would certify a dead key. So a second request is sent with a deliberately
invalid key. If THAT is not rejected either, the endpoint does not discriminate
on credentials and this probe cannot answer the question it was asked; it
reports INDISCRIMINATE rather than OK. Costing one extra request in a
diagnostic command is the price of a verdict that means something.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

#: The verdict states. ``OK`` is only reachable when the control also passed.
INACTIVE = "inactive"
OK = "ok"
REJECTED = "rejected"
UNRESOLVED = "unresolved"
UNREACHABLE = "unreachable"
INDISCRIMINATE = "indiscriminate"
PROBE_FAILED = "probe-failed"
MODEL_MISMATCH = "model-mismatch"

#: Sent as the control. Deliberately not a plausible key: any backend that
#: accepts THIS is not checking credentials at all.
_CONTROL_KEY = "sac-preflight-control-not-a-valid-key"

_REJECTING_STATUSES = frozenset({401, 403})


@dataclass(frozen=True)
class ProviderAuthVerdict:
    """What the backend said about the resolved key."""

    state: str
    detail: str
    status_code: int | None = None
    actual_model: str | None = None

    @property
    def is_failure(self) -> bool:
        """True only for states backed by EVIDENCE that the key is wrong.

        ``UNREACHABLE`` and ``INDISCRIMINATE`` are deliberately absent: the
        first is absence of evidence, the second is evidence that the probe
        cannot answer. Neither convicts a key.
        """
        return self.state in (MODEL_MISMATCH, PROBE_FAILED, REJECTED, UNRESOLVED)


def models_url(base_url: str) -> str:
    """The models endpoint for an Anthropic-compatible ``base_url``.

    Specs carry the bare origin (``http://host:18772``), but a hand-written
    one may already end in ``/v1``; appending blindly would produce
    ``/v1/v1/models`` and a 404, which this module reads as "reachable, not
    rejected" — an OK verdict from an endpoint that was never consulted.
    """
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        return f"{trimmed}/models"
    return f"{trimmed}/v1/models"


def chat_completions_url(base_url: str) -> str:
    """Resolve the OpenAI chat-completions endpoint from a provider root."""
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/chat/completions"):
        return trimmed
    if trimmed.endswith("/v1"):
        return f"{trimmed}/chat/completions"
    return f"{trimmed}/v1/chat/completions"


def _uses_hermes_chat_completions(config, base_url: str) -> bool:
    """True only for the Hermes endpoint shape known to accept chat POSTs."""
    if str(getattr(config, "harness", "") or "").strip() != "hermes":
        return False
    path = urllib.parse.urlsplit(base_url).path.rstrip("/")
    return not path.endswith("/responses")


def _status_for(url: str, api_key: str, timeout: float) -> int | None:
    """HTTP status for a GET with ``api_key``, or None when unreachable.

    Both header conventions are sent because the fleet's gateways span them:
    ``Authorization: Bearer`` (OpenAI-compatible, what LiteLLM reads) and
    ``x-api-key`` (Anthropic's own). Sending one only would read a backend
    that wanted the other as "does not check credentials".
    """
    request = urllib.request.Request(  # noqa: S310 (scheme comes from the spec)
        url,
        method="GET",
        headers={
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "User-Agent": "scitex-agent-container/preflight",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        # A 401 arrives HERE, not as a response — it is the answer we most
        # need, so catching HTTPError before URLError matters (HTTPError is a
        # SUBCLASS of URLError; the reverse order would report every rejection
        # as unreachable).
        return int(exc.code)
    except Exception:  # stx-allow: fallback (unreachable is not a bad key)
        return None


def _hermes_headers(config, provider, api_key: str) -> dict[str, str]:
    """Credential and non-secret identity headers for the Hermes probe."""
    from ..config._hermes_config import AGENT_ID_TEMPLATE, SESSION_ID_TEMPLATE

    agent = str(getattr(config, "name", "") or "provider-preflight")
    session = f"sac-preflight:{agent}"
    raw = getattr(provider, "extra_headers", {}) or {}
    headers = {
        str(name): str(value)
        .replace(AGENT_ID_TEMPLATE, agent)
        .replace(SESSION_ID_TEMPLATE, session)
        for name, value in raw.items()
    }
    headers.update(
        {
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "scitex-agent-container/preflight",
        }
    )
    return headers


def _chat_completion_for(
    config,
    provider,
    url: str,
    api_key: str,
    model: str,
    timeout: float,
) -> tuple[int | None, str | None]:
    """Return ``(status, response model)`` for one minimal real HTTP turn."""
    body = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply OK."}],
            "max_tokens": 1,
            "stream": False,
        }
    ).encode()
    request = urllib.request.Request(  # noqa: S310 (scheme comes from the spec)
        url,
        data=body,
        method="POST",
        headers=_hermes_headers(config, provider, api_key),
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            status = int(response.status)
            payload = response.read(1_048_576)
    except urllib.error.HTTPError as exc:
        return int(exc.code), None
    except Exception:  # stx-allow: fallback (unreachable is not a bad key)
        return None, None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, ValueError):
        return status, None
    actual = decoded.get("model") if isinstance(decoded, dict) else None
    return status, str(actual) if isinstance(actual, str) and actual else None


def _probe_hermes_chat_auth(
    config,
    provider,
    *,
    base_url: str,
    api_key: str,
    timeout: float,
) -> ProviderAuthVerdict:
    """Prove auth and selected-model identity on the endpoint Hermes uses."""
    model = str(getattr(config, "model", "") or "").strip()
    url = chat_completions_url(base_url)
    if not model:
        return ProviderAuthVerdict(
            PROBE_FAILED,
            f"Hermes provider preflight cannot POST {url}: resolved config.model "
            "is empty.",
        )
    status, actual = _chat_completion_for(
        config, provider, url, api_key, model, timeout
    )
    if status is None:
        return ProviderAuthVerdict(
            UNREACHABLE,
            f"{url} did not answer the Hermes chat-completions authentication "
            f"probe for selected model {model!r} within {timeout:g}s.",
        )
    env_name = str(getattr(provider, "auth_token_env", "") or "?")
    if status in _REJECTING_STATUSES:
        return ProviderAuthVerdict(
            REJECTED,
            f"{url} rejected the key from {env_name} with HTTP {status} while "
            f"probing selected model {model!r}.",
            status,
        )
    if not 200 <= status < 300:
        return ProviderAuthVerdict(
            PROBE_FAILED,
            f"{url} returned HTTP {status} for the real key and selected model "
            f"{model!r}; successful authentication/model resolution was not proven.",
            status,
        )
    if actual is not None and actual != model:
        return ProviderAuthVerdict(
            MODEL_MISMATCH,
            f"{url} accepted the real key but resolved model {actual!r}, not the "
            f"selected config.model {model!r}.",
            status,
            actual,
        )

    control, _ = _chat_completion_for(
        config, provider, url, _CONTROL_KEY, model, timeout
    )
    if control is None:
        return ProviderAuthVerdict(
            UNREACHABLE,
            f"{url} accepted the real key for selected model {model!r} but did "
            "not answer the deliberately invalid-key control request.",
            status,
            actual,
        )
    if control not in _REJECTING_STATUSES:
        return ProviderAuthVerdict(
            INDISCRIMINATE,
            f"{url} answered HTTP {status} for the real key and HTTP {control} "
            f"for a deliberately invalid key while probing model {model!r}; "
            "authentication is not discriminated on the actual inference path.",
            status,
            actual,
        )
    identity = f" and returned model {actual!r}" if actual is not None else ""
    return ProviderAuthVerdict(
        OK,
        f"{url} accepted the real key (HTTP {status}){identity}, and rejected "
        f"an invalid key (HTTP {control}) for selected model {model!r}.",
        status,
        actual,
    )


def probe_provider_auth(config, *, timeout: float = 5.0) -> ProviderAuthVerdict:
    """Ask ``config``'s provider backend whether its resolved key works."""
    from ..runtimes._apptainer_provider import (
        ProviderEnvError,
        provider_active,
        resolve_provider_api_key,
    )

    if not provider_active(config):
        return ProviderAuthVerdict(INACTIVE, "no provider override declared")

    try:
        api_key = resolve_provider_api_key(config)
    except ProviderEnvError as exc:
        return ProviderAuthVerdict(UNRESOLVED, str(exc))

    claude = getattr(config, "claude", None)
    provider = getattr(claude, "provider", None) if claude is not None else None
    base_url = str(getattr(provider, "base_url", "") or "")
    if _uses_hermes_chat_completions(config, base_url):
        return _probe_hermes_chat_auth(
            config,
            provider,
            base_url=base_url,
            api_key=api_key,
            timeout=timeout,
        )
    url = models_url(base_url)

    status = _status_for(url, api_key, timeout)
    if status is None:
        return ProviderAuthVerdict(
            UNREACHABLE,
            f"{base_url} did not answer within {timeout:g}s — cannot verify the "
            "key. This is NOT a failing key: a backend that is merely down "
            "must not fail a preflight.",
        )

    if status in _REJECTING_STATUSES:
        env_name = str(getattr(provider, "auth_token_env", "") or "?")
        return ProviderAuthVerdict(
            REJECTED,
            f"{base_url} rejected the key from {env_name} with HTTP {status}. "
            "The value resolved to a non-empty string, so start will NOT "
            "refuse — the agent would boot and 401 on every turn behind a "
            f"green heartbeat. Check what {env_name} actually holds (a "
            "placeholder in $HOME/.env resolves just as well as a real key) "
            "and whether it is still the variable this backend uses.",
            status,
        )

    control = _status_for(url, _CONTROL_KEY, timeout)
    if control is None:
        return ProviderAuthVerdict(
            UNREACHABLE,
            f"{base_url} accepted the key (HTTP {status}) but stopped "
            "answering during the control request, so 'accepted' is "
            "unconfirmed.",
            status,
        )
    if control not in _REJECTING_STATUSES:
        return ProviderAuthVerdict(
            INDISCRIMINATE,
            f"{base_url} answered HTTP {status} for the real key AND HTTP "
            f"{control} for a deliberately invalid one, so it is not checking "
            "credentials on this endpoint. The key may still be wrong — this "
            "probe cannot tell, and reports that rather than a green tick.",
            status,
        )

    return ProviderAuthVerdict(
        OK,
        f"{base_url} accepted the key (HTTP {status}) and rejected an invalid "
        f"one (HTTP {control}), so the endpoint discriminates and this key "
        "passes.",
        status,
    )
