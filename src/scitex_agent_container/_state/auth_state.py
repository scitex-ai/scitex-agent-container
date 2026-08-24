"""Cached per-agent auth verdict — "is this tmux-GREEN agent actually working?"

WHY THIS EXISTS
    **tmux-up is not operational.** An agent whose API calls are being rejected
    sits at its prompt under an auth banner forever: the tmux session exists and
    its pane process is alive, so every liveness probe reads ``running`` — GREEN
    — while the agent does exactly nothing. Claude Code never re-reads the
    credentials file, so only a RESTART clears it. On a fleet of ~30 agents
    sharing one OAuth account this is the dominant silent-failure mode, and
    until now ``sac agents list`` could not tell a green-and-working agent apart
    from a green-and-dead one. That has cost the operator real time — and on
    2026-07-13 it took out four agents at once, including the one writing this.

WHAT WE ASSERT, AND WHAT WE DO NOT
    The stored fact is ``auth_failed``: *this agent cannot authenticate to the
    API*. That is what the watchdog can actually verify (a frozen auth-rejection
    banner on the agent's own pane), and it is all we claim.

    We deliberately do NOT call it "login required" or "token expired". Claude
    Code renders every 401 as ``Login expired · Please run /login``, and on this
    fleet that text is usually a LIE: nothing expired — a sibling agent's OAuth
    refresh consumed the single-use ``refresh_token`` and rotated the access
    token, REVOKING the one this agent still held in memory. The remedy is a
    restart, not a login. Believing the banner is precisely why the bug survived
    so long, so this module records the verifiable FAILURE and stores the CAUSE
    separately, as a diagnosis (``reason`` ∈ revoked / expired / unknown; see
    ``_account.auth_failure_reason``) rather than as an assumption.

READ CHEAP, WRITE CAREFULLY
    :func:`list_auth_states` is on the ``sac agents list`` hot path, which
    PR #635 just spent real effort making fast (it was ~296ms/row). So the read
    opens the db ONCE, runs ONE ``SELECT``, and **never initialises schema** — a
    reader that ran DDL would take a write lock on every ``sac agents list``. A
    missing db / missing table / any sqlite hiccup returns ``{}`` ("nobody has
    been checked yet"): the list can neither crash nor stall on a cache miss.
    The WRITE path (:func:`record_auth_checks`) owns the DDL and runs at
    watchdog cadence, so its cost does not matter.

A CACHE IS NOT TRUTH — IT HAS AN AGE
    An ``auth_failed`` from 6 hours ago is far weaker evidence than one from 60
    seconds ago and must never be rendered as fresh truth. Every row carries
    ``checked_at``, and two rules keep the cache honest:

    * **STALE** — a verdict older than :data:`STALE_AFTER_S` is still shown (it
      is the only evidence there is, and nothing self-heals) but is FLAGGED as
      weak rather than asserted. See :func:`is_stale`.
    * **SUPERSEDED** — a verdict taken BEFORE the agent's current ``started_at``
      describes a PREVIOUS incarnation and is discarded outright. This is what
      stops a just-restarted, now-healthy agent from still reading
      ``auth-failed`` until the next watchdog sweep. See :func:`verdict_for`.

    Both rules live in :func:`verdict_for`, a PURE function of (cached row,
    started_at, now) — so the reader's honesty is unit-testable without a
    database, and the read stays a single dict lookup per agent.
"""

from __future__ import annotations

import logging

from datetime import datetime, timezone

from .state_db import now_iso

__all__ = [
    "STALE_AFTER_S",
    "age_seconds",
    "clear_auth_state",
    "get_auth_state",
    "is_stale",
    "list_auth_states",
    "parse_ts",
    "record_auth_check",
    "record_auth_checks",
    "verdict_for",
]

# A cached verdict older than this is STALE — shown, but marked as weak evidence
# rather than asserted as current truth. 15 min is several times a healthy
# watchdog period, so a working watchdog never trips it; a watchdog that has
# actually STOPPED becomes visible precisely because every verdict ages past it
# (the fleet's green then honestly reads "unverified" instead of "fine").
STALE_AFTER_S = 900.0

# Matches ``state_db.now_iso()`` and the registry's ``started_at`` — the same
# second-resolution ISO-8601 UTC 'Z' stamp, so the two are directly comparable
# in :func:`verdict_for`'s SUPERSEDED check.
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: One logical store per host. The record identity is the agent NAME alone —
#: NO host column, deliberately. `host_store` already resolves to THIS host's
#: PostgreSQL, so two hosts' rows live in two different databases: the per-host
#: database IS the isolation, and a host field would add a failure mode while
#: buying nothing until federation exists. This matches the closest precedent,
#: `state_db_acl_deny_notify` (explicitly "a per-host rate-limit ledger", which
#: also keeps its SQLite key and carries no host column).
logger = logging.getLogger(__name__)

STORE_NAME = "auth_state"

#: Every write from this host is attributed to one actor. MULTI_WRITER because
#: several processes on one host legitimately write this cache — the watchdog
#: (`sac agents auth-status`), and the heal path.
_ACTOR = "scitex-agent-container"

_FIELDS = ("auth_failed", "checked_at", "banner", "reason", "note")


def _schema():
    """Identity is `name`; every other field is LAST_WRITER_WINS.

    Built lazily so importing this module does not import scitex-dev, and via
    ``Schema.build`` rather than the bare constructor so the primitive asserts
    the reserved-column and identity rules instead of us assuming them.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind):
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def fact(kind):
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=False,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=False,
        )

    return Schema.build(
        STORE_NAME,
        {
            "name": ident(FieldKind.TEXT),
            "auth_failed": fact(FieldKind.INTEGER),
            "checked_at": fact(FieldKind.TEXT),
            "banner": fact(FieldKind.TEXT),
            "reason": fact(FieldKind.TEXT),
            "note": fact(FieldKind.TEXT),
        },
    )


def auth_state_store_target():
    """Resolve WHERE the verdicts live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def _open_store():
    """Open the per-host store. Raises StoreTargetError NAMING the DSN if down."""
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        auth_state_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )


def _row_to_state(row) -> dict:
    """Normalise a stored ``Row`` into the shape every reader expects.

    ``Row.values`` is the SCHEMA FIELDS ONLY — the store's own bookkeeping
    (hlc, seq, origin, owner, hidden) hangs off sibling attributes and
    deliberately does not ride along. The SQLite version returned
    ``dict(sqlite3.Row)``, which was likewise exactly the table columns, so
    every caller sees the shape it always did.
    """
    v = row.values
    return {
        "name": v.get("name"),
        "auth_failed": bool(v.get("auth_failed")),
        "checked_at": v.get("checked_at"),
        "banner": v.get("banner") or None,
        "reason": v.get("reason") or "",
        "note": v.get("note") or "",
    }


def record_auth_checks(
    checks: list[dict],
    *,
    checked_at: str | None = None,
    db_path=None,
) -> int:
    """UPSERT a batch of auth verdicts. Returns how many rows were written.

    ``checks`` is a list of ``{"name", "auth_failed", "banner"?, "reason"?,
    "note"?}``. Called by the auth watchdog (``sac agents auth-status``) with
    every agent whose pane it actually MANAGED TO READ.

    Pass ONLY agents that were genuinely observed. An agent that could not be
    read has produced NO evidence, and writing ``auth_failed=False`` for it
    would assert "auth is fine" on the strength of nothing — the exact species
    of comfortable lie this feature exists to kill. Leaving its previous row
    untouched is the honest outcome: the stamp simply ages, and the reader marks
    it stale.

    ALL FIVE FIELDS ARE SENT ON EVERY WRITE, including empty ones. The SQLite
    ``ON CONFLICT DO UPDATE`` overwrote every column unconditionally, so a
    recovered agent's stale ``banner`` was cleared by the next sweep. Sending
    only the non-empty fields would leave that banner in place forever, which
    reads as "still failing" long after it stopped.

    ``db_path`` is ACCEPTED AND IGNORED. It named a SQLite file and there is no
    file; it stays in the signature only so the ten existing call sites keep
    working across this change, and it should be removed in a follow-up.
    """
    if not checks:
        return 0
    from scitex_dev.store import ANY_REVISION

    stamp = checked_at or now_iso()
    written = 0
    store = _open_store()
    try:
        with store.batch():
            for check in checks:
                name = str(check.get("name") or "").strip()
                if not name:
                    continue
                # A put onto a HIDDEN record is invisible: apply_entry carries
                # `hidden` forward through an upsert and this schema declares no
                # hide-flag field. So a previously-cleared agent that starts
                # failing again would write verdicts nobody could read. Unhide
                # first, guarded on the three-valued state (None = absent, so
                # there is nothing to unhide).
                if store.is_hidden({"name": name}):
                    store.unhide({"name": name}, expected_revision=ANY_REVISION)
                store.put(
                    {
                        "name": name,
                        "auth_failed": 1 if check.get("auth_failed") else 0,
                        "checked_at": stamp,
                        "banner": check.get("banner") or None,
                        "reason": check.get("reason") or "",
                        "note": check.get("note") or "",
                    },
                    expected_revision=ANY_REVISION,
                )
                written += 1
    finally:
        store.close()
    return written


def record_auth_check(
    name: str,
    *,
    auth_failed: bool,
    banner: str | None = None,
    reason: str = "",
    note: str = "",
    checked_at: str | None = None,
    db_path=None,
) -> int:
    """UPSERT a single verdict. Thin wrapper over :func:`record_auth_checks`."""
    return record_auth_checks(
        [
            {
                "name": name,
                "auth_failed": auth_failed,
                "banner": banner,
                "reason": reason,
                "note": note,
            }
        ],
        checked_at=checked_at,
    )


def clear_auth_state(name: str, *, db_path=None) -> bool:
    """Forget one agent's verdict. True when a row was actually removed.

    Mirrors the old ``DELETE ... WHERE name=?`` and its ``rowcount > 0``: a
    request to clear a name that was never recorded is not an error, it is
    simply False.
    """
    from scitex_dev.store import ANY_REVISION

    store = _open_store()
    try:
        # HIDE, not delete: the primitive has no hard delete — a record is
        # retired by hiding it, and `is_hidden` is THREE-VALUED (True / False /
        # None-for-absent). None is what reproduces the old `rowcount > 0`:
        # clearing a name that was never recorded is False, not an error.
        state = store.is_hidden({"name": name})
        if state is None or state:
            return False
        store.hide({"name": name}, expected_revision=ANY_REVISION)
        return True
    finally:
        store.close()


def list_auth_states(*, db_path=None) -> dict[str, dict]:
    """Every recorded verdict, keyed by agent name.

    ON THE HOT PATH. This is called once per ``sac agents list``, and the shape
    is what keeps it affordable: ONE store open and ONE bulk read for the WHOLE
    fleet, looked up per row from the returned dict — never a per-agent store
    hit. The port made the fixed cost of that single open dearer than SQLite's;
    it did not make the call count grow.

    AN UNREACHABLE STORE IS REPORTED, NOT SWALLOWED SILENTLY. It returns the
    empty shape so ``sac agents list`` still renders, but it logs at ERROR with
    the DSN in the message. That distinction matters: an empty dict from a
    REACHABLE store means "no watchdog has run", and an empty dict from an
    unreachable one means "I could not look" — the two must never be
    indistinguishable, because the second silently renders every wedged agent
    as green.
    """
    try:
        store = _open_store()
    except Exception as exc:  # stx-allow: fallback (reason: `sac agents list` must still render when this host's PostgreSQL is down; the store's own error NAMES the DSN and is logged at ERROR here. SINK: logger.error on this module's logger -> journald via sac-listen.service, or the caller's stderr for a direct CLI run; `journalctl --user | grep "auth cache unreadable"` is the check)
        logger.error("auth cache unreadable, rendering as no-verdict: %s", exc)
        return {}
    try:
        return {
            str(row.values.get("name")): _row_to_state(row)
            for row in store.rows()
            if row.values.get("name")
        }
    finally:
        store.close()


def get_auth_state(name: str, *, db_path=None) -> dict | None:
    """One agent's verdict, or None when nothing is recorded for it.

    Same unreachable-store contract as :func:`list_auth_states`: logged loudly,
    then None, so an agent START is never blocked by a stopped PostgreSQL.
    """
    try:
        store = _open_store()
    except Exception as exc:  # stx-allow: fallback (reason: `agent start` calls this through resolve_start_verdict with NO try/except of its own; letting it raise would make a stopped PostgreSQL fail every start on the host, where a missing state.db was benign. UNKNOWN falls through to a real start, which is the safe direction. SINK: logger.error as above)
        logger.error("auth cache unreadable for %s: %s", name, exc)
        return None
    try:
        rec = store.get({"name": name})
        return _row_to_state(rec) if rec else None
    finally:
        store.close()


def parse_ts(stamp: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC ``Z`` stamp; ``None`` when absent/unparseable.

    Tolerates the sentinels the registry uses for "no value" (``-`` / ``?``) and
    an ISO stamp carrying a real offset, so a hand-edited or differently-stamped
    row degrades to "unknown age" rather than raising.
    """
    if not stamp or stamp in ("-", "?"):
        return None
    try:
        return datetime.strptime(stamp, _TS_FMT).replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    try:  # stx-allow: fallback (reason: tolerate an offset-carrying ISO stamp)
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:  # stx-allow: fallback (reason: unparseable ⇒ unknown age, never a raise)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def age_seconds(checked_at: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds since ``checked_at``; ``None`` when absent/unparseable.

    Never negative: a stamp from the future (clock skew between the watchdog host
    and the reader) clamps to 0 rather than rendering as "-3s ago".
    """
    parsed = parse_ts(checked_at)
    if parsed is None:
        return None
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (reference - parsed).total_seconds())


def is_stale(
    checked_at: str | None,
    *,
    now: datetime | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> bool:
    """True when the verdict is older than ``stale_after_s`` — weak evidence.

    An absent/unparseable stamp is NOT "stale": it is *no evidence at all*, a
    different state the readers render as "never checked". Callers discriminate
    on ``auth_checked_at`` being empty, not on this flag.
    """
    age = age_seconds(checked_at, now=now)
    return age is not None and age > stale_after_s


def verdict_for(
    state: dict | None,
    *,
    started_at: str | None = None,
    now: datetime | None = None,
    stale_after_s: float = STALE_AFTER_S,
) -> dict:
    """The PURE read-side rule: cached row + agent start-time → row auth fields.

    Returns the seven keys every agent-list row carries, ALWAYS all present::

        {"auth_failed": bool,           # the cached verdict (False if none)
         "auth_checked_at": str,        # "" ⇒ never checked (no evidence)
         "auth_check_age_s": int|None,  # None ⇒ never checked
         "auth_check_stale": bool,      # verdict is old ⇒ weak evidence
         "auth_banner": str|None,       # what was on the pane
         "auth_reason": str,            # revoked / expired / unknown
         "auth_remedy": str}            # restart / login

    Two honesty rules are enforced here, and only here:

    * **SUPERSEDED** — a verdict stamped BEFORE ``started_at`` was taken on a
      PREVIOUS incarnation of this agent and is DISCARDED (reported as
      never-checked). Restart is the cure for the common (revoked) failure, so
      without this rule the operator would restart a wedged agent, fix it, and
      STILL be told ``auth-failed`` until the next watchdog sweep — the cache
      would be lying at the exact moment he acted on it. Being purely read-side,
      it holds no matter HOW the agent was restarted (CLI, host-listen broker,
      tmux respawn): there is no start-path to instrument, and therefore none to
      forget.
    * **STALE** — an old-but-current verdict is KEPT (nothing self-heals, so it
      remains the best evidence available) and FLAGGED, so the reader can show it
      as weak rather than assert it.
    """
    if not state or not state.get("checked_at"):
        return _no_verdict()
    checked_at = str(state["checked_at"])
    started = parse_ts(started_at)
    checked = parse_ts(checked_at)
    if started is not None and checked is not None and checked < started:
        # The verdict predates this incarnation — it describes an agent that no
        # longer exists, and says nothing about the process running now.
        return _no_verdict()
    from .._account.auth_failure_reason import REASON_UNKNOWN, remedy_for

    age = age_seconds(checked_at, now=now)
    reason = str(state.get("reason") or REASON_UNKNOWN)
    return {
        "auth_failed": bool(state.get("auth_failed")),
        "auth_checked_at": checked_at,
        "auth_check_age_s": None if age is None else int(age),
        "auth_check_stale": is_stale(checked_at, now=now, stale_after_s=stale_after_s),
        "auth_banner": state.get("banner") or None,
        "auth_reason": reason,
        "auth_remedy": remedy_for(reason),
    }


def _no_verdict() -> dict:
    """The never-checked shape — no evidence, so no claim about this agent."""
    return {
        "auth_failed": False,
        "auth_checked_at": "",
        "auth_check_age_s": None,
        "auth_check_stale": False,
        "auth_banner": None,
        "auth_reason": "",
        "auth_remedy": "",
    }
