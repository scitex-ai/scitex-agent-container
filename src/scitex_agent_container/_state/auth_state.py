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

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .state_db import DEFAULT_DB_PATH, init_schema, now_iso, open_db

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

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_auth_state (
    name         TEXT PRIMARY KEY,
    auth_failed  INTEGER NOT NULL,
    checked_at   TEXT NOT NULL,
    banner       TEXT,
    reason       TEXT,
    note         TEXT
);
"""

_COLUMNS = "name, auth_failed, checked_at, banner, reason, note"


def _resolve_db_path(db_path: Path | None) -> Path:
    """The state.db this module reads/writes (``None`` → the process default)."""
    return Path(db_path) if db_path else DEFAULT_DB_PATH


def _ensure_schema(db_path: Path | None) -> None:
    """Create ``agent_auth_state`` if missing — WRITE path only.

    Deliberately NOT called by :func:`list_auth_states`: a reader that ran DDL
    would take a write lock on state.db every time an operator typed
    ``sac agents list``.
    """
    init_schema(db_path)
    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)


def _row_to_state(row: sqlite3.Row) -> dict:
    """One db row → the plain cached-verdict dict the readers consume."""
    return {
        "auth_failed": bool(row["auth_failed"]),
        "checked_at": row["checked_at"] or "",
        "banner": row["banner"] or None,
        "reason": row["reason"] or "",
        "note": row["note"] or "",
    }


# --- write path (the watchdog) ----------------------------------------------


def record_auth_checks(
    checks: list[dict],
    *,
    checked_at: str | None = None,
    db_path: Path | None = None,
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
    """
    if not checks:
        return 0
    stamp = checked_at or now_iso()
    _ensure_schema(db_path)
    written = 0
    with open_db(db_path) as conn:
        for check in checks:
            name = str(check.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                f"INSERT INTO agent_auth_state ({_COLUMNS}) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(name) DO UPDATE SET "
                "auth_failed=excluded.auth_failed, "
                "checked_at=excluded.checked_at, "
                "banner=excluded.banner, "
                "reason=excluded.reason, "
                "note=excluded.note",
                (
                    name,
                    1 if check.get("auth_failed") else 0,
                    stamp,
                    check.get("banner") or None,
                    check.get("reason") or "",
                    check.get("note") or "",
                ),
            )
            written += 1
    return written


def record_auth_check(
    name: str,
    auth_failed: bool,
    *,
    banner: str | None = None,
    reason: str = "",
    note: str = "",
    checked_at: str | None = None,
    db_path: Path | None = None,
) -> None:
    """UPSERT ONE agent's auth verdict (thin wrapper over the batch write)."""
    record_auth_checks(
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
        db_path=db_path,
    )


def clear_auth_state(name: str, *, db_path: Path | None = None) -> bool:
    """Drop ``name``'s cached verdict. Idempotent; True iff a row was deleted."""
    _ensure_schema(db_path)
    with open_db(db_path) as conn:
        cur = conn.execute("DELETE FROM agent_auth_state WHERE name=?", (name,))
        return cur.rowcount > 0


# --- read path (``sac agents list`` — keep this CHEAP) -----------------------


def list_auth_states(*, db_path: Path | None = None) -> dict[str, dict]:
    """``{agent_name: verdict}`` for every cached check, in ONE db read.

    The ``sac agents list`` read. Mirrors the one-query shape of
    ``port_allocator.list_claims()`` and is deliberately the cheapest thing that
    can work: ONE connect, ONE ``SELECT``, and NO ``init_schema`` (see
    :func:`_ensure_schema` for why a reader must not run DDL).

    Tolerant by design — a state.db that does not exist yet, an
    ``agent_auth_state`` table no watchdog has ever created, or any sqlite
    hiccup all return ``{}``: "nobody has been checked". Every row then renders
    as never-checked, which is the honest answer. This must never crash, and
    never stall, ``sac agents list``.
    """
    path = _resolve_db_path(db_path)
    # Guard BEFORE connecting: sqlite3.connect() CREATES a missing file, and a
    # READ has no business materialising an empty state.db on a fresh host.
    if not path.is_file():
        return {}
    # stx-allow: fallback (reason: the auth cache ENRICHES the agent list; a db
    # that is missing/locked/schema-less means "not checked yet", never a failed
    # `sac agents list`.)
    try:
        conn = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error:  # stx-allow: fallback (reason: see inline comment)
        return {}
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(f"SELECT {_COLUMNS} FROM agent_auth_state").fetchall()
    except sqlite3.Error:  # stx-allow: fallback (reason: table not created yet — no watchdog has run on this host)
        return {}
    finally:
        conn.close()
    return {str(r["name"]): _row_to_state(r) for r in rows}


def get_auth_state(name: str, *, db_path: Path | None = None) -> dict | None:
    """One agent's cached verdict, or ``None`` when it has never been checked."""
    return list_auth_states(db_path=db_path).get(name)


# --- staleness (a cache is not truth — it has an age) ------------------------


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
