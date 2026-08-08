"""One place to say "this publish reached nobody" (2026-08-08).

:meth:`a2a._inbox_bus.Broker.publish` returns the number of live subscribers
that accepted the event. Most callers threw that number away, so a publish to
an agent with no attached inbox adapter was indistinguishable from one that
landed — the caller carried on as if it had delivered.

Operator, 2026-08-08: 「送ったつもりで黙って失敗はありえないです」 — a send
believed delivered that silently failed is never acceptable.

WHY ZERO IS ``INFO`` AND NOT AN ERROR
Every caller in this package persists the event to ``channel_events`` BEFORE
publishing, and a fresh subscriber replays its undelivered rows on connect
(:func:`_state.state_db_channel.list_undelivered`). So a zero here is not lost
data — it is "nobody was listening at this instant, the row is waiting". An
agent that is simply stopped is a normal and expected state; emitting an ERROR
per publish per stopped agent would train the reader to skip exactly the line
that matters on the day it means something. The defect was never that zero
occurs. It was that zero and success looked identical.

The success path stays SILENT for the same reason: a line per publish per
agent is its own kind of noise.

Callers that can hand the count back to the SENDER should do that too — see
``_listen/_node_channel.py::node_message_send``, which returns
``delivered_subscriber_count`` in its JSON response. This helper is for the
publishes with no such return path: receiver-side notifications, synthetic
frames, and scheduled nudges, where a log line is the only surface there is.
"""

from __future__ import annotations

import logging
from typing import Any

__all__ = ["report_zero_delivery"]


def report_zero_delivery(
    log: logging.Logger,
    *,
    target: str,
    what: str,
    delivered: Any,
    row_id: Any = None,
) -> bool:
    """Log when a publish reached no subscriber. Return whether it reported.

    ``delivered`` is the return value of :meth:`Broker.publish` — the count of
    subscribers that accepted the event. Anything truthy means it landed and
    this is a no-op; ``0`` (and ``None``, for a caller whose publish could not
    report at all) is the case worth a line.

    ``what`` names the KIND of frame in reader-facing words ("ACL-deny
    notification", "approval prompt"), because by the time someone reads this
    line the interesting question is which notification the agent missed, not
    which source line emitted it.

    ``row_id`` is the ``channel_events`` row id when the caller has one. It is
    what makes the line actionable: it says the event is durable and names the
    row to look at. Omit it only when there genuinely is no persisted row.

    Returns ``True`` iff a line was emitted, so callers and tests can assert on
    the outcome without parsing log records.
    """
    if delivered:
        return False
    if row_id is None:
        log.info(
            "a2a: %s for %r reached NO subscriber (delivered=%r)",
            what,
            target,
            delivered,
        )
    else:
        log.info(
            "a2a: %s for %r reached NO subscriber (delivered=%r); "
            "channel_events row %s is durable and replays on next connect",
            what,
            target,
            delivered,
            row_id,
        )
    return True
