"""Strict client for the host listener's authoritative fleet inventory."""

from __future__ import annotations

from typing import Callable

from pydantic import ValidationError

from .._inventory import FleetInventoryResponse
from .._lifecycle._in_sif_http_client import (
    HostListenTransportError,
    host_listen_call,
)


class FleetInventoryAuthorityError(RuntimeError):
    """No compatible authoritative inventory response was available."""


def request_fleet_inventory(
    *,
    capability: str | None = None,
    machine: str | None = None,
    base_url: str | None = None,
    bearer: str | None = None,
    opener: Callable | None = None,
) -> FleetInventoryResponse:
    query: list[tuple[str, str]] = []
    from urllib.parse import urlencode

    if capability:
        query.append(("capability", capability))
    if machine:
        query.append(("machine", machine))
    path = "/v1/fleet/inventory"
    if query:
        path += "?" + urlencode(query)
    try:
        status, body = host_listen_call(
            "GET",
            path,
            base_url=base_url,
            bearer=bearer,
            opener=opener,
        )
    except HostListenTransportError as exc:
        raise FleetInventoryAuthorityError(
            f"authoritative host fleet inventory unavailable: {exc}"
        ) from exc
    if status != 200:
        raise FleetInventoryAuthorityError(
            "authoritative host fleet inventory rejected the request: "
            f"HTTP {status} ({body!r})"
        )
    try:
        return FleetInventoryResponse.model_validate(body)
    except ValidationError as exc:
        raise FleetInventoryAuthorityError(
            "authoritative host fleet inventory response is missing or "
            f"incompatible with sac.fleet-inventory/v1: {exc}"
        ) from exc


__all__ = ["FleetInventoryAuthorityError", "request_fleet_inventory"]
