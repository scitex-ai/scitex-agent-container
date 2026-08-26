"""The credential-distribution run's PAUSE partition, and how it says so.

OPERATOR REQUEST 2026-08-26. He stops and restarts Anthropic
subscriptions deliberately, watching quota, and asked for one thing
about the gap between: 「その休止の間も失敗しないようにしてほしい」 --
while an account is paused, nothing must fail because of it.

WHAT FAILED. ``sac accounts send-credentials --all`` enumerates every
account this host holds refresh material for and pushes an access-only
copy to each peer. A rested account still holds refresh material, so it
was still enumerated; minting from it raised ``MintError`` (its
entitlement verdict reads FORBIDDEN, or its token is simply not being
kept alive), which became a ``KeepaliveError`` per peer, which set the
run's single ``failed`` boolean, which exited 1. One resting account x
N peers x a short timer = hundreds of failures a day, none of which
means anything. That is the same defect ``--optional-peer`` was written
for -- "a signal that is always red is one nobody reads" -- one axis
over: that flag forgives an intermittent PEER, and nothing forgave an
ACCOUNT whose absence is intended.

THE MECHANISM IS A PARTITION, NOT A SECOND TOLERANCE. The paused
accounts are removed from the list BEFORE the push loop, so they cannot
reach ``failed`` at all. The exit-code logic is untouched; there is no
new boolean and no new forgiveness path. Two consequences follow, both
deliberate:

* A SKIP IS NOT A TOLERATED FAILURE, and the run never merges the two
  counts. A tolerated failure is work that failed and we forgave; a
  skip is work we correctly did not do. Collapsing them would let a
  real peer failure hide inside a count of skips.
* Skipping BEATS tolerating here. Reusing the optional-peer path would
  be a smaller diff but would still open one ssh connection and print
  one FAILED line per peer, per account, per pass. Tolerance is the
  right verb for a peer that MIGHT be up; skip is the right verb for an
  account the operator has decided is down.

WHY NO ``--paused-account`` FLAG. ``--optional-peer``'s doctrine is "a
declaration, not a detector … the unit file states exactly which hosts
may be absent", and this does not violate it. A peer's intermittency is
per-INVOCATION policy, which belongs where ``systemctl cat`` shows it.
A pause is a property of the ACCOUNT: already written down, already
timestamped, already carrying its reason and its author. On the command
line it would mean editing the JobSpec and redeploying the unit on
every pause -- a redeploy per pause, the opposite of the one-command
stop/restart that was asked for. The explicitness lives in
``pause.json``, and :func:`skip_line` prints the reason AND the age on
every run, so ``journalctl`` states it as plainly as the unit file
would.

THE AGE IS IN THE LINE ON PURPOSE. A pause does not expire (see
:mod:`.._creds._pause`), so the only thing standing between a
deliberate rest and a forgotten account is that the rest keeps
announcing how long it has stood. A forty-day pause reads ``40d``,
against the operator's own seven-day horizon for forgotten work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._creds._pause import Pause, read_pause

__all__ = [
    "partition_paused",
    "skip_line",
    "skip_record",
    "all_paused_line",
]


def partition_paused(
    accounts: list[str],
    *,
    store_dir: Path | None = None,
    home: Path | None = None,
) -> tuple[list[str], list[tuple[str, Pause]]]:
    """Split ``accounts`` into (to-push, paused-with-their-decision).

    Order is preserved in both halves so the run's output stays
    deterministic and diffable across passes.

    ``store_dir`` / ``home`` are the SAME test seams every function on
    this path already takes (``account_health``,
    ``mint_access_only_artifact``, ``keepalive_push``), so a test drives
    this against a real store on a real ``tmp_path`` with nothing
    substituted.
    """
    from .._state.account_store import _store_path

    store = _store_path(store_dir, home if home is not None else Path.home())
    judged = [(name, read_pause(name, store / name)) for name in accounts]
    return (
        [name for name, pause in judged if not pause.active],
        [(name, pause) for name, pause in judged if pause.active],
    )


def skip_line(account: str, pause: Pause, *, now: float | None = None) -> str:
    """The one stderr line a skipped account gets, per run.

    States four things, and each earns its place: that nothing was
    pushed, that nothing FAILED (the operator's actual requirement --
    the line must not read like a soft error), how long the pause has
    stood, and the exact command that lifts it. His words are quoted
    back to him verbatim: 「また復活させる」 -- he intends to bring these
    accounts back, so the line has to be readable as a standing
    reminder, not as noise to filter out.
    """
    return (
        f"  {'(paused)':20s}  {account}: SKIPPED — paused "
        f"{pause.age_human(now)} ago: {pause.reason}. Nothing pushed, "
        f"nothing failed. `sac accounts resume {account}` puts it back."
    )


def skip_record(account: str, pause: Pause) -> dict[str, Any]:
    """The ``--json`` record for a skipped account.

    ``ok`` is True because the run did the right thing, and ``skipped``
    names WHY so a consumer can tell "pushed successfully" from "did not
    need pushing" without parsing prose. ``peer`` is None because no
    peer was involved -- this decision is made once per account, before
    any peer is contacted.
    """
    return {
        "account": account,
        "peer": None,
        "ok": True,
        "skipped": "paused",
        "reason": pause.reason,
        "since": pause.since,
    }


def all_paused_line(count: int) -> str:
    """The summary when EVERY enumerable account is paused.

    This case must exit 0, and it must not be confused with the
    ``--all`` guard that refuses when this host holds refresh material
    for nothing. That guard exits 1 because a host which cannot keep
    anybody alive has to say so; this one exits 0 because the empty list
    is exactly what the operator asked for. Same empty list, opposite
    verdicts, so the two sentences must not sound alike either.
    """
    return (
        f"  all {count} account(s) this host holds refresh material for are "
        "PAUSED — nothing to push, and that is the intended state, not a "
        "failure."
    )
