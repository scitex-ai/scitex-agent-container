"""The CCT observations that run at agent start — both of them, in one place.

Two things want to happen at the same moment, for the same reason, and neither
may affect the start:

* the RAIL VERDICT (:func:`._cct_rail_alarm.check_cct_rail_at_start`) — is
  this agent mute and deaf on Telegram, and does anyone need paging?
* the OWNERSHIP LEDGER (:func:`._cct_token_ledger.record_token_claim_at_start`)
  — which bot did it take, written down so "who holds this one?" is a query.

The moment is load-bearing and is the reason they share a seam: both read the
agent's materialised ``$HOME/.env``, which ``runtime.start`` has just written
and which is precedence #1 of the token resolution. Run either one earlier and
it reports an agent as token-less that in fact has a token.

ORDER IS DELIBERATE. The rail verdict runs FIRST because it is the one that
can page a human about an outage; the ledger is bookkeeping nothing reads yet.
A slow or unreachable PostgreSQL must never sit in front of the page.

NEITHER RAISES. Each has its own never-raises contract in its own module, and
this function adds no new failure of its own — it is a call sequence, not a
try/except, precisely so that neither module's guarantee is quietly relocated
here where a reader of that module would not find it.
"""

from __future__ import annotations

from pathlib import Path


def observe_cct_at_start(config, *, dest: Path | None = None) -> tuple[str, str]:
    """Run both start-time CCT observations. Returns ``(rail, claim)``.

    ``rail`` is :func:`._cct_rail_alarm.alarm_cct_rail`'s outcome
    ("paged" / "recorded" / "clear" / "skipped"); ``claim`` is one of
    :mod:`._cct_token_ledger`'s ``CLAIM_*``. The caller in
    :mod:`.._lifecycle._start` discards both — they are returned so the pair
    is testable as a unit, not so anything branches on them.
    """
    from ._cct_rail_alarm import check_cct_rail_at_start
    from ._cct_token_ledger import record_token_claim_at_start

    rail = check_cct_rail_at_start(config, dest=dest)
    claim = record_token_claim_at_start(config, dest=dest)
    return rail, claim


__all__ = ["observe_cct_at_start"]
