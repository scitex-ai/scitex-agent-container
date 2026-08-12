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


def render(count: int, oldest_days: "int | None") -> str:
    """The one line. Empty when there is nothing to report.

    The AGE is the part that does the work. A count alone reads as steady
    state; "oldest 24 days" reads as a problem — which is what it is.
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
    return f"⏸ {count} card(s) awaiting the operator{age} — surface or reclassify"


def _read_board(agent: str) -> str:
    """Run the read command and render its answer. Silent on every failure."""
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
        return ""
    if proc.returncode != 0:
        return ""
    rows = _rows_from(proc.stdout or "")
    if rows is None:
        return ""
    return render(*summarize(rows))


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


def _write_cache(path: Path, epoch: float, line: str) -> None:
    """Persist the reading — INCLUDING an empty one.

    Caching the empty answer is not an optimisation, it is the fail-open
    budget: an unreadable board would otherwise pay the full timeout on every
    single stop attempt, which is how a hook earns a latency reputation and
    gets switched off.
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
        line = _read_board(agent)
        _write_cache(path, now, line)
        return line
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see the docstring above)
        return ""


__all__ = [
    "BLOCKED_STATUS",
    "CMD_ENV",
    "OPERATOR_BLOCKER",
    "TTL_ENV",
    "cache_path",
    "notice",
    "query_argv",
    "render",
    "summarize",
]
