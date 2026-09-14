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

from scitex_dev.status import StatusCode, is_exchange_id

POLL_INTERVAL_S = 0.1


def resolve_turn_response(
    url: str,
    payload: Any,
    *,
    http_status: int | None,
    timeout_s: float,
) -> str:
    """Return a synchronous reply or resolve a canonical 202 exchange."""
    exchange_id = _receipt_exchange_id(payload, http_status=http_status)
    return _poll_exchange(
        url,
        exchange_id,
        timeout_s=timeout_s,
        accepted_body=payload if isinstance(payload, dict) else None,
    )


def _receipt_exchange_id(payload: Any, *, http_status: int | None) -> str:
    """Validate an asynchronous receipt and return its opaque exchange id."""
    from .peer import PeerError

    if not isinstance(payload, dict):
        raise PeerError(f"peer returned malformed body: {payload!r}")
    exchange_id = payload.get("exchange_id")
    # The ssh transport cannot currently recover the POST's HTTP status, so
    # ``None`` means "validate the canonical body" rather than inventing one.
    valid_http_status = http_status in (None, 202)
    if not valid_http_status:
        raise PeerError(
            "peer returned malformed body (asynchronous receipt): expected HTTP 202 "
            "with canonical xch_ exchange_id and status_code=http/202; "
            f"got HTTP {http_status!r}, body={payload!r}"
        )
    if not is_exchange_id(exchange_id):
        raise PeerError(
            "peer returned malformed body (asynchronous receipt): expected HTTP 202 "
            "with canonical xch_ exchange_id and status_code=http/202; "
            f"got HTTP {http_status!r}, body={payload!r}"
        )
    status = _status_code(payload, exchange_id=str(exchange_id or "receipt"))
    if not (
        valid_http_status
        and status.kind == "http"
        and status.code == 202
        and not status.final
        and is_exchange_id(exchange_id)
    ):
        raise PeerError(
            "peer returned malformed body (asynchronous receipt): expected HTTP 202 "
            "with canonical xch_ exchange_id and status_code=http/202; "
            f"got HTTP {http_status!r}, body={payload!r}"
        )
    _validate_receipt_projection(payload, status=status, exchange_id=exchange_id)
    return exchange_id


def _poll_exchange(
    url: str,
    exchange_id: str,
    *,
    timeout_s: float,
    accepted_body: dict[str, Any] | None = None,
) -> str:
    """Poll one accepted exchange to a terminal status within one deadline."""
    from ._peer_timeout import PeerTimeoutPending
    from .peer import PeerError

    status_url = _exchange_url(url, exchange_id)
    hint = _poll_hint(status_url)
    deadline = time.monotonic() + max(0.0, timeout_s)
    # Preserve the accepted receipt even if the deadline expires before the
    # first GET. Consumers can then surface optional fields such as Hermes'
    # delivery_mode instead of replacing a valid receipt with an empty guess.
    last_body: dict[str, Any] | None = accepted_body
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
                exchange_id=exchange_id,
                poll_hint=hint,
            )

        status_code, body = _get_exchange(status_url, timeout_s=remaining)
        if status_code != 200:
            raise PeerError(
                f"peer exchange probe returned HTTP {status_code}: {body!r}; "
                f"retry the probe with `{hint}`"
            )
        status = _exchange_status(body, exchange_id=exchange_id)
        if not status.final:
            last_body = body
            time.sleep(min(POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))
            continue
        message = status.message
        if status.kind == "http" and status.code == 200:
            detail = f": {message}" if message else ""
            return (
                f"Turn accepted (exchange {exchange_id}){detail}. "
                "This confirms delivery, not agent completion; completion "
                "feedback is asynchronous."
            )
        raise PeerError(
            f"turn exchange {exchange_id} concluded with {status.kind}/{status.code}: "
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


def _exchange_status(payload: dict[str, Any], *, exchange_id: str) -> StatusCode:
    """Validate the canonical exchange result envelope."""
    from .peer import PeerError

    if payload.get("exchange_id") != exchange_id:
        raise PeerError(
            f"peer exchange {exchange_id} returned malformed result: {payload!r}"
        )
    status = _status_code(payload, exchange_id=exchange_id)
    _validate_receipt_projection(payload, status=status, exchange_id=exchange_id)
    return status


def _status_code(payload: dict[str, Any], *, exchange_id: str) -> StatusCode:
    """Parse the one authoritative status primitive from an exchange body."""
    from .peer import PeerError

    wire = payload.get("status_code")
    if not isinstance(wire, dict):
        raise PeerError(
            f"peer returned malformed body: exchange {exchange_id} has no canonical "
            f"status_code: {payload!r}"
        )
    try:
        return StatusCode.from_dict(wire)
    except Exception as exc:
        raise PeerError(
            f"peer exchange {exchange_id} returned invalid canonical status: {exc}; "
            "do not resend an accepted turn; repeat the same exchange GET and "
            "inspect the peer turn-bridge logs"
        ) from exc


def _validate_receipt_projection(
    payload: dict[str, Any], *, status: StatusCode, exchange_id: str
) -> None:
    """Reject a receipt projection that disagrees with ``StatusCode``.

    ``status_code`` is the protocol primitive and therefore authoritative.
    Hermes also includes a convenient ``receipt`` projection for humans.  It
    is optional at protocol boundaries, but when present it must describe the
    same finality/state; otherwise the client would have two incompatible
    answers and no principled way to choose one.
    """
    from .peer import PeerError

    receipt = payload.get("receipt")
    if receipt is None:
        return
    if not isinstance(receipt, dict) or type(receipt.get("final")) is not bool:
        raise PeerError(
            f"peer exchange {exchange_id} returned malformed receipt projection: "
            f"{receipt!r}; do not resend the accepted turn"
        )
    expected_state = _receipt_state(status)
    if (
        receipt.get("final") is not status.final
        or receipt.get("state") != expected_state
    ):
        raise PeerError(
            f"peer exchange {exchange_id} returned contradictory receipt: "
            f"status_code={status.kind}/{status.code} derives "
            f"state={expected_state!r}, final={status.final}, but receipt={receipt!r}; "
            "do not resend the accepted turn"
        )


def _receipt_state(status: StatusCode) -> str:
    """Derive Hermes' display projection from the canonical status."""
    if not status.final:
        return (
            "retryable" if status.kind == "http" and status.code == 102 else "pending"
        )
    if status.kind == "http" and status.code == 200:
        return "delivered"
    return "failed"


__all__ = ["resolve_turn_response"]
