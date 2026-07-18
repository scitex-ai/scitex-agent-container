"""C10 — sac's ``scitex_todo.hooks`` consumer: deliver card-events to agents.

The problem this closes
=======================
scitex-todo's board emits canonical card-events on the shared
``scitex_todo.hooks`` entry-point bus (its C5, already deployed): kinds
``commented`` / ``created`` / ``reassigned`` / ``status_changed`` /
``completed`` (and C6 adds ``committed`` / ``pushed`` / ``merged``). Each
event names the card + its owner / collaborators / subscribers.

sac REGISTERS this module's :func:`deliver_card_event` as a consumer in
that entry-point group (see this repo's ``pyproject.toml``
``[project.entry-points."scitex_todo.hooks"]``). When scitex-todo emits a
card-event, this consumer is invoked IN THE EMITTING PROCESS (the board's
process — the same host that runs ``sac listen``). It resolves the target
agent(s) from the event and delivers the notification to each.

Why HTTP to ``/v1/notify`` (not an in-process broker call)
==========================================================
The a2a inbox :class:`~scitex_agent_container.a2a._inbox_bus.Broker` that
a containerized agent subscribes to lives inside the ``sac listen``
daemon's process. This consumer runs in the BOARD's process, which is a
*different* process — it has no handle to that broker. So delivery goes
over loopback HTTP to the local ``sac listen`` daemon's ``POST
/v1/notify`` endpoint (:mod:`._notify`), which then publishes into the
agent's bus via the router. That endpoint is the ONE delivery seam: a
containerized agent SUBSCRIBES OUTBOUND to the daemon's SSE stream and
the daemon PUBLISHES down it, so a direct POST to the agent's own
``turn_url`` (``Connection refused`` for a container) is never attempted.

The bus is shared in BOTH directions
=====================================
sac ITSELF emits anomaly events on ``scitex_todo.hooks`` (the
liveness-tick producer in :mod:`._liveness_tick`). Those have a
``reason`` / ``severity`` shape, NOT one of the card-event kinds above.
:func:`deliver_card_event` FILTERS for the card-event kinds and IGNORES
everything else (incl. sac's own anomaly events), so the two flows never
cross-wire.

Degrade gracefully
===================
A delivery failure to one agent must NEVER crash the bus dispatch (the
producer loops over every consumer and a raise would break the chain for
the rest). Every failure is logged LOUD and swallowed; the function
returns the count of agents it delivered to.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# The card-event kinds this consumer recognises. Anything else — most
# importantly sac's OWN liveness-tick anomaly events on the same bus, plus
# any future kind we don't yet handle — is ignored (no-op) so the two
# flows sharing ``scitex_todo.hooks`` never cross-wire.
CARD_EVENT_KINDS = frozenset(
    {
        "commented",
        "created",
        "reassigned",
        "status_changed",
        "completed",
        # C6 git-lifecycle kinds.
        "committed",
        "pushed",
        "merged",
    }
)

# Env knobs (same names the rest of the sac client surface reads).
ENV_BASE_URL = "SAC_LISTEN_BASE_URL"
ENV_BEARER = "SAC_LISTEN_BEARER"
ENV_DISABLED = "SAC_CARD_EVENT_DELIVERY_DISABLED"

DEFAULT_BASE_URL = "http://127.0.0.1:7878"
_NOTIFY_PATH = "/v1/notify"
_POST_TIMEOUT_S = 5.0


def _event_kind(event: dict[str, Any]) -> str | None:
    """Return the event's kind string, tolerating a couple of spellings.

    The canonical field is ``kind``; some producers label it ``event`` or
    ``type``. Non-string / absent ⇒ ``None`` (the caller treats that as
    "unrecognized" and no-ops)."""
    for key in ("kind", "event", "type"):
        val = event.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _coerce_names(value: Any) -> list[str]:
    """Flatten an owner/collaborators/subscribers field into agent names.

    Accepts a bare string, a list of strings, or a list of dicts carrying
    a ``name`` / ``agent`` key (defensive against the producer shipping
    richer member objects). Anything else contributes nothing."""
    out: list[str] = []
    if isinstance(value, str):
        if value.strip():
            out.append(value.strip())
        return out
    if isinstance(value, dict):
        for key in ("name", "agent"):
            inner = value.get(key)
            if isinstance(inner, str) and inner.strip():
                out.append(inner.strip())
                break
        return out
    if isinstance(value, (list, tuple, set)):
        for item in value:
            out.extend(_coerce_names(item))
    return out


def resolve_targets(event: dict[str, Any]) -> list[str]:
    """Resolve the unique, order-preserving set of target agent names.

    Sources, in priority order (first occurrence wins for dedup):

    * the card OWNER — ``owner`` / ``assignee`` / ``owner_agent`` /
      ``agent`` (the field the producer uses for "whose card is this");
    * ``collaborators`` — agents working the card alongside the owner;
    * ``subscribers`` — agents watching the card.

    Each may be a string, a list of strings, or a list of ``{"name": …}``
    member dicts (see :func:`_coerce_names`). Self/empty names are
    dropped. The result drives one ``/v1/notify`` POST per agent."""
    ordered: list[str] = []
    for key in ("owner", "assignee", "owner_agent", "agent"):
        ordered.extend(_coerce_names(event.get(key)))
    ordered.extend(_coerce_names(event.get("collaborators")))
    ordered.extend(_coerce_names(event.get("subscribers")))

    seen: set[str] = set()
    unique: list[str] = []
    for name in ordered:
        if name and name not in seen:
            seen.add(name)
            unique.append(name)
    return unique


def _render_body(event: dict[str, Any], kind: str) -> str:
    """Render the notification text a target agent sees.

    Prefers an explicit human-facing field the producer may set
    (``body`` / ``message`` / ``text`` / ``comment``); otherwise builds a
    compact one-liner from the kind + card id so the agent always gets an
    actionable string (never an empty push)."""
    for key in ("body", "message", "text", "comment"):
        val = event.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    card_id = event.get("card_id") or event.get("card") or event.get("id")
    if card_id:
        return f"scitex-todo: card {card_id} {kind}"
    return f"scitex-todo: card {kind}"


def _resolve_base_url() -> str:
    base = os.environ.get(ENV_BASE_URL, "").strip()
    return (base or DEFAULT_BASE_URL).rstrip("/")


def _resolve_bearer() -> str | None:
    """Bearer for the local ``sac listen`` ``/v1/notify`` call.

    ``SAC_LISTEN_BEARER`` env first (what every other sac client reads);
    fall back to the host token file written by ``sac listen`` at startup.
    ``None`` only when neither is present — the POST then goes
    unauthenticated and the daemon answers 401, which surfaces loudly in
    the per-target log line rather than silently dropping."""
    env_bearer = os.environ.get(ENV_BEARER, "").strip()
    if env_bearer:
        return env_bearer
    # stx-allow: fallback (reason: token-file read is best-effort; a
    # missing/unreadable token degrades to an unauthenticated POST that
    # fails LOUD with 401, never a silent drop.)
    try:
        from .tokens import default_token_path, read_token

        return read_token(default_token_path())
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


def _post_notify(
    base_url: str,
    bearer: str | None,
    *,
    agent: str,
    body: str,
    card_id: str | None,
) -> bool:
    """POST one ``/v1/notify`` to the local daemon. Return True on 2xx.

    Uses stdlib ``urllib`` (no async, no extra deps) since the consumer
    runs in the producer's synchronous dispatch context. Any transport /
    HTTP error is logged LOUD and returns False — the caller continues to
    the next target (one bad delivery must not abort the rest)."""
    # Free-form provenance label — rendered into the notification's
    # ``meta.source`` bracket the operator reads. Not a lookup key and not
    # ACL-bearing (this POST authenticates with a bearer), so the rename is a
    # straight flip with no transitional tolerance needed.
    payload = {"agent": agent, "body": body, "from_agent": "scitex-cards"}
    if card_id:
        payload["card_id"] = card_id
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    req = urllib.request.Request(
        f"{base_url}{_NOTIFY_PATH}", data=data, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(status) < 300:
                return True
            logger.warning(
                "card_event_delivery: /v1/notify for agent=%s returned %s",
                agent,
                status,
            )
            return False
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except (
            Exception
        ):  # stx-allow: fallback (reason: error-body read is diagnostic only)
            pass
        logger.warning(
            "card_event_delivery: /v1/notify for agent=%s failed HTTP %s: %s",
            agent,
            exc.code,
            detail,
        )
        return False
    except (urllib.error.URLError, OSError, ValueError) as exc:
        logger.warning(
            "card_event_delivery: /v1/notify for agent=%s failed to reach %s: %s",
            agent,
            base_url,
            exc,
        )
        return False


def deliver_card_event(event: Any) -> int:
    """``scitex_todo.hooks`` consumer entry-point — deliver a card-event.

    Registered via ``pyproject.toml``
    ``[project.entry-points."scitex_todo.hooks"]``. Invoked by
    scitex-todo's board (and ignored-by-design when sac's own
    liveness-tick fires on the same bus). Contract:

    1. FILTER: a non-dict event, or one whose kind is not in
       :data:`CARD_EVENT_KINDS` (e.g. sac's anomaly events), is a
       no-op — returns ``0``.
    2. RESOLVE: target agents = owner + collaborators + subscribers
       (:func:`resolve_targets`).
    3. DELIVER: POST one ``/v1/notify`` per agent to the local ``sac
       listen`` daemon, which publishes into each agent's a2a bus so a
       subscribed (containerized) agent receives it.

    DEGRADE GRACEFULLY: never raises. A per-agent delivery failure is
    logged LOUD and skipped; the function returns the count of agents
    it delivered to. (A raise here would break the producer's dispatch
    loop for every consumer after this one.)

    NO import of ``scitex_todo`` — the contract is bus-only.
    """
    try:
        if os.environ.get(ENV_DISABLED, "") == "1":
            return 0
        if not isinstance(event, dict):
            return 0

        kind = _event_kind(event)
        if kind is None or kind not in CARD_EVENT_KINDS:
            # Unrecognized kind — most importantly sac's own liveness-tick
            # anomaly events (``reason``/``severity`` shape). No-op.
            return 0

        targets = resolve_targets(event)
        if not targets:
            logger.warning(
                "card_event_delivery: card-event kind=%s named no deliverable "
                "agent (owner/collaborators/subscribers all empty); skipping",
                kind,
            )
            return 0

        card_id = event.get("card_id") or event.get("card") or event.get("id")
        card_id = card_id if isinstance(card_id, str) and card_id.strip() else None
        body = _render_body(event, kind)

        base_url = _resolve_base_url()
        bearer = _resolve_bearer()

        return _deliver_to_targets(
            targets, base_url=base_url, bearer=bearer, body=body, card_id=card_id
        )
    except Exception as exc:  # stx-allow: fallback (reason: a consumer MUST NOT crash the producer's bus-dispatch loop — log loud and swallow)
        logger.warning(
            "card_event_delivery: unexpected failure handling card-event "
            "(%s); swallowed so the bus dispatch survives",
            exc,
        )
        return 0


def _deliver_to_targets(
    targets: Iterable[str],
    *,
    base_url: str,
    bearer: str | None,
    body: str,
    card_id: str | None,
) -> int:
    """POST to each target; tolerate per-target failure. Return success count."""
    delivered = 0
    for agent in targets:
        if _post_notify(base_url, bearer, agent=agent, body=body, card_id=card_id):
            delivered += 1
    return delivered


__all__ = [
    "CARD_EVENT_KINDS",
    "deliver_card_event",
    "resolve_targets",
]
