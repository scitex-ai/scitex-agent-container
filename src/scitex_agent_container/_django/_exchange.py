"""Read one accepted native admission receipt, never a conversation or task verdict."""

from __future__ import annotations

try:
    from django.core import signing
    from django.http import JsonResponse
    from django.views.decorators.cache import never_cache
    from django.views.decorators.http import require_GET
except ImportError as exc:
    raise ImportError(
        "SAC GUI dependencies are unavailable; install scitex-agent-container[gui]."
    ) from exc

from scitex_dev.status import StatusCode, StatusError, is_exchange_id

from ._authorization import can_control, resolve_identity, scope_rows
from ._control import redact
from ._remote import FleetUnavailableError, RemoteFleet, RemoteOperationError

_SALT = "sac-gui-admission-v1"


def accepted_receipt(identity: str, agent: str, exchange_id: str) -> str:
    """Bind only a canonical acceptance to its caller and exact recipient."""
    if not is_exchange_id(exchange_id):
        raise ValueError("invalid exchange id")
    return signing.dumps(
        {"identity": identity, "agent": agent, "exchange_id": exchange_id}, salt=_SALT
    )


@never_cache
@require_GET
def exchange_status(request, name: str, exchange_id: str):
    """Reauthorize and project bounded native status from the configured listener."""
    identity = resolve_identity(request)
    if not is_exchange_id(exchange_id):
        return JsonResponse(
            {"state": "refused", "message": "Invalid admission receipt."}, status=400
        )
    try:
        binding = signing.loads(
            request.headers.get("X-SAC-Exchange-Receipt", ""), salt=_SALT, max_age=3600
        )
    except signing.BadSignature:
        return JsonResponse(
            {"state": "refused", "message": "Admission receipt is invalid or expired."},
            status=403,
        )
    if binding != {"identity": identity, "agent": name, "exchange_id": exchange_id}:
        return JsonResponse(
            {
                "state": "refused",
                "message": "Admission receipt does not match this request.",
            },
            status=403,
        )
    fleet = RemoteFleet.from_environment()
    try:
        row = next(
            (
                r
                for r in scope_rows(fleet.list_all(), identity)
                if r.get("name") == name
            ),
            None,
        )
    except (FleetUnavailableError, RemoteOperationError, ValueError, TypeError):
        return JsonResponse(
            {
                "state": "unknown",
                "message": "Admission status is unavailable; do not resend.",
            },
            status=502,
        )
    if (
        row is None
        or row.get("scope") == "cross-host"
        or not can_control(identity, cross_host=False, request=request, agent=name)
    ):
        return JsonResponse(
            {
                "state": "refused",
                "message": "You are not authorized for this admission receipt.",
            },
            status=403,
        )
    try:
        payload = fleet.read_exchange(name, exchange_id, timeout=5)
        if payload.get("exchange_id") != exchange_id:
            raise ValueError("wrong exchange")
        native = StatusCode.from_dict(payload.get("status_code", {}))
        if native.kind != "http":
            raise ValueError("unsupported native status vocabulary")
    except (
        FleetUnavailableError,
        RemoteOperationError,
        OSError,
        ValueError,
        TypeError,
        AttributeError,
        StatusError,
    ):
        return JsonResponse(
            {
                "state": "unknown",
                "message": "Admission status is unavailable; do not resend.",
            },
            status=502,
        )
    state = (
        "pending"
        if not native.final
        else "received"
        if native.code == 200
        else "failed"
        if not native.ok
        else "unknown"
    )
    message = {
        "pending": "Agent admission is still pending.",
        "received": "Received by agent. Model task completion is separate.",
        "failed": "Agent admission failed.",
        "unknown": "Native admission is unconfirmed; do not resend.",
    }[state]
    return JsonResponse(
        {
            "exchange_id": exchange_id,
            "state": state,
            "final": native.final,
            "code": native.code,
            "message": message,
            "native_message": redact(native.message)[:240],
        }
    )
