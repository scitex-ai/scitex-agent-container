"""Agent→lead push helpers (ADR-0013 Phase 1).

Phase 1 of the central fleet registry closes the *agent→lead push gap*:
today the lead polls and agents have no way to notify the lead. This
module is the agent-side helper. The lead is just another A2A node —
it runs the same ``sac listen`` HTTP control plane as every agent, and
its inbox is the existing ``POST /agents/<name>/message:send`` route
(see :mod:`scitex_agent_container._listen._node_channel`). The lead's
identity is declared in ``config.yaml`` under the ``lead:`` block (see
:class:`scitex_agent_container._state.host_config.LeadConfig`).

Two responsibilities:

* :func:`build_lead_envelope` — construct the A2A ``message/send``
  JSON-RPC body for a typed ``kind`` event (``done`` / ``blocker`` /
  ``status``). Pure function, no I/O. Useful in tests and for the CLI
  ``--dry-run`` path.
* :func:`push_to_lead` — POST the envelope to the lead's listen via
  urllib. Authenticates with the lead host's bearer pulled from the
  per-host ``peer-tokens/<host>.token`` registry — same mechanism the
  cross-host A2A forwarder uses (see
  :mod:`scitex_agent_container._listen.peer_tokens`).

Failure surfaces are sharp — no silent fallbacks (handoff §0 / ADR-0011
loudness contract, extended to this push path by ADR-0013 Phase 1):

* Missing ``lead:`` block in config       → :class:`LeadInboxError`
* Unknown event kind                       → :class:`LeadInboxError`
* Missing ``peer-tokens/<host>.token``     → :class:`LeadInboxError`
                                              (wraps ``PeerTokenError``)
* Transport failure (refused, timeout)     → :class:`LeadInboxError`
* Lead returns non-2xx                     → :class:`LeadInboxError`
                                              (includes status + body)
* Lead returns malformed JSON              → :class:`LeadInboxError`

The kind allow-list is enforced at envelope-mint time. The receiving
``node_message_send`` route reads ``params.metadata.kind`` verbatim (no
allow-list at the receiver), so a typo on the sender side never reaches
the channel-events table — a small but loud guarantee that the lead's
inbox only sees the kinds we intend.

Phase 2 (propagating registry, ``sac fleet status``, liveness eviction)
is explicitly out of scope here — this module is a thin push helper,
not a registry implementation.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Literal

from .host_config import Config, LeadConfig, load

__all__ = [
    "LEAD_EVENT_KINDS",
    "LeadConfig",
    "LeadEventKind",
    "LeadInboxError",
    "build_lead_envelope",
    "push_to_lead",
    "resolve_lead",
]

log = logging.getLogger(__name__)

LEAD_EVENT_KINDS: tuple[str, ...] = ("done", "blocker", "status")
LeadEventKind = Literal["done", "blocker", "status"]


class LeadInboxError(RuntimeError):
    """Raised when an agent→lead push cannot be completed.

    Loud-by-design: this push is how the lead learns an agent finished
    (or is blocked, or is reporting status). A silent failure would
    leave the lead believing the agent is still working when it is not.
    Every failure path in this module surfaces as a ``LeadInboxError``
    with an actionable message.
    """


def resolve_lead(*, config_path: Path | None = None) -> LeadConfig:
    """Load the lead-inbox target from ``config.yaml``.

    Returns the :class:`LeadConfig` declared under ``lead:``. Missing
    block is a loud error — Phase 1 has no "auto-discover the lead"
    behaviour, deliberately, so the operator's choice of lead is
    explicit and reviewable.

    ``config_path`` is forwarded to :func:`host_config.load` for tests
    that point production at a tmp file; production callers leave it
    unset and rely on the SciTeX local-state cascade.
    """
    cfg: Config = load(config_path)
    if cfg.lead is None:
        source = cfg.source_path or "<no config file resolved>"
        raise LeadInboxError(
            f"no lead inbox configured. Add a 'lead:' block to "
            f"{source} with name/host/a2a_port — see "
            f"scitex_agent_container._state.host_config.LeadConfig "
            f"for the schema."
        )
    return cfg.lead


def build_lead_envelope(
    *,
    kind: str,
    summary: str,
    from_agent: str,
    detail: str | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Mint the A2A ``message/send`` body for an agent→lead push event.

    The envelope shape matches the existing
    ``node_message_send`` route (see
    :mod:`scitex_agent_container._listen._node_channel`):

    * ``method`` is ``"message/send"`` (one of the three values the
      route accepts).
    * ``params.message.parts[0].text`` carries the human-readable
      ``summary`` so a subscriber that does not understand the
      typed-event extension still sees a sensible line.
    * ``params.metadata.kind`` is the typed event kind — surfaced into
      the published event so consumers can filter (``done`` /
      ``blocker`` / ``status``).
    * ``params.metadata.from_agent`` is the sender identity the ACL
      check gates on. Required (the empty-sender deny path would
      otherwise reject the push at the lead).
    * ``params.metadata.detail`` carries an optional extended payload
      (rationale, error text, full report). Stored as a string so the
      receiver does not need to know how to parse JSON-in-JSON.

    Pure function, no I/O. Loud on bad ``kind`` (allow-list of
    :data:`LEAD_EVENT_KINDS`) — a typo MUST fail at mint time, before
    it can land in the lead's inbox under the wrong label.
    """
    if not kind or kind not in LEAD_EVENT_KINDS:
        raise LeadInboxError(
            f"unsupported event kind {kind!r}; expected one of "
            f"{list(LEAD_EVENT_KINDS)}"
        )
    if not isinstance(from_agent, str) or not from_agent.strip():
        raise LeadInboxError(
            "from_agent is required (non-empty string) so the lead "
            "knows who reported the event"
        )
    if not isinstance(summary, str):
        raise LeadInboxError(
            f"summary must be a string (got {type(summary).__name__})"
        )

    metadata: dict[str, Any] = {
        "kind": kind,
        "from_agent": from_agent,
    }
    if detail is not None:
        metadata["detail"] = detail
    if conversation_id is not None:
        metadata["conversation_id"] = conversation_id

    return {
        "jsonrpc": "2.0",
        "id": uuid.uuid4().hex,
        "method": "message/send",
        "params": {
            "message": {
                "message_id": uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": summary}],
            },
            "metadata": metadata,
        },
    }


def push_to_lead(
    *,
    kind: str,
    summary: str,
    from_agent: str,
    detail: str | None = None,
    conversation_id: str | None = None,
    lead: LeadConfig | None = None,
    config_path: Path | None = None,
    peer_tokens_dir: Path | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    """POST a typed event to the lead's ``sac listen`` inbox.

    Resolves the lead address (``lead`` arg, else ``config.yaml``),
    pulls the lead-host bearer from the per-host ``peer-tokens/``
    registry, builds the A2A envelope, and POSTs to
    ``http://<lead.host>:<lead.a2a_port>/agents/<lead.name>/message:send``.

    Returns the server's JSON response body so the caller can log /
    inspect ``msg_id`` and ``delivered_subscriber_count``. Raises
    :class:`LeadInboxError` for every failure mode — there is no
    silent fallback to a no-op success.

    Args:
        kind: One of :data:`LEAD_EVENT_KINDS`.
        summary: Human-readable one-line summary.
        from_agent: Sender identity for ACL gating at the lead.
        detail: Optional extended payload (rationale / error).
        conversation_id: Optional thread id for replies.
        lead: Override the configured :class:`LeadConfig` (tests).
        config_path: Override ``config.yaml`` location (tests).
        peer_tokens_dir: Override the peer-tokens directory (tests).
        timeout_s: HTTP timeout. Defaults to 15 s — agents push small
            envelopes; a long timeout would mask a wedged lead listen.
    """
    # Import here to keep module-level imports light and to mirror the
    # pattern used by the cross-host forwarder (which also defers the
    # peer-tokens import to call time).
    from .._listen.peer_tokens import PeerTokenError, read_peer_token

    target = lead if lead is not None else resolve_lead(config_path=config_path)

    try:
        bearer = read_peer_token(
            peer_host=target.host,
            tokens_dir=peer_tokens_dir,
        )
    except PeerTokenError as exc:
        raise LeadInboxError(f"cannot push to lead inbox: {exc}") from exc

    envelope = build_lead_envelope(
        kind=kind,
        summary=summary,
        from_agent=from_agent,
        detail=detail,
        conversation_id=conversation_id,
    )

    url = (
        f"http://{target.host}:{target.a2a_port}"
        f"/agents/{target.name}/message:send"
    )
    body = json.dumps(envelope).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8")
        except OSError:
            err_body = ""
        raise LeadInboxError(
            f"lead inbox at {url} returned HTTP {exc.code}: "
            f"{err_body or exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LeadInboxError(
            f"lead inbox at {url} unreachable: {exc.reason}"
        ) from exc
    except TimeoutError as exc:
        raise LeadInboxError(
            f"lead inbox at {url} timed out after {timeout_s:.1f}s"
        ) from exc

    if status < 200 or status >= 300:
        raise LeadInboxError(
            f"lead inbox at {url} returned HTTP {status}: {raw}"
        )

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LeadInboxError(
            f"lead inbox at {url} returned non-JSON body: {raw!r}"
        ) from exc

    if not isinstance(payload, dict):
        raise LeadInboxError(
            f"lead inbox at {url} returned non-object body: {payload!r}"
        )

    log.info(
        "pushed lead-inbox event kind=%s from=%s msg_id=%s",
        kind,
        from_agent,
        payload.get("msg_id"),
    )
    return payload
