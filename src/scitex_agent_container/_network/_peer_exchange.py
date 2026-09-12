"""Resolve harness-neutral asynchronous ``/v1/turn`` exchanges.

Some runners answer a turn synchronously with ``{"text": ...}``.  Others
admit delivery first and return the SciTeX status protocol's canonical HTTP
202 receipt.  The latter is not an agent reply: its ``exchange_id`` names a
separate result that must be polled before delivery can be claimed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

POLL_INTERVAL_S = 0.1


def resolve_turn_response(
    url: str,
    payload: Any,
    *,
    http_status: int | None,
    timeout_s: float,
) -> str:
    """Return a synchronous reply or resolve a canonical 202 exchange."""
    if isinstance(payload, dict) and "text" in payload:
        return str(payload["text"])

    exchange_id = _receipt_exchange_id(payload, http_status=http_status)
    return _poll_exchange(url, exchange_id, timeout_s=timeout_s)


def _receipt_exchange_id(payload: Any, *, http_status: int | None) -> str:
    """Validate an asynchronous receipt and return its opaque exchange id."""
    from .peer import PeerError

    if not isinstance(payload, dict):
        raise PeerError(f"peer returned malformed body: {payload!r}")
    status = payload.get("status_code")
    exchange_id = payload.get("exchange_id")
    valid_status = (
        isinstance(status, dict)
        and status.get("kind") == "http"
        and type(status.get("code")) is int
        and status.get("code") == 202
    )
    # The ssh transport cannot currently recover the POST's HTTP status, so
    # ``None`` means "validate the canonical body" rather than inventing one.
    valid_http_status = http_status in (None, 202)
    if not (
        valid_http_status
        and valid_status
        and isinstance(exchange_id, str)
        and bool(exchange_id)
    ):
        raise PeerError(
            "peer returned malformed body (asynchronous receipt): expected HTTP 202 "
            "with non-empty exchange_id and status_code=http/202; "
            f"got HTTP {http_status!r}, body={payload!r}"
        )
    return exchange_id


def _poll_exchange(url: str, exchange_id: str, *, timeout_s: float) -> str:
    """Poll one accepted exchange to a terminal status within one deadline."""
    from ._peer_timeout import PeerTimeoutPending
    from .peer import PeerError

    status_url = _exchange_url(url, exchange_id)
    hint = _poll_hint(status_url)
    deadline = time.monotonic() + max(0.0, timeout_s)
    last_body: dict[str, Any] | None = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = (
                f"Turn exchange {exchange_id} is still non-final after "
                f"{timeout_s:g}s. The turn was accepted; do not resend it. "
                f"Continue polling with `{hint}`."
            )
            raise PeerTimeoutPending(
                message,
                status="exchange_pending",
                timeout_s=timeout_s,
                possibilities=["accepted turn is still being delivered"],
                raw_body=last_body,
            )

        status_code, body = _get_exchange(status_url, timeout_s=remaining)
        if status_code != 200:
            raise PeerError(
                f"peer exchange probe returned HTTP {status_code}: {body!r}; "
                f"retry the probe with `{hint}`"
            )
        status = _exchange_status(body, exchange_id=exchange_id)
        code = status["code"]
        if code == 202:
            last_body = body
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        message = str(status.get("message") or "")
        if code == 200:
            detail = f": {message}" if message else ""
            return (
                f"Turn accepted (exchange {exchange_id}){detail}. "
                "This confirms delivery, not agent completion; completion "
                "feedback is asynchronous."
            )
        raise PeerError(
            f"turn exchange {exchange_id} concluded with http/{code}: "
            f"{message or 'no detail supplied'}; inspect the final result with "
            f"`{hint}` before retrying"
        )


def _exchange_url(turn_url: str, exchange_id: str) -> str:
    """Build the exchange resource beside either local or ssh turn URL."""
    parts = urllib.parse.urlsplit(turn_url)
    encoded_id = urllib.parse.quote(exchange_id, safe="")
    return urllib.parse.urlunsplit(
        parts._replace(path=f"/v1/exchanges/{encoded_id}", query="", fragment="")
    )


def _poll_hint(status_url: str) -> str:
    """Return a runnable probe for the concrete transport."""
    parts = urllib.parse.urlsplit(status_url)
    if parts.scheme == "ssh":
        return (
            f"ssh {parts.hostname} curl -sS http://127.0.0.1:{parts.port}{parts.path}"
        )
    return f"curl -sS {status_url}"


def _get_exchange(status_url: str, *, timeout_s: float) -> tuple[int, dict[str, Any]]:
    """GET an exchange over the same local/ssh transport as its turn."""
    from .peer import PeerError

    parts = urllib.parse.urlsplit(status_url)
    if parts.scheme == "ssh":
        from ._ssh_curl import _get_via_ssh_curl, split_status_line

        if not parts.hostname or not parts.port:
            raise PeerError(f"malformed ssh exchange URL: {status_url!r}")
        rc, stdout, stderr = _get_via_ssh_curl(
            host=parts.hostname,
            port=parts.port,
            path=parts.path,
            timeout_s=timeout_s,
        )
        if rc != 0:
            detail = stderr.decode("utf-8", "replace").strip()
            raise PeerError(f"ssh+curl exchange probe failed (rc={rc}): {detail[:300]}")
        http_status, body_text = split_status_line(stdout)
        if http_status is None:
            raise PeerError("ssh+curl exchange probe returned no HTTP status")
        return http_status, _decode_json_body(body_text)

    request = urllib.request.Request(status_url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return int(response.status), _decode_json_body(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise PeerError(
            f"peer exchange probe returned HTTP {exc.code}: {detail or exc.reason}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PeerError(
            f"peer exchange probe unreachable at {status_url}: {exc}"
        ) from exc


def _decode_json_body(text: str) -> dict[str, Any]:
    """Decode curl output, tolerating ssh login banners before the JSON line."""
    from .peer import PeerError

    lines = [line for line in text.strip().splitlines() if line.strip()]
    try:
        payload = json.loads(lines[-1])
    except (json.JSONDecodeError, IndexError) as exc:
        raise PeerError(f"peer exchange probe returned non-JSON: {text[:300]}") from exc
    if not isinstance(payload, dict):
        raise PeerError(f"peer exchange probe returned malformed body: {payload!r}")
    return payload


def _exchange_status(payload: dict[str, Any], *, exchange_id: str) -> dict[str, Any]:
    """Validate the canonical exchange result envelope."""
    from .peer import PeerError

    status = payload.get("status_code")
    if not (
        payload.get("exchange_id") == exchange_id
        and isinstance(status, dict)
        and status.get("kind") == "http"
        and type(status.get("code")) is int
    ):
        raise PeerError(
            f"peer exchange {exchange_id} returned malformed result: {payload!r}"
        )
    return status


__all__ = ["resolve_turn_response"]
