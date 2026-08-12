"""Rotate-only-when-stale gate for ``sac accounts refresh``.

Whether an account should be rotated at all, and — when it should not —
what to tell the caller. Extracted from ``_account_refresh.py``, where it
lived as a closure over the click command's parameters and so could only
be exercised by driving the whole CLI.

INCIDENT 2026-08-09 — why this module exists
--------------------------------------------
The gate used to be skipped for a single named account, on the reasoning
that an explicit request should always be honoured::

    if force or not do_all:      # <- the bug
        return True

But a refresh is not a read. It rotates the SINGLE-USE OAuth
refresh_token, and the server invalidates the previous one, so every
running agent still holding the old access token starts getting 401s —
on this host and on every other host that binds the same snapshot.

On 2026-08-09 one ``sac accounts refresh <name>``, run as a diagnostic on
the master host, stranded an entire host's agents. The timer's ``--all``
path had been correctly skipping that very account every ten minutes
("skipped; token still fresh (TTL >= 2h)"). The safe default was present
on the bulk path and absent on the path a human reaches for while
debugging — which is the path where the blast radius is least expected.

So the gate is now the SAME on every path, and ``--force`` is the single
documented way past it. Each function here takes its inputs explicitly
(including ``now``) so the gate can be unit-tested on its own — a gate
that can only be tested through the command it guards is the shape that
let the original bug ship.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone

def hours_left(expires_ms: int | None, now: float | None = None) -> float | None:
    """Signed hours until ``expires_ms``; ``None`` when there is no expiry.

    ``expires_ms`` is unix MILLISECONDS — the format claude-code writes
    into ``claudeAiOauth.expiresAt`` — and is ALWAYS read as milliseconds.

    It is tempting to auto-detect seconds-vs-milliseconds here the way
    ``_account.creds_sync`` does (``value > 1e12`` -> ms). Do not: that
    rule reads the exact value ``1e12`` as SECONDS, i.e. the year 33658,
    so a credential seeded with ``1_000_000_000_000`` flips from "expired
    in 2001" to "fresh for millennia" and the gate silently stops
    refreshing it. Three existing tests caught precisely that when this
    module was first extracted. This path has always been
    milliseconds-only; keep it that way.

    Positive means life remaining, negative means already expired.
    ``None`` (absent/unparseable expiry) is deliberately NOT collapsed
    into a number — the caller must decide what an unknown expiry means,
    and :func:`needs_refresh` treats it as "refresh", the safe direction.

    ``now`` is unix SECONDS, injected so tests need no wall clock.
    """
    if not isinstance(expires_ms, (int, float)) or isinstance(expires_ms, bool):
        return None
    now_s = now if now is not None else _time.time()
    return (expires_ms / 1000.0 - now_s) / 3600.0


def needs_refresh(
    expires_ms: int | None,
    *,
    force: bool,
    min_ttl_hours: float,
    now: float | None = None,
) -> bool:
    """True when this account's access token should be rotated now.

    The rule, identical for a single named account and for ``--all``:

    * ``force`` -> always rotate (the explicit override).
    * unknown/absent expiry -> rotate (we cannot prove it is fresh).
    * otherwise rotate only when less than ``min_ttl_hours`` remain.

    There is deliberately no ``do_all`` parameter. Callers used to pass
    one and short-circuit on it; that asymmetry WAS the 2026-08-09 bug, so
    the signature no longer offers a way to express it.
    """
    if force:
        return True
    remaining = hours_left(expires_ms, now)
    if remaining is None:
        return True
    return remaining < min_ttl_hours


def iso_ms(expires_ms: int | None) -> str | None:
    """Render a millisecond epoch as an ISO-8601 UTC string, or ``None``."""
    if not isinstance(expires_ms, int) or isinstance(expires_ms, bool):
        return None
    return datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc).isoformat()


def refusal_message(
    name: str,
    expires_at_iso: str | None,
    min_ttl_hours: float,
    *,
    is_pinned: bool,
) -> str:
    """Explain why a NAMED account was not rotated, and how to override.

    A named account held back by the gate is a REFUSAL TO ACT: the caller
    asked for a rotation and got none, so silence would read as success.
    The message names the account, its actual expiry, the threshold that
    held it back, the cost the rotation would have carried, and the exact
    flag that overrides — an error that only says what broke is half
    written.

    ``is_pinned`` says whether a running LOCAL agent is pinned to this
    account (see ``_account_refresh_skip._collect_pinned_running_accounts``).
    It only sharpens the wording: agents on OTHER hosts bind their own copy
    of the snapshot and are stranded by the rotation either way, so the
    absence of a local pin is never reported as "safe".
    """
    strands = (
        "running agents on this host are pinned to it and would be stranded"
        if is_pinned
        else (
            "no local agent is pinned to it right now, but agents on OTHER "
            "hosts binding this snapshot would still be stranded"
        )
    )
    return (
        f"refusing to refresh '{name}': its access token is still fresh "
        f"(expires {expires_at_iso or '(unknown)'}, threshold "
        f"--min-ttl-hours={min_ttl_hours:g}h).\n"
        f"A refresh ROTATES the single-use refresh_token, which invalidates "
        f"the access token every agent holding it is using — {strands}.\n"
        f"If you meant to rotate it anyway, re-run with --force."
    )


__all__ = ["hours_left", "iso_ms", "needs_refresh", "refusal_message"]
