"""Periodic-drive injection port for external hooks (CCT delivery).

The periodic-drive lane (:mod:`._periodic_drive`) already drives turns
into agent inboxes every tick via its ``emit`` seam — but only the
in-process listen loop could call it. ``claude-code-telegrammer`` (CCT)
delivery runs OUTSIDE that process (hook scripts), and when a Telegram
send goes out but never lands (single-check Clock, ``connecting...``)
the recovery turn has to reach the agent through the SAME inbox path —
not a second ad-hoc channel.

This module is that port: a narrow, stable surface a hook calls to
inject a CCT-delivery turn exactly as if the drive had emitted it:

* :func:`build_cct_delivery_turn` renders the turn body MECHANICALLY
  from a delivery summary string (typically
  :meth:`.._delivery._redeliver.RedeliveryReport.summary_text`) —
  never hand-written, per operator doctrine;
* :func:`inject_cct_turn` builds + passes the envelope to an ``emit``
  callable with the same ``(envelope) -> None`` shape
  :func:`._periodic_drive.sweep` takes, so the loop's inbox writer and
  the hook's writer are interchangeable;
* ``CCT_DRIVE_KIND`` (``"cct_delivery"``) is deliberately DISTINCT from
  :data:`._periodic_drive.ENVELOPE_KIND` so the recipient's prompt
  logic matches delivery-recovery turns separately from routine nudges.

Dependency direction: this module imports ONLY :mod:`._periodic_drive`
(envelope type). It takes the delivery summary as a plain string, so it
never imports the delivery package — no cycle, and a shell hook can
call it with JSON/text it already holds.

On-demand injection is NOT gated by ``SAC_PERIODIC_DRIVE_DISABLED``:
that env var pauses the periodic tick, not an explicit hook call.
"""

from __future__ import annotations

import time
from typing import Callable

from ._periodic_drive import PeriodicDriveEnvelope

__all__ = [
    "CCT_DRIVE_KIND",
    "build_cct_delivery_turn",
    "inject_cct_turn",
]

#: Envelope kind for CCT-delivery recovery turns. Distinct from the
#: routine ``periodic_drive`` kind so recipients match on it separately.
CCT_DRIVE_KIND = "cct_delivery"


def build_cct_delivery_turn(
    agent_name: str,
    delivery_summary: str,
    *,
    now: float | None = None,
) -> PeriodicDriveEnvelope:
    """Render a CCT-delivery recovery turn for ``agent_name``.

    Pure: no disk, no git, no inbox — the caller supplies the already-
    rendered ``delivery_summary`` and, optionally, ``now``. Lets the
    test suite assert the shape without a live daemon.
    """
    when = now if now is not None else time.time()
    body = (
        f"[sac cct-delivery — {agent_name}]\n"
        f"\n"
        f"## Delivery report (mechanical — confirm, retry, route-switch log)\n"
        f"{delivery_summary}\n"
        f"\n"
        f"## Action\n"
        f"Confirm whether the payload above reached you. If it did not, "
        f"report what you actually received (or that nothing arrived) so "
        f"the sender can retry or switch route. Then continue your mission.\n"
    )
    return PeriodicDriveEnvelope(
        agent_name=agent_name,
        kind=CCT_DRIVE_KIND,
        body=body,
        generated_at=when,
    )


def inject_cct_turn(
    agent_name: str,
    delivery_summary: str,
    *,
    emit: Callable[[PeriodicDriveEnvelope], None],
    now: float | None = None,
) -> PeriodicDriveEnvelope:
    """Build the turn and hand it to ``emit`` — the hook entry point.

    ``emit`` has the same shape as the ``emit`` seam of
    :func:`._periodic_drive.sweep` (synchronous side-effect). Returns
    the envelope emitted so the hook can log / assert on it.
    """
    envelope = build_cct_delivery_turn(
        agent_name, delivery_summary, now=now
    )
    emit(envelope)
    return envelope


# EOF
