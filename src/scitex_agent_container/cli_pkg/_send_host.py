"""Authenticated host prompt dispatch shared by CLI and MCP callers."""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from scitex_dev.status import StatusCode, StatusError, is_exchange_id

from .._lifecycle._in_sif_http_client import (
    HostListenTransportError,
    host_listen_call,
)


def host_authority_declared() -> bool:
    """A container or explicit host authority must never use local guesses."""
    from .._env import getenv
    from .._lifecycle._in_sif_broker import is_in_sif

    return is_in_sif() or bool((getenv("LISTEN_BASE_URL", "") or "").strip())


def request_host_send(
    name: str,
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    timeout_s: float | None = None,
    opener: Callable | None = None,
) -> tuple[int, Any]:
    """Submit once to the host's existing ACL gate; validate nonfinal receipts."""
    body: dict[str, Any] = {"prompt": prompt}
    if model is not None or max_turns is not None:
        # This is the host route's authored options field, not an engine change.
        body["options"] = {}
        if model is not None:
            body["options"]["model"] = model
        if max_turns is not None:
            body["options"]["max_turns"] = max_turns
    path = f"/agents/{quote(name, safe='')}/send"
    status, response = host_listen_call(
        "POST", path, body=body, timeout_s=timeout_s, opener=opener
    )
    if status == 202:
        try:
            receipt = StatusCode.from_dict(response.get("status_code", {}))
            exchange_id = response.get("exchange_id")
        except (AttributeError, TypeError, ValueError, StatusError) as exc:
            raise HostListenTransportError(
                f"host send returned an invalid canonical 202 receipt: {exc}",
                url=path,
            ) from exc
        if receipt.kind != "http" or receipt.code != 202 or receipt.final:
            raise HostListenTransportError(
                "host send returned HTTP 202 without a non-final http/202 status_code",
                url=path,
            )
        if not is_exchange_id(exchange_id):
            raise HostListenTransportError(
                "host send returned HTTP 202 without a canonical xch_ exchange_id",
                url=path,
            )
    return status, response


def send_to_host(
    name: str,
    prompt: str,
    *,
    model: str | None = None,
    max_turns: int | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Keep queued, final and refused host answers distinct for MCP consumers."""
    try:
        status, body = request_host_send(
            name, prompt, model=model, max_turns=max_turns, timeout_s=timeout_s
        )
    except HostListenTransportError as exc:
        return {
            "status": "error",
            "agent": name,
            "error": str(exc),
            "diagnosis": {"registry_status": "unknown_host_authority"},
        }
    if not 200 <= status < 300:
        return {
            "status": "error",
            "agent": name,
            "http_status": status,
            "error": body.get("error", str(body))
            if isinstance(body, dict)
            else str(body),
            "response_metadata": body,
            "diagnosis": {
                "registry_status": "not_found"
                if status == 404
                else "unknown_host_authority"
            },
        }
    if not isinstance(body, dict):
        return {
            "status": "error",
            "agent": name,
            "error": "host send response is not a JSON object",
        }
    if status == 202:
        return {
            **body,
            "status": "pending",
            "agent": name,
            "http_status": status,
            "receipt": {
                **(
                    body.get("receipt") if isinstance(body.get("receipt"), dict) else {}
                ),
                "state": "pending",
                "final": False,
            },
        }
    return {
        "status": "ok",
        "agent": name,
        "http_status": status,
        "response_text": body.get("reply", ""),
        "response_metadata": body,
    }
