"""Agent control surfaces: message/steer + pending-choice response.

Card sac-agent-activity-timeline-dashboard-20260917. The card is explicit: the
GUI must use EXISTING SAC APIs only, own no alternate state, and never touch
tmux directly. The APIs are the agent's own turn bridge:

  * turn / steer  POST /v1/turn  (or /agents/<name>/{turn,send,message:send})
                  body {"text": "..."} — the SAME verb the fleet's A2A send
                  uses, with the same two accepted body shapes.
  * UI control    POST /v1/control  body {"key": "Enter|Escape|C-c"} — a
                  POSITIONAL KEY, not a command. The bridge accepts exactly
                  those three and 400s anything else, so the GUI must not
                  invent a fourth.

Why this module exists rather than a raw POST from the view: two properties
must hold and both are easy to get wrong inline.

1. **Idempotency.** A double-submitted message would inject the text TWICE into
   a live agent's turn — the worst failure this surface can cause. Every
   mutation carries a dispatch id, and a repeated submit with the same id is
   reported as a replay rather than delivered again.
2. **No secret or prompt leakage.** Text sent to an agent can contain anything;
   the confirmation the GUI renders back must never echo a credential, and the
   audit trail must not become a transcript.

The GUI holds NO queue and NO pending-message store: a delivery is either
reported as delivered by the bridge, or reported as failed. Nothing is parked
in the dashboard waiting to be retried behind the operator's back.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

#: The complete set of UI-control keys the SAC bridge accepts. Any other value
#: is refused by the bridge with a 400, so offering a fourth here would be a
#: GUI that renders a button the backend will always reject.
CONTROL_KEYS = ("Enter", "Escape", "C-c")

#: A message is bounded: the bridge and the agent's turn both have to carry it.
MAX_MESSAGE_CHARS = 4000

_SECRET_RE = re.compile(
    r"(?:"
    r"sk-[A-Za-z0-9_\-]{16,}"
    r"|Bearer\s+[A-Za-z0-9_\-\.=]{16,}"
    r"|(?:api[_-]?key|token|password|passwd|secret)\s*[=:]\s*[\"']?[A-Za-z0-9_\-\.=]{8,}"
    r"|[A-Za-z0-9+/]{40,}={0,2}"
    r")",
    re.IGNORECASE,
)

_DELIVERY_TIMEOUT = 120.0


@dataclass(frozen=True)
class DeliveryResult:
    """What the bridge said about one delivery attempt."""

    delivered: bool
    state: str          # delivered | replay | failed | refused
    message: str
    dispatch_id: str
    mode: str = "turn"

    def as_dict(self) -> dict:
        return {
            "delivered": self.delivered,
            "state": self.state,
            "message": self.message,
            "dispatch_id": self.dispatch_id,
            "mode": self.mode,
        }


class _Replay:
    """Process-local record of dispatch ids already delivered.

    Deliberately tiny and bounded: this exists to stop a double-click injecting
    a message twice, NOT to become a message store. An unknown id is delivered;
    a known id is reported as a replay and NOT re-sent. The dashboard keeps no
    transcript.
    """

    _seen: dict[str, str] = {}
    _MAX = 512

    @classmethod
    def remember(cls, dispatch_id: str) -> None:
        if len(cls._seen) >= cls._MAX:
            for key in list(cls._seen)[: cls._MAX // 4]:
                cls._seen.pop(key, None)
        cls._seen[dispatch_id] = "delivered"

    @classmethod
    def seen(cls, dispatch_id: str) -> bool:
        return dispatch_id in cls._seen

    @classmethod
    def forget(cls, dispatch_id: str) -> None:
        cls._seen.pop(dispatch_id, None)


def redact(text: str) -> str:
    """Scrub obvious secrets before text is echoed back to a browser."""
    if not isinstance(text, str):
        return ""
    return _SECRET_RE.sub("[REDACTED]", text)


def new_dispatch_id() -> str:
    return uuid.uuid4().hex


def exactly_one_of(*values) -> bool:
    """True when exactly one argument is truthy — the form guards use this."""
    return sum(1 for v in values if v) == 1


def _bridge_base(turn_url: str | None) -> str:
    """The agent's own bridge base URL from its published ``turn_url``.

    ``turn_url`` already names the agent's real placement
    (``http://<host>:<port>/v1/turn``), so the GUI dials the SAME endpoint the
    fleet's A2A send dials — it does not construct a host/port of its own.
    """
    if not isinstance(turn_url, str) or not turn_url.strip():
        return ""
    base = turn_url.strip().rstrip("/")
    for suffix in ("/v1/turn", "/v1/control"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
    return base


def send_message(
    turn_url: str | None,
    *,
    text: str,
    dispatch_id: str | None = None,
    timeout: float = _DELIVERY_TIMEOUT,
) -> DeliveryResult:
    """Deliver a message/steer to an agent through its own turn bridge.

    Idempotent by ``dispatch_id``: a repeat of an id already delivered is
    reported as ``replay`` and NOT sent again, because a duplicate here injects
    the text into a live agent's session twice.
    """
    dispatch = dispatch_id or new_dispatch_id()
    if not isinstance(text, str) or not text.strip():
        return DeliveryResult(False, "refused", "a message needs non-empty text", dispatch)
    if len(text) > MAX_MESSAGE_CHARS:
        return DeliveryResult(
            False, "refused", f"message exceeds {MAX_MESSAGE_CHARS} characters", dispatch
        )
    base = _bridge_base(turn_url)
    if not base:
        return DeliveryResult(
            False, "refused", "this agent publishes no turn endpoint", dispatch
        )

    if _Replay.seen(dispatch):
        return DeliveryResult(
            True, "replay", "already delivered (duplicate submit ignored)", dispatch
        )

    body = json.dumps({"text": text, "dispatch_id": dispatch}).encode("utf-8")
    request = Request(
        f"{base}/v1/turn",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error", "")
        except (ValueError, OSError):
            detail = ""
        return DeliveryResult(
            False, "failed", redact(detail or f"HTTP {exc.code}"), dispatch
        )
    except (URLError, OSError, ValueError) as exc:
        return DeliveryResult(False, "failed", redact(str(exc)), dispatch)

    _Replay.remember(dispatch)
    delivered = bool(payload.get("delivered", True)) if isinstance(payload, dict) else True
    state = "delivered" if delivered else "failed"
    if not delivered:
        _Replay.forget(dispatch)
    return DeliveryResult(delivered, state, "message delivered", dispatch)


def send_control_key(
    turn_url: str | None,
    *,
    key: str,
    dispatch_id: str | None = None,
    timeout: float = _DELIVERY_TIMEOUT,
) -> DeliveryResult:
    """Send ONE of the three SAC UI-control keys to an agent's bridge.

    A key is positional input, not a command: ``Enter`` submits, ``Escape``
    dismisses, ``C-c`` interrupts. Anything else is refused HERE (the bridge
    would 400 it anyway) so the GUI never renders a control the backend cannot
    honour.
    """
    dispatch = dispatch_id or new_dispatch_id()
    if key not in CONTROL_KEYS:
        return DeliveryResult(
            False,
            "refused",
            f"unsupported control key {key!r}; SAC accepts {'/'.join(CONTROL_KEYS)}",
            dispatch,
            mode="control",
        )
    base = _bridge_base(turn_url)
    if not base:
        return DeliveryResult(
            False, "refused", "this agent publishes no control endpoint", dispatch, mode="control"
        )
    if _Replay.seen(f"{dispatch}:{key}"):
        return DeliveryResult(
            True, "replay", "already sent (duplicate submit ignored)", dispatch, mode="control"
        )

    body = json.dumps({"key": key}).encode("utf-8")
    request = Request(
        f"{base}/v1/control",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
    except HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode("utf-8", errors="replace")).get("error", "")
        except (ValueError, OSError):
            detail = ""
        return DeliveryResult(
            False, "failed", redact(detail or f"HTTP {exc.code}"), dispatch, mode="control"
        )
    except (URLError, OSError, ValueError) as exc:
        return DeliveryResult(False, "failed", redact(str(exc)), dispatch, mode="control")

    _Replay.remember(f"{dispatch}:{key}")
    delivered = bool(payload.get("delivered", True)) if isinstance(payload, dict) else True
    return DeliveryResult(
        delivered,
        "delivered" if delivered else "failed",
        f"control key {key} sent",
        dispatch,
        mode="control",
    )


__all__ = [
    "CONTROL_KEYS",
    "MAX_MESSAGE_CHARS",
    "DeliveryResult",
    "exactly_one_of",
    "new_dispatch_id",
    "redact",
    "send_control_key",
    "send_message",
]
