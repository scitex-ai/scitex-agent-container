"""The append-only fleet AUTH-EVENT log — one JSONL line per auth event.

WHY THIS EXISTS (operator, 2026-07-18)
    「サーバーが落とすんだから、ログを取ればいいんじゃないですか？」 — the server
    is what drops us, so log it. On 2026-07-18 ~19:30 JST six parallel workers
    died at once behind "Login expired". The facts needed to explain that —
    WHICH account rotated, WHEN, and whether the restarts that followed did
    anything — existed in three unjoined places: ``auth-heal.log`` (prose,
    intent-only), ``accounts/rotation-audit.jsonl`` (the causal rotation), and
    each agent's private session transcript. No single timeline held them, so a
    one-query answer took hours. This module is that timeline.

WHAT IT IS AND IS NOT
    It OBSERVES. It never detects, restarts, refreshes or remediates anything —
    ``auth-heal.py`` / :mod:`.._authheal` own remediation and keep owning it. A
    second actor on this rail would be the double-supervisor class; a recorder
    is not an actor.

ATTEMPT AND OUTCOME ARE SEPARATE RECORDS — THE WHOLE POINT
    ``auth-heal.log`` recorded 169 ``-> auto-restart`` lines over seven days
    whose ``age=`` field never reset (one reached 262200s = three days). Every
    one of those lines is a statement of INTENT that reads exactly like a
    statement of EFFECT, and nothing existed to disagree with it. So a restart
    here writes TWO records — :data:`RESTART_ATTEMPTED` before the act,
    :data:`RESTART_OUTCOME` after it — joined by an ``attempt_id``. An attempt
    whose outcome never lands, or lands with ``succeeded: false``, is then a
    QUERY (:func:`unresolved_attempts`), not an inference from screens. Merging
    them back into one "restarted" event would restore the exact blindness that
    let 169 ineffective restarts look like remediation.

FAIL-OPEN, ALWAYS
    This is an observability rail bolted to the side of the auth path. Every
    write is best-effort and returns a bool; a full disk, a read-only mount or a
    serialisation failure must NEVER abort a restart or a refresh. The thing
    observed always outranks the observing of it.

TRI-STATE FIELDS
    ``account`` and ``http_status`` are ALWAYS present and are ``null`` when not
    determinable. An absent field and an unknown value are different facts: the
    first says nobody thought to record it, the second says we looked and could
    not tell. Never guess an account onto a record — a wrong account is worse
    than a null one, because it will be believed.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

__all__ = [
    "AUTH_EVENT_FILENAME",
    "AUTH_EVENT_LOG_ENV",
    "AUTH_FAILURE_OBSERVED",
    "KNOWN_EVENTS",
    "RESTART_ATTEMPTED",
    "RESTART_OUTCOME",
    "TOKEN_ROTATED",
    "AuthEvent",
    "auth_event_log_path",
    "log_auth_event",
    "log_auth_failure_observed",
    "log_restart_attempted",
    "log_restart_outcome",
    "log_token_rotated",
    "read_auth_events",
    "unresolved_attempts",
]

#: Filename of the append-only event log, relative to the runtime dir. Sits
#: alongside ``auth-heal.log`` deliberately — the convention is the runtime
#: dir, and an observability file that needs explaining where it lives is one
#: nobody will open at 3am.
AUTH_EVENT_FILENAME = "auth-events.jsonl"

#: An auth failure was OBSERVED (an HTTP 401 seen, or a login banner on a pane).
#: Note the name: what is recorded is that we SAW it, not that a token expired.
#: Claude Code renders ANY 401 as "Login expired · Please run /login" while
#: nothing has necessarily expired, so the string is not the fact — the status
#: code, the account and the timing are.
AUTH_FAILURE_OBSERVED = "auth-failure-observed"

#: A credential ROTATION was performed. THE causal event: the refresh_token is
#: single-use, so one rotation invalidates every co-tenant's in-memory token at
#: once. Six agents dying together is one of these, not six coincidences.
TOKEN_ROTATED = "token-rotated"

#: A restart was ATTEMPTED. Written BEFORE the act, so an attempt that never
#: returns still leaves a record. Intent — never read this as effect.
RESTART_ATTEMPTED = "restart-attempted"

#: What the attempt ACTUALLY did, carrying ``attempt_id`` + ``succeeded``.
#: This is the record that can REFUTE the one above.
RESTART_OUTCOME = "restart-outcome"

#: The closed set. Unknown values are still written (forward-compat: a record
#: we do not recognise is evidence too) but flagged with ``event_known: false``
#: so a reader can tell a new event type from a typo.
KNOWN_EVENTS = frozenset(
    {AUTH_FAILURE_OBSERVED, TOKEN_ROTATED, RESTART_ATTEMPTED, RESTART_OUTCOME}
)

#: Env override for the log location. Lets a test drive a REAL file in a temp
#: dir (no mocks) and lets an operator relocate the rail without a code change.
AUTH_EVENT_LOG_ENV = "SAC_AUTH_EVENT_LOG"


def auth_event_log_path() -> Path:
    """Where the auth-event log lives. Resolved PER CALL, never cached.

    Per-call resolution matters: a module-level constant computed at import
    cannot be redirected by an env var a test sets afterwards, which is how a
    suite ends up writing into the REAL fleet runtime dir while believing it is
    hermetic.
    """
    override = os.environ.get(AUTH_EVENT_LOG_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / AUTH_EVENT_FILENAME


def _resolve_host() -> str:
    """Best-effort canonical host label. Never raises."""
    # stx-allow: fallback (reason: the host is a cosmetic label on an
    # observability record; a resolver failure must degrade to a short
    # hostname and then to "unknown", never break the write.)
    try:
        from .._state.state_db_hostname import resolve_host

        return resolve_host(None)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        try:
            return socket.gethostname()
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            return "unknown"


def _normalise_account(account: str | None) -> str | None:
    """Map a non-answer to ``None`` so the record says UNKNOWN, not a guess.

    ``resolve_agent_account_label`` answers the literal string ``"unknown"``
    when it has no credentials file and no env override. Writing that through
    verbatim would put a plausible-looking value in a field readers join on.
    An unknown account must read as ``null`` — the tri-state — so nobody
    correlates a rotation against the string "unknown" and believes it.
    """
    if account is None:
        return None
    text = str(account).strip()
    if not text or text.lower() == "unknown":
        return None
    return text


def log_auth_event(
    *,
    event: str,
    agent: str | None,
    detail: str,
    account: str | None = None,
    http_status: int | None = None,
    path: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Append ONE auth event. Returns success; NEVER raises.

    Parameters
    ----------
    event
        One of :data:`KNOWN_EVENTS`. Unknown values are recorded anyway and
        marked ``event_known: false``.
    agent
        The agent this concerns, or ``None`` when it is fleet-wide (a rotation
        hits every co-tenant, so it belongs to no single agent).
    account
        The account in play. ``None``, or an undeterminable label, is written
        as JSON ``null`` — present-and-unknown, never absent, never guessed.
    http_status
        The HTTP status when one was actually observed, else ``None``. Do not
        synthesise a 401 from a banner string: a banner is a rendering, a
        status code is a fact, and this field is for facts.
    """
    now_ts = now if now is not None else datetime.now(tz=timezone.utc).timestamp()
    record: dict[str, Any] = {
        "timestamp_utc": datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat(),
        "event": event,
        "agent": agent,
        # Tri-state, always present: null == "we looked and could not tell".
        "account": _normalise_account(account),
        "http_status": http_status,
        "detail": detail,
        "host": _resolve_host(),
        "pid": os.getpid(),
    }
    if event not in KNOWN_EVENTS:
        record["event_known"] = False
    if extra:
        for key, val in extra.items():
            record.setdefault(key, val)

    target = path if path is not None else auth_event_log_path()
    # stx-allow: fallback (reason: FAIL-OPEN is this rail's contract. A failure
    # to record an auth event must never abort the restart or refresh being
    # observed — losing a log line is bad, losing the recovery is worse.)
    try:
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        return True
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return False


def log_auth_failure_observed(
    *,
    agent: str | None,
    detail: str,
    account: str | None = None,
    http_status: int | None = None,
    path: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Record that an auth failure was SEEN. Returns success; never raises."""
    return log_auth_event(
        event=AUTH_FAILURE_OBSERVED,
        agent=agent,
        detail=detail,
        account=account,
        http_status=http_status,
        path=path,
        now=now,
        extra=extra,
    )


def log_token_rotated(
    *,
    account: str | None,
    detail: str,
    agent: str | None = None,
    path: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Record a credential rotation — the causal event. Never raises."""
    return log_auth_event(
        event=TOKEN_ROTATED,
        agent=agent,
        detail=detail,
        account=account,
        path=path,
        now=now,
        extra=extra,
    )


def log_restart_attempted(
    *,
    agent: str,
    detail: str,
    account: str | None = None,
    attempt_id: str | None = None,
    path: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Record that a restart is ABOUT to be attempted; return its ``attempt_id``.

    Written BEFORE the restart runs, so a restart that hangs, crashes the
    process, or is killed by a timer still leaves its intent on the record.
    The returned id is what :func:`log_restart_outcome` joins on.

    Returns the ``attempt_id`` even if the write FAILED — the caller must be
    able to carry on and still label its outcome. This function's job is to
    observe the restart, never to gate it.
    """
    ident = attempt_id or uuid.uuid4().hex[:12]
    payload: dict[str, Any] = {"attempt_id": ident}
    if extra:
        payload.update(extra)
    log_auth_event(
        event=RESTART_ATTEMPTED,
        agent=agent,
        detail=detail,
        account=account,
        path=path,
        now=now,
        extra=payload,
    )
    return ident


def log_restart_outcome(
    *,
    agent: str,
    attempt_id: str,
    succeeded: bool,
    detail: str,
    account: str | None = None,
    path: Path | None = None,
    now: float | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Record what the attempt ACTUALLY did. Never raises.

    ``succeeded=False`` is the record that REFUTES the attempt above it. It is
    a first-class outcome, not an error path: a restart that ran and left the
    agent wedged is exactly the event the 169-line ``auto-restart`` history
    could not express.
    """
    payload: dict[str, Any] = {"attempt_id": attempt_id, "succeeded": bool(succeeded)}
    if extra:
        payload.update(extra)
    return log_auth_event(
        event=RESTART_OUTCOME,
        agent=agent,
        detail=detail,
        account=account,
        path=path,
        now=now,
        extra=payload,
    )


@dataclass(frozen=True)
class AuthEvent:
    """One parsed record. ``raw`` keeps every field, including unknown ones."""

    timestamp_utc: str | None
    event: str | None
    agent: str | None
    account: str | None
    http_status: int | None
    detail: str | None
    raw: dict[str, Any]

    @property
    def attempt_id(self) -> str | None:
        value = self.raw.get("attempt_id")
        return str(value) if value is not None else None

    @property
    def succeeded(self) -> bool | None:
        """Tri-state: True / False / ``None`` when the record does not say."""
        value = self.raw.get("succeeded")
        return bool(value) if isinstance(value, bool) else None


def _parse(line: str) -> AuthEvent | None:
    # stx-allow: fallback (reason: a half-written or corrupt line must not
    # blind a reader to the thousands of good ones around it — an append-only
    # log read during an incident has to degrade, not refuse.)
    try:
        obj = json.loads(line)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    if not isinstance(obj, dict):
        return None
    status = obj.get("http_status")
    return AuthEvent(
        timestamp_utc=obj.get("timestamp_utc"),
        event=obj.get("event"),
        agent=obj.get("agent"),
        account=obj.get("account"),
        http_status=status if isinstance(status, int) else None,
        detail=obj.get("detail"),
        raw=obj,
    )


def read_auth_events(path: Path | None = None) -> list[AuthEvent]:
    """Read the log, skipping unparseable lines. Returns ``[]`` if absent.

    A missing log is an empty reading, not an error — but note it is also NOT
    evidence that no auth event happened. Absence of the rail is absence of
    evidence, and this function cannot tell those apart for you.
    """
    target = Path(path) if path is not None else auth_event_log_path()
    # stx-allow: fallback (reason: reading is diagnostic; an unreadable log
    # must degrade to an empty reading rather than raise into an incident.)
    try:
        with target.open("r", encoding="utf-8") as handle:
            return [event for event in (_parse(ln) for ln in handle) if event]
    except FileNotFoundError:
        return []
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return []


def unresolved_attempts(events: Iterable[AuthEvent]) -> list[AuthEvent]:
    """Attempts with NO successful outcome — the refutation query.

    Returns every :data:`RESTART_ATTEMPTED` whose ``attempt_id`` has no
    :data:`RESTART_OUTCOME` recording ``succeeded: true``. That covers both
    ways a restart fails to be remediation:

    * an outcome landed saying ``succeeded: false`` — it ran and did not work;
    * no outcome landed at all — it never came back to tell us.

    Both are "we tried and cannot show it worked", which is precisely what 169
    ``-> auto-restart`` lines were silently asserting the opposite of. An
    attempt leaves this list only by being contradicted by a real success.
    """
    collected = list(events)
    succeeded_ids = {
        event.attempt_id
        for event in collected
        if event.event == RESTART_OUTCOME
        and event.succeeded is True
        and event.attempt_id
    }
    return [
        event
        for event in collected
        if event.event == RESTART_ATTEMPTED
        and (event.attempt_id is None or event.attempt_id not in succeeded_ids)
    ]
