"""Generic stdlib HTTP client for in-SIF auto-fallback verbs.

PR-3 Checkpoint 3 CLI piece. When the in-SIF CLI verb (``delete``,
``status``, ``send``, ``tail``) detects it is running inside an
apptainer SIF (via :func:`_in_sif_broker.is_in_sif`), it auto-
proxies the operation to the host's ``sac listen`` server. This
module is the transport layer.

The existing :mod:`._spawn_client` is purpose-built for the
spawn POST (one path, one body shape, returns just the parsed
JSON). The in-SIF verbs need a slightly broader surface:

  * any HTTP method (GET for tail/status, DELETE for delete,
    POST for send);
  * any URL path under ``/agents/<name>/...``;
  * the FULL ``(http_status, parsed_body)`` tuple — the verb
    needs the status to map into an :class:`InSifOutcome`.

Same fail-loud invariants as the spawn client:

  * Missing ``SAC_LISTEN_BASE_URL`` → :class:`HostListenTransportError`
    naming the env var. The apptainer runtime always injects it;
    a missing value means the container was launched wrong.
  * Transport failure (host listen down, DNS, refused, timeout) →
    :class:`HostListenTransportError` carrying the URL so the
    operator-facing message names what was tried.
  * Non-2xx HTTP responses do NOT raise — they are returned as the
    tuple ``(http_status, body)`` because the verb layer wants to
    branch on ``body["kind"]`` to map into the :class:`InSifOutcome`
    exit code. A 403 / 400 / 410 is a valid host answer, not a
    transport error.

Stdlib-only on purpose (mirrors :mod:`._spawn_client`): urllib
+ injected ``opener`` callable for tests.
"""

from __future__ import annotations

import json
import logging
import socket
from typing import Any, Callable
from urllib import error as urlerror
from urllib import request as urlrequest

logger = logging.getLogger(__name__)

__all__ = ["HostListenTransportError", "host_listen_call"]

_DEFAULT_TIMEOUT_S = 30.0


class HostListenTransportError(RuntimeError):
    """Raised when the in-SIF verb cannot reach the host listen.

    ``url`` carries the full URL we tried so the
    :func:`_in_sif_outcome.transport_outcome` builder can echo it
    into the stdout JSON for the operator to copy/paste-debug.
    """

    def __init__(self, message: str, *, url: str | None = None) -> None:
        super().__init__(message)
        self.url = url


def _resolve_base_url(explicit: str | None) -> str:
    """Return the listen base URL or raise loudly.

    Mirror of :mod:`._spawn_client._resolve_base_url` so the
    fail-loud invariant on missing ``SAC_LISTEN_BASE_URL`` is
    identical across the spawn path and the in-SIF verb path.
    """
    if explicit:
        return explicit.rstrip("/")
    from .._env import getenv

    raw = (getenv("LISTEN_BASE_URL", "") or "").strip()
    if not raw:
        raise HostListenTransportError(
            "in-SIF host-listen call requires SAC_LISTEN_BASE_URL "
            "(the host-stable `sac listen` URL injected into the "
            "container by the apptainer runtime). Got empty/unset."
        )
    return raw.rstrip("/")


def _resolve_bearer(explicit: str | None) -> str | None:
    """Return the listen bearer, or ``None`` if unset.

    An absent bearer is NOT fatal — the listen server may run
    without auth in dev. Production injects the value via the
    apptainer runtime; the CLI verb forwards what it received.
    """
    if explicit is not None:
        return explicit or None
    from .._env import getenv

    raw = (getenv("LISTEN_BEARER", "") or "").strip()
    return raw or None


def _parse_body(raw: bytes) -> Any:
    """Return JSON-parsed body or raw text fallback.

    Non-JSON bodies (a proxy error page, an old-daemon HTML) are
    decoded as UTF-8 with errors=replace so the caller still sees
    SOMETHING. The :func:`_in_sif_outcome.build_outcome` mapper
    treats non-dict bodies as ``kind="transport"`` (no structured
    tag to switch on), so the operator sees a non-classifiable
    failure clearly.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        # stx-allow: fallback (reason: non-JSON body surfaced
        # verbatim — see module docstring; the outcome layer maps
        # this to transport class so the consumer sees an
        # unrecognised response shape rather than a silent drop)
        return raw.decode("utf-8", errors="replace")


def host_listen_call(
    method: str,
    path: str,
    *,
    body: dict | None = None,
    base_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    opener: Callable | None = None,
) -> tuple[int, Any]:
    """Call the host listen and return ``(http_status, parsed_body)``.

    Does NOT raise on non-2xx HTTP statuses — those are valid host
    answers the verb layer maps into an :class:`InSifOutcome`.
    Raises :class:`HostListenTransportError` only when the request
    never made it to a structured response (DNS / connection
    refused / timeout / malformed URL / missing base URL).

    Args:
        method: HTTP method (``"GET"`` / ``"POST"`` / ``"DELETE"``
            / ``"PUT"``). The verb layer picks per surface.
        path: URL path under the base, leading slash optional.
            Common values: ``/agents/<name>``, ``/agents/<name>/tail``,
            ``/agents/<name>/send``.
        body: Optional JSON-serialisable request body. Sent with
            ``Content-Type: application/json``. ``None`` means no
            body (typical for GET / DELETE).
        base_url: Override ``SAC_LISTEN_BASE_URL``. Tests pass an
            in-process URL; production passes ``None`` so the env
            wins.
        bearer: Override ``SAC_LISTEN_BEARER``. Tests pass explicit
            values; production passes ``None``.
        timeout_s: Per-request timeout. Default 30 seconds — enough
            for the host's verb subprocess (a DELETE SIGTERMs a pid,
            a GET reads a small JSON, neither should be slow).
        opener: Optional ``urllib.request.urlopen``-shaped callable.
            Default ``urlrequest.urlopen``; tests inject a fake
            opener that returns a urllib-response-shaped object so
            no real network round-trip happens.

    Returns:
        ``(http_status, parsed_body)`` tuple. ``parsed_body`` is
        the JSON dict on a structured response, a fallback string
        when the body wasn't JSON, or ``None`` when the body was
        empty.

    Raises:
        HostListenTransportError: when no HTTP response could be
            obtained. The error message names what was tried; the
            ``url`` attribute carries the full URL so the outcome
            builder can echo it into stdout.
    """
    resolved_base = _resolve_base_url(base_url)
    resolved_bearer = _resolve_bearer(bearer)
    rel = path if path.startswith("/") else f"/{path}"
    url = f"{resolved_base}{rel}"

    headers: dict[str, str] = {}
    if resolved_bearer:
        headers["Authorization"] = f"Bearer {resolved_bearer}"
    data: bytes | None = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = urlrequest.Request(url, method=method.upper(), data=data, headers=headers)
    opener = opener or urlrequest.urlopen
    try:
        with opener(req, timeout=timeout_s) as resp:
            raw = resp.read()
            return resp.status, _parse_body(raw)
    except urlerror.HTTPError as exc:
        # HTTPError IS a urllib response — non-2xx status with a
        # body. We want the structured body so the verb layer can
        # branch on body["kind"]. Read the body, return the tuple
        # — not a transport error.
        try:
            raw = exc.read()
        except Exception:  # stx-allow: fallback (reason: HTTPError.read may itself raise on some Python versions when the underlying body stream is exhausted; in that case fall back to the message)
            raw = b""
        return exc.code, _parse_body(raw)
    except urlerror.URLError as exc:
        # Transport-level failure: connection refused, DNS error,
        # timeout. Fail loud with the URL so the operator sees
        # what was tried.
        raise HostListenTransportError(
            f"host listen unreachable at {url!r}: {exc.reason}",
            url=url,
        ) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise HostListenTransportError(
            f"host listen call to {url!r} timed out after {timeout_s}s",
            url=url,
        ) from exc
    except OSError as exc:
        # Broader socket-level error (network unreachable, etc.).
        raise HostListenTransportError(
            f"host listen call to {url!r} failed: {exc}",
            url=url,
        ) from exc
