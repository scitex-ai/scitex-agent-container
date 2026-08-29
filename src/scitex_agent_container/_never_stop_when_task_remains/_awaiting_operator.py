"""Report the queue that stopped existing: cards waiting on a human.

THE DEFECT THIS EXISTS FOR
--------------------------
A card with ``status=blocked`` sends no nudge — deliberately, so blocked work
stops nagging. It is ALSO excluded from the runnable-items count this hook
reports. Those two facts together mean a blocked card **stops existing**:
nothing surfaces it, nothing counts it, and an agent reporting "board clear"
is telling the truth about the only number it can see.

Measured on 2026-08-11, on two boards, independently::

    scitex-agent-container   38 blocked, 21 of them blocker=operator-decision
    scitex-dev               69 blocked, 24 of them operator-decision,
                             oldest 2026-07-19

Three weeks of questions naming the operator as the gate, unasked — and on
the same night the operator asked one of those agents directly whether it had
anything to ask him, and got ONE item back, because nobody looked.

The general shape, which outlives this fix: **a queue excluded from the alarm
is a queue nobody is waiting on.** Blocked cards were designed to stop
nagging, and the cost of that design was that they stopped existing.

REPORT, DO NOT GATE — THE WHOLE DESIGN RESTS ON THIS
----------------------------------------------------
A card blocked on the operator is CORRECTLY waiting. It must never prevent an
agent from stopping. Making it a gate would be strictly worse than the status
quo: it would make this hook unstoppable, and the first thing anyone does with
an unstoppable hook is bypass it — after which it protects nothing at all.
The loop guard in :mod:`._loop_guard` exists because that failure has already
been reasoned through once here; do not re-introduce it through this door.

So this module is a **read path with one output: a string**. It never blocks,
never writes a card, never raises, and never prints. The caller merges its
line into ``systemMessage``, which is the one field a Stop hook can carry
while still ALLOWING the stop.

Why ``systemMessage`` and not the block ``reason``:

1. ``reason`` does not exist on the allow path, and the allow path — the
   agent that says "board clear" — is the exact failure being fixed.
2. ``reason`` is authored by scitex-cards and forwarded verbatim; appending
   to it would make sac an editor of someone else's instruction.
3. ``reason`` feeds the loop-guard signature, whose contract (see
   :meth:`._detector.Verdict.block_signature_source`) warns that any value
   moving turn to turn stops the guard ever tripping. An age in days is
   precisely such a value.

WHAT COUNTS AS "WAITING ON A HUMAN"
-----------------------------------
``status=blocked`` AND ``blocker=operator-decision`` — the same predicate the
board calls BLOCKING-YOU (``list-tasks --blocking-me`` /
``--blocking-operator``). It is spelled here in ``--status`` / ``--blocker``,
which are far older flags than either predicate, because THE FLEET ALWAYS RUNS
OLDER THAN THE PUBLISHED VERSION and a report that silently never appears on
half the hosts is the defect, not the fix. Both spellings were measured to
return the same 21 rows on the live board.

``blocker=agent-wait`` is DELIBERATELY EXCLUDED. An agent waiting on another
agent is a different failure with a different owner and a different remedy —
counting it here would misattribute the gate and dilute the one number this
line exists to make unignorable. It wants its own line, not a share of this
one. :data:`OPERATOR_BLOCKER` is the single place that decision lives.

COST
----
This runs on EVERY stop attempt, so it is bounded three ways: a hard
subprocess timeout, a TTL cache under the sac runtime tree, and a NEGATIVE
cache — an unreadable board is remembered for the same TTL, so a database that
is down cannot make stopping expensive. A hook that adds latency to every turn
gets disabled, and a disabled hook protects nothing.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from .._runtime_paths import runtime_base_dir
from ._loop_guard import _STATE_SUBDIR, _safe_name

#: The blocker value that means "a human owes this card an answer". The ONE
#: place the agent-wait decision above is encoded.
OPERATOR_BLOCKER = "operator-decision"

#: The status a card must carry to be counted. A card that is not blocked is
#: not waiting, whatever its blocker field still says.
BLOCKED_STATUS = "blocked"

#: The read command. Overridable via ``$SAC_AWAITING_CARDS_CMD`` — an operator
#: escape hatch AND the seam that lets a test point at a real script.
CMD_ENV = "SAC_AWAITING_CARDS_CMD"
_DEFAULT_CMD = "scitex-cards list-tasks"

#: The store-identity command. A READER THAT DOES NOT NAME ITS SOURCE WILL BE
#: WRONG AGAIN the next time a store moves, and this fleet currently has four:
#: two Postgres clones, an abandoned SQLite inbox sidecar still being opened
#: constantly and written never, and a YAML file that `scitex-cards done`
#: resolved to while ``$SCITEX_CARDS_DB`` named Postgres.
#:
#: We ask the PACKAGE what it resolved to rather than reading
#: ``$SCITEX_CARDS_DB`` ourselves. The env var is a CLAIM; the resolver
#: reports the target actually opened, and the gap between those two is
#: exactly how a reader ends up confidently quoting a corpse.
STORE_CMD_ENV = "SAC_AWAITING_STORE_CMD"
_DEFAULT_STORE_CMD = "scitex-cards resolve-store"

#: How long a reading stays good, in seconds. ``0`` disables the cache.
TTL_ENV = "SAC_AWAITING_CARDS_TTL_S"
_DEFAULT_TTL_S = 900.0

#: Hard bound on the read. Shorter than the detector's 15s because this is a
#: REPORT: if it cannot answer promptly, saying nothing is the correct answer.
_TIMEOUT_S = 10.0

#: Stamps in order of preference. ``blocked_at`` is when the store says the
#: card entered the blocked state — the exact wait. It is not always recorded
#: (15 of 21 rows on the live sac board), so ``created_at`` stands in: a card
#: that has existed N days and is blocked on the operator has been a pending
#: question for at most N days, and that is the honest available proxy.
_STAMP_FIELDS = ("blocked_at", "created_at", "last_activity")


def query_argv(agent: str) -> list[str]:
    """Build the read command, always naming the agent.

    Scoped to ONE agent's board on purpose. The hook answers for the agent it
    resolved; a fleet-wide number would name a queue this agent cannot act on,
    and "surface or reclassify" is only advice you can follow about your own
    cards.
    """
    base = (os.environ.get(CMD_ENV) or "").strip() or _DEFAULT_CMD
    return [
        *shlex.split(base),
        "--assignee",
        agent,
        # OPT OUT OF $SCITEX_TODO_SCOPE. `list-tasks` silently ANDs that
        # ambient variable into the filter, and an agent whose scope does not
        # match its own cards' scope then gets 0 rows back from a board holding
        # 21 — MEASURED, exactly that, on 2026-08-12: baseline 21, with
        # SCITEX_TODO_SCOPE set 0, with this flag 21 again. A report that can
        # be silenced to zero by an ambient env var reproduces the very defect
        # it exists to fix, so the scope is stated rather than inherited.
        "--scope",
        "",
        "--status",
        BLOCKED_STATUS,
        "--blocker",
        OPERATOR_BLOCKER,
        "--json",
    ]


def store_argv() -> list[str]:
    """Build the store-identity command."""
    base = (os.environ.get(STORE_CMD_ENV) or "").strip() or _DEFAULT_STORE_CMD
    return [*shlex.split(base), "--json"]


def redact(target: str) -> str:
    """Strip any password from a store URL. NEVER print a credential.

    A store target may carry userinfo (``postgresql://user:secret@host/db``).
    The identity we want is scheme, user, host, port and database — never the
    secret. Non-URL targets (a file path) are identities in themselves and
    pass through unchanged.
    """
    if "://" not in target:
        return target
    scheme, _, rest = target.partition("://")
    netloc, slash, path = rest.partition("/")
    if "@" in netloc:
        userinfo, _, hostport = netloc.rpartition("@")
        user, colon, _secret = userinfo.partition(":")
        netloc = f"{user}:***@{hostport}" if colon else f"{user}@{hostport}"
    return f"{scheme}://{netloc}{slash}{path}"


def _store_identity() -> str:
    """What the package says it resolved to, redacted. ``""`` if it will not say.

    A URL alone cannot tell ``127.0.0.1:55432/scitex_cards`` on this host from
    the identical string on another, and "two clones" is the fleet's current
    state — so the store's own identity is carried alongside it.

    But ``store_uuid`` ALONE does not separate two clones, and this function
    used to claim it did. **A uuid stored inside a database is copied by a fork
    of that database**, so it names the LINEAGE ("which store am I a copy
    of?"), never the instance. Measured 2026-08-11: two endpoints — ``:55432``
    and a tunnel at ``127.0.0.1:5442`` — both answered ``store_uuid``
    ``1d55dd6e-3d2a-4c24-a429-a78835ab988f`` while holding 404 and 146 cards
    the other lacked. Every operation on both succeeded.

    The half a fork cannot forge comes from the ENGINE, not the rows —
    ``pg_control_system().system_identifier`` on Postgres, its sqlite analogue
    on a file store. So identity is the PAIR. When the store reports only the
    uuid, this says so in the rendered line rather than presenting a
    lineage id as though it identified this instance: a reader who is told
    ``uuid 1d55dd6e`` and nothing else will reasonably assume two agents
    printing it are on the same store, which is exactly the inference that was
    false on 2026-08-11.
    """
    try:
        proc = subprocess.run(
            store_argv(),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: an unnameable store degrades to an unnamed report, never to a broken stop)
        return ""
    if proc.returncode != 0:
        return ""
    start = (proc.stdout or "").find("{")
    if start < 0:
        return ""
    try:
        data, _ = json.JSONDecoder().raw_decode(proc.stdout[start:])
    except json.JSONDecodeError:  # stx-allow: fallback (reason: unparseable identity is no identity; say nothing rather than guess)
        return ""
    if not isinstance(data, dict):
        return ""
    resolved = str(data.get("resolved") or "").strip()
    if not resolved:
        return ""
    uuid = str(data.get("store_uuid") or "").strip()
    system = str(data.get("system_identifier") or "").strip()
    identity = redact(resolved)
    if not uuid:
        return identity
    if system:
        return f"{identity} (uuid {uuid[:8]} sys {system[:8]})"
    # Lineage id with no engine half: it cannot rule out a fork, and saying
    # so costs three words. Silence here reads as certainty.
    return f"{identity} (uuid {uuid[:8]}, lineage only — fork undetectable)"


def cache_path(agent: str) -> Path:
    """Where ``agent``'s last reading lives — beside the loop-guard state."""
    return runtime_base_dir() / _STATE_SUBDIR / f"awaiting-{_safe_name(agent)}.json"


def _ttl_seconds() -> float:
    raw = (os.environ.get(TTL_ENV) or "").strip()
    if not raw:
        return _DEFAULT_TTL_S
    try:
        return max(0.0, float(raw))
    except ValueError:  # stx-allow: fallback (reason: a malformed TTL must not break the stop path; fall back to the default)
        return _DEFAULT_TTL_S


def _rows_from(stdout: str) -> "list | None":
    """The JSON array on stdout, or ``None`` when there is not one.

    Decodes the FIRST array and ignores whatever follows, so a trailing
    diagnostic line cannot cost us a reading we already have in hand.
    """
    start = stdout.find("[")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(stdout[start:])
    except json.JSONDecodeError:  # stx-allow: fallback (reason: unparseable output is "could not tell", which this module renders as silence)
        return None
    return data if isinstance(data, list) else None


def _parse_stamp(value: object) -> "datetime | None":
    """Parse a store timestamp, tolerating the trailing ``Z`` form."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:  # stx-allow: fallback (reason: one unreadable stamp must not cost the whole count)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _card_stamp(row: dict) -> "datetime | None":
    for field in _STAMP_FIELDS:
        stamp = _parse_stamp(row.get(field))
        if stamp is not None:
            return stamp
    return None


def _is_awaiting_operator(row: object) -> bool:
    """Re-assert the predicate on the rows we actually received.

    The command already filters, so this is normally a no-op — but it means
    the number is defined HERE, in code, and cannot be inflated by an
    overridden command that answers with the whole board.
    """
    return (
        isinstance(row, dict)
        and row.get("status") == BLOCKED_STATUS
        and row.get("blocker") == OPERATOR_BLOCKER
    )


def summarize(rows: list, now: "datetime | None" = None) -> "tuple[int, int | None]":
    """Return ``(count, oldest_age_in_days)`` for the awaiting-operator rows.

    ``oldest_age_in_days`` is ``None`` when no row carries a readable stamp —
    a count with no age is still worth printing, an invented age is not.
    """
    moment = now or datetime.now(timezone.utc)
    waiting = [row for row in rows if _is_awaiting_operator(row)]
    ages = [
        (moment - stamp).days
        for stamp in (_card_stamp(row) for row in waiting)
        if stamp is not None
    ]
    return len(waiting), max(ages) if ages else None


def render(count: int, oldest_days: "int | None", store: str = "") -> str:
    """The one line. Empty when there is nothing to report.

    The AGE is the part that does the work. A count alone reads as steady
    state; "oldest 24 days" reads as a problem — which is what it is.

    The STORE is the part that keeps the number honest. A count with no named
    source is unfalsifiable: it looks identical whether it came from the live
    board or from a database nothing has written to since yesterday morning.
    """
    if count <= 0:
        return ""
    if oldest_days is None:
        age = ""
    elif oldest_days <= 0:
        age = " (oldest today)"
    elif oldest_days == 1:
        age = " (oldest 1 day)"
    else:
        age = f" (oldest {oldest_days} days)"
    line = f"⏸ {count} card(s) awaiting the operator{age} — surface or reclassify"
    return f"{line}\n   read from {store}" if store else line


def _read_board(agent: str) -> "tuple[str, int, str]":
    """Read the board. Returns ``(line, count, store)``; silent on any failure.

    The store is resolved ONLY when there is something to say, so a clean
    board costs one subprocess rather than two.
    """
    argv = query_argv(agent)
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            # The hook's own stdin carries the Stop payload. A reader that
            # inherited it could consume or block on that pipe, which would
            # turn a REPORT into a broken stop path.
            stdin=subprocess.DEVNULL,
        )
    except (
        OSError,
        subprocess.SubprocessError,
    ):  # stx-allow: fallback (reason: a missing/hanging/crashing reader must degrade to today's behaviour, silently)
        return "", 0, ""
    if proc.returncode != 0:
        return "", 0, ""
    rows = _rows_from(proc.stdout or "")
    if rows is None:
        return "", 0, ""
    count, oldest = summarize(rows)
    store = _store_identity() if count > 0 else ""
    return render(count, oldest, store), count, store


def _read_cache(path: Path) -> "tuple[float, str] | None":
    try:
        data = json.loads(path.read_text())
    except (
        OSError,
        json.JSONDecodeError,
    ):  # stx-allow: fallback (reason: no cache and a corrupt cache are the same fact — take a fresh reading)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return float(data.get("checked_at_epoch")), str(data.get("line") or "")
    except (TypeError, ValueError):  # stx-allow: fallback (reason: a cache entry we cannot read is no cache entry)
        return None


def _write_cache(
    path: Path, epoch: float, line: str, count: int = 0, store: str = ""
) -> None:
    """Persist the reading — INCLUDING an empty one.

    Caching the empty answer is not an optimisation, it is the fail-open
    budget: an unreadable board would otherwise pay the full timeout on every
    single stop attempt, which is how a hook earns a latency reputation and
    gets switched off.

    ``count`` and ``store`` are recorded even when the rendered line is empty.
    A ZERO IS THE ONE ANSWER THAT PRINTS NOTHING, and a zero read from an
    abandoned store looks exactly like a clean board — so the file keeps the
    audit trail the line cannot: what was counted, and where it was read from.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "checked_at_epoch": epoch,
                    "checked_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch)
                    ),
                    "count": count,
                    "store": store,
                    "line": line,
                },
                indent=2,
            )
            + "\n"
        )
    except OSError:  # stx-allow: fallback (reason: an unwritable runtime dir costs us caching, never the stop)
        pass


def notice(agent: str) -> str:
    """The awaiting-operator line for ``agent``, or ``""``. NEVER raises.

    Every failure — no identity, no reader, a refused read, a timeout, an
    unwritable cache, a defect in this module — returns the empty string and
    prints NOTHING. A hook that breaks the stop path breaks everything, so
    this one is allowed exactly one way to fail: quietly.
    """
    # stx-allow: fallback (reason: this is a REPORT on the stop path; no defect in it may ever reach the agent as an error or an exception)
    try:
        if not agent:
            return ""
        ttl = _ttl_seconds()
        path = cache_path(agent)
        now = time.time()
        if ttl > 0:
            cached = _read_cache(path)
            if cached is not None and 0 <= now - cached[0] < ttl:
                return cached[1]
        line, count, store = _read_board(agent)
        _write_cache(path, now, line, count, store)
        return line
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see the docstring above)
        return ""


__all__ = [
    "BLOCKED_STATUS",
    "CMD_ENV",
    "OPERATOR_BLOCKER",
    "STORE_CMD_ENV",
    "TTL_ENV",
    "cache_path",
    "notice",
    "query_argv",
    "redact",
    "render",
    "store_argv",
    "summarize",
]
