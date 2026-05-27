"""``sac fleet notify`` — agent→lead typed push (ADR-0013 Phase 1).

CLI front for :func:`scitex_agent_container._state.lead_inbox.push_to_lead`.
An agent (or its Stop hook, or an operator running ad hoc) publishes a
typed event — ``done`` / ``blocker`` / ``status`` — to the lead's
``sac listen`` inbox over the existing A2A ``message:send`` route.

Exit codes:

* 0  — pushed successfully; server returned a 2xx.
* 2  — operator/usage error (no ``lead:`` block, bad ``--kind``, no
       ``--from-agent`` resolvable, ...). Click raises ``UsageError``
       which renders exit 2.
* 1  — push failed (lead unreachable, 403 ACL deny, missing
       peer-token, malformed body). The error is printed to stderr
       loud — no silent retry, no silent fallback.

The default ``from_agent`` is resolved from the ``SAC_NAME`` env var
(set inside every agent container) so the common in-container call
site reduces to ``sac fleet notify done --summary "..."``. Outside a
container (operator on the lead host running a one-shot test) the
``--from-agent`` flag is required and surfaces a loud usage error if
omitted.

The push helper itself owns the loudness contract; this CLI is a
thin click wrapper that turns ``LeadInboxError`` into an exit code
plus stderr line.
"""

from __future__ import annotations

import json
import os
from typing import Any

import click

from .._state.lead_inbox import (
    LEAD_EVENT_KINDS,
    LeadInboxError,
    build_lead_envelope,
    push_to_lead,
    resolve_lead,
)

__all__ = ["fleet_notify"]


def _resolve_from_agent(explicit: str | None) -> str:
    """Pick the sender identity for the lead-push envelope.

    Precedence: explicit ``--from-agent`` flag wins; otherwise the
    in-container ``SAC_NAME`` env var (set by every agent's runtime
    bootstrap). Empty / unset → usage error so the push never lands
    at the lead with ``from_agent="unknown"``.
    """
    if explicit:
        return explicit
    env = os.environ.get("SAC_NAME", "").strip()
    if env:
        return env
    raise click.UsageError(
        "no sender identity: pass --from-agent NAME, or run this CLI "
        "inside an agent container where SAC_NAME is set."
    )


@click.command("notify")
@click.argument(
    "kind",
    type=click.Choice(LEAD_EVENT_KINDS, case_sensitive=True),
)
@click.option(
    "--summary",
    required=True,
    help="One-line human-readable summary (lands in message.parts[0].text).",
)
@click.option(
    "--detail",
    default=None,
    help="Optional extended payload (rationale, error text, full report).",
)
@click.option(
    "--from-agent",
    default=None,
    help="Sender identity for ACL gating. Defaults to $SAC_NAME.",
)
@click.option(
    "--conversation-id",
    default=None,
    help="Optional thread id for replies / correlation.",
)
@click.option(
    "--timeout-seconds",
    default=15.0,
    show_default=True,
    type=float,
    help="HTTP timeout. Short by design — a long timeout would mask "
    "a wedged lead listen.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Build + print the A2A envelope without POSTing.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Print the server's response (or the dry-run envelope) as "
    "compact JSON on stdout.",
)
def fleet_notify(
    kind: str,
    summary: str,
    detail: str | None,
    from_agent: str | None,
    conversation_id: str | None,
    timeout_seconds: float,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Push a typed event (``done`` / ``blocker`` / ``status``) to the lead.

    \b
    Examples:
      $ sac fleet notify done --summary "PR #224 merged"
      $ sac fleet notify blocker --summary "creds expired" --detail "..."
      $ sac fleet notify status --summary "phase 2 of 4 complete" --json
    """
    sender = _resolve_from_agent(from_agent)

    if dry_run:
        # Envelope build is the same code path the wire push uses; a
        # dry-run is just "skip the POST". Useful for skill docs and
        # for confirming the lead address resolves before the agent
        # tries to deliver real events. ``resolve_lead()`` raises
        # ``LeadInboxError`` when no ``lead:`` block is configured;
        # surface that as exit 1, matching the wire path's behaviour.
        try:
            envelope = build_lead_envelope(
                kind=kind,
                summary=summary,
                from_agent=sender,
                detail=detail,
                conversation_id=conversation_id,
            )
            lead = resolve_lead()
        except LeadInboxError as exc:
            click.echo(f"error: {exc}", err=True)
            raise SystemExit(1) from exc
        report = {
            "dry_run": True,
            "lead": {
                "name": lead.name,
                "host": lead.host,
                "a2a_port": lead.a2a_port,
            },
            "envelope": envelope,
        }
        click.echo(json.dumps(report) if as_json else json.dumps(report, indent=2))
        return

    try:
        payload: dict[str, Any] = push_to_lead(
            kind=kind,
            summary=summary,
            from_agent=sender,
            detail=detail,
            conversation_id=conversation_id,
            timeout_s=timeout_seconds,
        )
    except LeadInboxError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc

    if as_json:
        click.echo(json.dumps(payload))
    else:
        msg_id = payload.get("msg_id", "?")
        delivered = payload.get("delivered_subscriber_count", "?")
        click.echo(
            f"pushed kind={kind} from={sender} msg_id={msg_id} "
            f"delivered_subscriber_count={delivered}"
        )
