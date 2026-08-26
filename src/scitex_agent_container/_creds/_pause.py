"""Has the operator DECIDED to stop using this account for a while?

OPERATOR REQUEST 2026-08-26, verbatim::

    WYSU、u、Ke のほうは除外というか休止ってできますか？すなわちまた
    アカウント復活させるので場合によってそれはクオーターを見ながら
    無駄遣いをしないように止めたり再開したりしてるんですけど、なので
    その休止の間も失敗しないようにしてほしいんですよ。

He offered 「除外」 (exclusion) and rejected it for 「休止」 -- a PAUSE. He
stops and restarts subscriptions deliberately, watching quota, and the
requirement is the last clause: **while an account is paused, nothing
must fail because of it.**

WHAT FAILS TODAY. One unusable account reds the credential-distribution
timer on every single pass. Measured chain, 2026-08-26:
``entitlement.json`` for ``wyusuuke-gmail-com`` holds ``{"state":
"FORBIDDEN", "http_status": 403}``; :func:`._account_health.account_health`
turns a VALID snapshot with a blocking verdict into ``FORBIDDEN``;
:func:`_account.mint_token.mint_access_only_artifact` refuses to mint from
anything whose ``is_healthy`` is False; that ``MintError`` becomes a
``KeepaliveError`` per peer; and
``cli_pkg/_account_keepalive.py`` sets ``failed = True`` and exits 1. One
resting account x N peers = a permanently red unit, and a signal that is
always red is one nobody reads -- which is the same reasoning that
produced ``--optional-peer``. That flag forgives an intermittent PEER;
nothing here forgave an account whose absence is INTENDED.

PAUSE IS A DECISION; ENTITLEMENT IS AN OBSERVATION
--------------------------------------------------
These are the two halves the design must never collapse:

* :mod:`._entitlement` records something MEASURED. A probe authored it,
  a later probe overwrites it, and nobody types it. It therefore decays
  (``DEFAULT_MAX_AGE_S``) -- an answer whose prober has stopped running
  must stop counting.
* A pause is AUTHORED. No probe can discover it and no probe may lift
  it. It therefore does **not** decay: a decision does not go stale
  because nobody re-asserted it, and an expiring pause would un-pause
  the account behind the operator's back -- the same conflation from
  the other side.

The separation is enforced by file, not by discipline: this record lives
in its own sidecar with exactly two writers, both of them operator verbs
(``sac accounts pause`` / ``sac accounts resume``). ``probe-entitlement``
has no code path to this file, so it does not need to be TOLD to leave
it alone -- and it deliberately keeps probing a paused account, so the
entitlement verdict underneath is already current the moment the
operator resumes.

WHY A NEW FILE AND NOT A FIELD ON AN EXISTING ONE
-------------------------------------------------
Both obvious candidates destroy the flag:

* ``account.json`` -- :func:`_state.account_store.save_account` writes a
  full object (``payload = dict(metadata); payload["name"] = name``) and
  ``_account/creds_sync.py`` calls it on every ``sync-live`` with the
  one-key dict ``{"email_address": ...}``. Any ``paused`` key there is
  gone at the next credential sync.
* ``entitlement.json`` -- :func:`._entitlement.write_entitlement` writes
  its whole four-key object, from a ``*/30`` timer. A pause there is
  erased within half an hour.

A pause that silently lifts itself is precisely what the operator asked
us to prevent. So: a sidecar named for its question, beside the
credential it describes, exactly like ``entitlement.json`` /
``identity.json`` / ``usage.json``.

PRESENCE IS THE PAUSE
---------------------
There is no ``"paused": false`` record. The file exists (with a
non-empty reason) or the account is not paused; ``resume`` deletes it.
One spelling per state -- a ``paused: false`` record would be a second
way to say what the filesystem already says, and the two would drift.

THE REASON IS REQUIRED, AND THAT IS NOT DECORATION
--------------------------------------------------
A pause with no stated reason is indistinguishable on disk from an
account somebody abandoned. The reason is the only thing that makes a
pause liftable months later, which matters because this record has no
expiry. The fleet already ruled the same way on ``scitex-cards``'
``parked`` field: it is a REASON, not a flag, because "a park with no
stated reason is exactly the abandonment the sweep should still catch".
A whitespace-only reason is therefore NOT a pause.

DEGRADE TOWARDS VISIBLE, NOT TOWARDS SILENT
-------------------------------------------
:func:`read_pause` never raises and never networks, like
:func:`._entitlement.read_entitlement`. Every failure mode -- absent,
unreadable, malformed, empty reason -- degrades to ``active=False``
carrying a non-empty ``problem``. That direction is chosen: an
unreadable pause record puts the account back in the pool and reds the
timer again, which is loud and re-pausable. The other direction takes
an account out of service silently and forever.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Pause",
    "pause_path",
    "read_pause",
    "write_pause",
    "clear_pause",
    "format_age",
]


def format_age(seconds: float | None) -> str:
    """Render a pause's age the way an operator reads it: ``3d`` / ``4h``.

    Coarse on purpose. This number exists so a pause that has stood for
    forty days READS as ``40d`` on the keepalive line every run, against
    the operator's own seven-day horizon for forgotten work. Minute
    precision on a multi-week decision would be noise.
    """
    if seconds is None:
        return "an unknown time"
    seconds = max(0.0, float(seconds))
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


@dataclass(frozen=True)
class Pause:
    """One account's pause decision, and the evidence behind it.

    Attributes
    ----------
    name
        The stored-account name this decision is about.
    active
        Whether the account is paused RIGHT NOW. False for every
        failure mode (see :func:`read_pause`), never ``None`` -- the
        callers of this flag are a boot picker and a timer, and both
        must be able to act on it without a third branch.
    reason
        The operator's own words for WHY. Empty when not paused.
    since
        Unix seconds when the pause was written; ``None`` when the
        record carried no usable timestamp (which does not invalidate
        the pause -- the reason is the load-bearing field).
    by
        Who wrote it (``user@host``), for the audit trail. Best-effort.
    problem
        Non-empty only when a record EXISTS but could not be believed.
        Rendered by the operator-facing verbs so an unreadable pause is
        visible rather than merely inert.
    """

    name: str
    active: bool = False
    reason: str = ""
    since: float | None = None
    by: str = ""
    problem: str = ""

    def age_seconds(self, now: float | None = None) -> float | None:
        """How long this pause has stood, or ``None`` without a ``since``."""
        if self.since is None:
            return None
        return (now if now is not None else time.time()) - self.since

    def age_human(self, now: float | None = None) -> str:
        """:meth:`age_seconds` rendered for a log line — ``3d`` / ``4h``."""
        return format_age(self.age_seconds(now))


def pause_path(account_dir: Path) -> Path:
    """Where one account's pause decision lives.

    Beside the credential it applies to, so a copied or backed-up
    account dir carries its own decision -- the same rule
    :func:`._entitlement.entitlement_path` states for the verdict.
    """
    return account_dir / "pause.json"


def read_pause(name: str, account_dir: Path) -> Pause:
    """Read a stored pause. NEVER raises, NEVER touches the network.

    Called from the boot picker and from the credential-distribution
    timer, so it is a plain local file read for the same reason
    :func:`._entitlement.read_entitlement` is: the picker runs at every
    agent boot and must not grow a round-trip.

    Deliberately has NO ``max_age_s`` -- and, unlike its sibling, no
    ``now`` seam either, because there is nothing here for a clock to
    decide. See the module docstring: a measurement decays, a decision
    does not. Age is a rendering question, answered by
    :meth:`Pause.age_seconds`, which takes its own ``now``.

    Every failure mode degrades to ``active=False`` with ``problem``
    saying which: absent (the normal case, and the only one with an
    empty ``problem``), unreadable, not an object, or a reason that is
    missing / empty / whitespace-only.
    """
    path = pause_path(account_dir)

    # stx-allow: fallback (reason: a boot-path read of an operator
    # decision must degrade to "not paused", never crash a start.)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return Pause(name)
    except (OSError, ValueError) as exc:
        return Pause(
            name,
            problem=f"unreadable pause record: {type(exc).__name__}",
        )

    if not isinstance(raw, dict):
        return Pause(name, problem="pause record is not an object")

    reason = str(raw.get("reason", "")).strip()
    if not reason:
        # A record with no stated reason is not a pause. See the module
        # docstring: this is the shape an abandoned account also has,
        # and refusing it here is what keeps the two distinguishable.
        return Pause(
            name,
            problem=(
                "pause record carries no reason - refusing to treat it as a "
                "pause. Re-run `sac accounts pause` with --reason."
            ),
        )

    since_raw = raw.get("since")
    since = float(since_raw) if isinstance(since_raw, (int, float)) else None
    # A pause with an unusable timestamp is STILL a pause. The reason is
    # what makes it one; `since` only decides how the age renders, and
    # discarding an operator's decision over a bad clock stamp would be
    # the silent direction this module refuses.
    return Pause(
        name=name,
        active=True,
        reason=reason,
        since=since,
        by=str(raw.get("by", "")),
    )


def write_pause(account_dir: Path, pause: Pause) -> bool:
    """Persist a pause decision beside its credential. Returns success.

    Unlike :func:`._entitlement.write_entitlement` -- which is
    best-effort bookkeeping from a timer and swallows its own failures
    -- this returns the outcome to a HUMAN who just typed a command.
    The caller must refuse to report a pause it could not write; an
    operator who is told an account is paused, and whose keepalive then
    keeps failing on it, has been lied to about the one thing he asked
    for.
    """
    path = pause_path(account_dir)
    payload = {
        "reason": pause.reason,
        "since": pause.since,
        "by": pause.by,
    }
    # stx-allow: fallback (reason: the boolean IS the error channel here
    # - the CLI renders a refusal from it - so a read-only or full disk
    # must return False rather than raise past the verb.)
    try:
        account_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(path)
        return True
    except OSError:
        return False


def clear_pause(account_dir: Path) -> bool:
    """Delete the pause record. Returns True iff a record was removed.

    Absence IS "not paused", so deletion is the whole of ``resume``.
    A False return means there was nothing to lift, which the verb
    reports rather than treating as an error -- resuming an account
    that is already running is a no-op the operator is allowed to make.

    Only ``FileNotFoundError`` is absorbed. Any OTHER ``OSError`` (a
    read-only store, a permission problem) propagates, because it means
    the pause is STILL THERE: reporting that as "nothing to lift" would
    tell the operator he had resumed an account that stays paused --
    exactly the lie :func:`write_pause` refuses in the other direction.
    """
    path = pause_path(account_dir)
    # stx-allow: fallback (reason: an already-absent record is the
    # success case for `resume`, not an exception. Every other OSError
    # is deliberately NOT caught - see the docstring.)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
