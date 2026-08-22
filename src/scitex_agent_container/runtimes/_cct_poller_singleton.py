"""ONE live Telegram poller per bot token — or say so. Three answers, never two.

THE FAULT THIS OBSERVES
-----------------------
Restarting an agent's container does NOT reliably kill the previous
``bun run …/telegram-server.ts`` poller: the container runtime leaves it
orphaned, attached to the HOST. The next start spawns a SECOND poller for the
SAME bot token. Telegram's ``getUpdates`` admits exactly ONE consumer per
token, so the pair enters a 409 conflict loop and the operator's messages are
dropped — silently, from the only side that matters.

This is documented in claude-code-telegrammer's ``ts/lib/takeover.ts`` and has
been since 2026-06-07, and it has never been instrumented. Every time the fleet
learned of a 409 storm it learned because ONE agent happened to read its own
log during an incident. That is a coincidence, not a measurement.

Operator, 2026-08-22 (Telegram 13379):
「ポーラーが ophan になってしまうことがあるんですかね。sac が持って置くべきで、
sac のプロセスとかならず１対１対応するように制限しなくてはいけないような。」
— approved as 「１トークン１ポーラーですか、はい、お願いします。」

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
A DETECTOR. It reads ``/proc`` and returns a verdict. It never kills, signals,
locks, reaps or otherwise touches a process, and it changes NOTHING about the
start/stop path. It answers one question — *is the 1:1 invariant holding on
this host right now?* — and the eventual enforcement is a separate change that
this makes verifiable. A fix without a detector cannot be shown to have worked.

THE INVARIANT
-------------
``count(distinct token fingerprints) == count(token-holding servers)``

One fingerprint carried by two live pids IS the fault, and is the only thing
reported as :data:`POLLER_VIOLATION`.

The population is the SERVER (``telegram-server.ts``), not a poller process,
and that choice is load-bearing: measured 2026-08-22, a host with a live
token conflict had NO poller process at all — the rival holding the contended
token was a server. A server holding a real token is a poller-in-waiting, so a
poller census sees the fault only AFTER it becomes one.

A server carrying a DELIBERATELY EMPTY token holds no token at all and is
excluded from the invariant rather than clouding it — see
:data:`.._cct_poller_scan.TOKEN_DISABLED` for the measurement that forced
that distinction.

THREE-VALUED, AND THE THIRD VALUE IS LOAD-BEARING
-------------------------------------------------
``/proc/<pid>/environ`` is OWNER-ONLY. A poller belonging to another uid is a
poller whose token sac cannot read, and "I could not read it" is not "there is
no duplicate" — the unread one could be the twin of a read one. So a live
poller with no resolvable token yields :data:`POLLER_UNKNOWN`, never
:data:`POLLER_OK`. Folding that into OK would make the detector quietest
exactly when it is blindest, which is the most common bug we ship.

A scan that could not run at all (no readable ``/proc``) is UNKNOWN for the
same reason: zero pollers found because nobody looked is not zero pollers.

The observation half — which processes are pollers, and what token fingerprint
each holds — lives in :mod:`._cct_poller_scan`, including the contract that no
token VALUE is ever stored, returned or printed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ._cct_poller_scan import (
    UNRESOLVED_ENVIRON,
    LivePoller,
    scan_live_pollers,
)
from ._cct_token_pool import _TOKEN_VAR

#: The invariant holds: every live poller resolved a token, and no two of them
#: resolved the same one. Zero live pollers is OK — nothing can conflict.
POLLER_OK = "ok"
#: Two or more live pollers share a bot-token fingerprint. This is the 409
#: conflict loop, observed rather than inferred.
POLLER_VIOLATION = "violation"
#: sac could not assert the invariant: a live poller's token was unreadable, or
#: the process scan itself could not run. NOT a soft OK.
POLLER_UNKNOWN = "unknown"

#: The limit of this check, stated in every result rather than left to be
#: discovered. Telegram permits one ``getUpdates`` consumer per token
#: GLOBALLY, and this reads ONE host's ``/proc``. A cross-host duplicate is
#: real and this cannot see it: measured 2026-08-22, fingerprint
#: ``00ec09b9ad73`` was held on compute-04 AND compute-03 at once, and a
#: per-host probe returned OK on both while the fault was live across them.
#:
#: The PID namespace is the same trap one level down: run inside an apptainer
#: container with ``--containall``, ``/proc`` shows only that container's
#: processes — one server where the host has ten — and the small number reads
#: exactly like a clean host. Run it on the HOST.
SCOPE_NOTE = (
    "HOST-SCOPED, and Telegram's one-consumer-per-token rule is GLOBAL: a "
    "duplicate split across two hosts is invisible here and an ok on one host "
    "is not a fleet all-clear. Run this on the HOST, not inside a container — "
    "a --containall PID namespace shows only its own processes, which reads "
    "like a clean host."
)


@dataclass(frozen=True)
class DuplicatePollers:
    """Two or more live pids polling the SAME bot token. The fault itself."""

    token_fp: str
    pids: tuple[int, ...]
    agents: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "token_fp": self.token_fp,
            "pids": list(self.pids),
            "agents": list(self.agents),
        }


@dataclass(frozen=True)
class PollerSingletonVerdict:
    """The host-wide verdict. ``state`` is OK / VIOLATION / UNKNOWN."""

    state: str
    pollers: tuple[LivePoller, ...] = ()
    duplicates: tuple[DuplicatePollers, ...] = ()
    unresolved_pids: tuple[int, ...] = ()
    #: Live servers deliberately given an EMPTY token. Reported, never counted
    #: against the invariant — see :data:`.._cct_poller_scan.TOKEN_DISABLED`.
    disabled_pids: tuple[int, ...] = ()
    #: False when the process scan itself could not run — the reason a zero
    #: count must not read as OK.
    scanned: bool = True
    proc_root: str = "/proc"
    detail: str = ""

    @property
    def is_alarming(self) -> bool:
        """True for the two states a human must be told about."""
        return self.state in (POLLER_VIOLATION, POLLER_UNKNOWN)

    @property
    def distinct_fingerprints(self) -> int:
        """How many distinct bot tokens the live pollers resolved to."""
        return len({p.token_fp for p in self.pollers if p.token_fp})

    @property
    def blind_pids(self) -> tuple[int, ...]:
        """Unresolved pids whose ``environ`` sac could not READ at all.

        The subset of :attr:`unresolved_pids` that is a VANTAGE problem rather
        than a process started outside sac's env. The two need different
        remedies, so :meth:`hint` branches on this rather than on prose.
        """
        return tuple(
            p.pid
            for p in self.pollers
            if not p.resolved and p.reason == UNRESOLVED_ENVIRON
        )

    def population(self) -> str:
        """What was actually examined. Never let a clean count stand alone.

        "0 violations" means nothing until the same reading says how much was
        looked at: ``0 across 0 examined`` and ``0 across 10`` are different
        facts and must not render the same. scitex-hub's rule, adopted after
        it caught itself reporting "0 x 409 Conflict" over a window in which
        nothing at all had happened.
        """
        return (
            f"{len(self.pollers)} live server(s) examined, "
            f"{self.distinct_fingerprints} real token(s), "
            f"{len(self.disabled_pids)} deliberately tokenless, "
            f"{len(self.unresolved_pids)} unreadable"
        )

    def summary(self) -> str:
        """One-line human summary of the verdict."""
        live = len(self.pollers)
        if self.state == POLLER_VIOLATION:
            worst = ", ".join(
                f"{d.token_fp} held by pids {'+'.join(str(p) for p in d.pids)}"
                for d in self.duplicates
            )
            return f"{len(self.duplicates)} duplicated bot token(s): {worst}"
        if self.state == POLLER_UNKNOWN:
            if not self.scanned:
                return "unknown — the process scan could not run"
            return (
                f"unknown — {len(self.unresolved_pids)} of {live} live "
                "poller(s) would not yield a token"
            )
        disabled = (
            f", {len(self.disabled_pids)} deliberately tokenless"
            if self.disabled_pids
            else ""
        )
        return (
            f"{live} live server(s) on this host, "
            f"{self.distinct_fingerprints} distinct token(s){disabled}"
        )

    def hint(self) -> str:
        """What to DO. Empty for OK — an all-clear needs no remedy.

        The two alarming states need DIFFERENT actions: a violation is a live
        outage to end by hand, an unknown is an unread instrument whose vantage
        point must be fixed first. Handing out one sentence for both is how a
        hint stops being read.
        """
        if self.state == POLLER_VIOLATION:
            offenders = sorted({p for d in self.duplicates for p in d.pids})
            pids = ", ".join(str(p) for p in offenders)
            return (
                "Two or more pollers hold the same bot token, so Telegram is "
                "409-ing both and the operator's inbound messages are being "
                "dropped. To end it NOW: for each duplicated fingerprint, find "
                f"which of pids {pids} is a CHILD of that agent's current "
                "`claude` process (`ps -o pid,ppid,lstart,args -p <pid>`; the "
                "orphan is the one whose parent is 1, or an exited container), "
                "then SIGKILL the orphan(s), leaving exactly one. Confirm by "
                "re-running this check: it must read `ok`. sac does NOT do this "
                "for you — this is a detector; the reaper is a separate change."
            )
        if self.state == POLLER_UNKNOWN:
            if not self.scanned:
                return (
                    f"The process scan could not run against {self.proc_root}, "
                    "so NOTHING was learned — this is not an all-clear. Re-run "
                    "on a Linux host with a readable /proc."
                )
            pids = ", ".join(str(p) for p in self.unresolved_pids)
            head = (
                f"A live poller yielded no {_TOKEN_VAR}, so sac cannot assert "
                "one-poller-per-token: the unread process could be the twin of "
                f"a read one. Affected pid(s): {pids}. "
            )
            if self.blind_pids:
                return head + (
                    "`/proc/<pid>/environ` is OWNER-ONLY, so the cause here is "
                    "VANTAGE — pids "
                    + ", ".join(str(p) for p in self.blind_pids)
                    + " belong to another uid. Re-run as the user that owns "
                    "those processes (or as root) before believing any row."
                )
            return head + (
                "Their environments ARE readable and simply carry no "
                f"{_TOKEN_VAR}: these pollers were started outside sac's env, "
                "so sac has nothing to match them on. Identify them by hand "
                "(`ps -o pid,ppid,lstart,args -p <pid>`) and either restart "
                "them through sac or stop them."
            )
        return ""

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces).

        Same shape as the doctor's neighbouring drift check
        (:meth:`.._drift.DriftStatus.to_dict`) — ``state`` / ``detail`` /
        ``summary`` plus this check's own fields — with ``hint`` added because
        a failing check that does not say what to do is a check people learn
        to scroll past, and ``scope_note`` because an unstated limit is the
        same defect as a wrong hint.
        """
        return {
            "state": self.state,
            "scope": "host",
            "scope_note": SCOPE_NOTE,
            "live_pollers": len(self.pollers),
            "distinct_fingerprints": self.distinct_fingerprints,
            "population": self.population(),
            "pollers": [p.to_dict() for p in self.pollers],
            "duplicates": [d.to_dict() for d in self.duplicates],
            "unresolved_pids": list(self.unresolved_pids),
            "disabled_pids": list(self.disabled_pids),
            "scanned": self.scanned,
            "proc_root": self.proc_root,
            "detail": self.detail,
            "summary": self.summary(),
            "hint": self.hint(),
        }


def group_duplicates(pollers: Sequence[LivePoller]) -> tuple[DuplicatePollers, ...]:
    """Fingerprints held by two or more live pids, in fingerprint order.

    Pure over its input — the seam that lets the duplicate condition be
    constructed and asserted without a process, next to the test that
    constructs it with two real ones.
    """
    by_fp: dict[str, list[LivePoller]] = {}
    for poller in pollers:
        if poller.token_fp:
            by_fp.setdefault(poller.token_fp, []).append(poller)
    return tuple(
        DuplicatePollers(
            token_fp=fp,
            pids=tuple(p.pid for p in group),
            agents=tuple(dict.fromkeys(p.agent for p in group if p.agent)),
        )
        for fp, group in sorted(by_fp.items())
        if len(group) > 1
    )


def verdict_for(
    pollers: Sequence[LivePoller],
    *,
    proc_root: str = "/proc",
) -> PollerSingletonVerdict:
    """Decide OK / VIOLATION / UNKNOWN for an already-observed population.

    A VIOLATION outranks an UNKNOWN: a duplicate that HAS been observed is a
    fact, and an unread third process does not make it less true. The reverse
    ordering would let one unreadable poller mute a live outage.
    """
    pollers = tuple(pollers)
    duplicates = group_duplicates(pollers)
    disabled = tuple(p.pid for p in pollers if p.disabled)
    unresolved = tuple(p.pid for p in pollers if not p.resolved and not p.disabled)
    distinct = len({p.token_fp for p in pollers if p.token_fp})

    if duplicates:
        return PollerSingletonVerdict(
            state=POLLER_VIOLATION,
            pollers=pollers,
            duplicates=duplicates,
            unresolved_pids=unresolved,
            disabled_pids=disabled,
            proc_root=proc_root,
            detail=(
                f"{len(pollers)} live poller(s) under {proc_root} resolve to "
                f"only {distinct} distinct bot token(s). Telegram's getUpdates "
                "admits ONE consumer per token, so every duplicated "
                "fingerprint below is a live 409 conflict loop in which the "
                "operator's inbound messages are dropped."
            ),
        )

    if unresolved:
        blind = [p for p in pollers if not p.resolved and not p.disabled]
        why = "; ".join(f"pid {p.pid}: {p.detail}" for p in blind)
        return PollerSingletonVerdict(
            state=POLLER_UNKNOWN,
            pollers=pollers,
            unresolved_pids=unresolved,
            disabled_pids=disabled,
            proc_root=proc_root,
            detail=(
                f"{len(unresolved)} of {len(pollers)} live server(s) yielded no "
                f"{_TOKEN_VAR}, so the one-poller-per-token invariant cannot be "
                "asserted: an unread server could hold the same token as a read "
                f"one. {why} ({SCOPE_NOTE})"
            ),
        )

    if not pollers:
        return PollerSingletonVerdict(
            state=POLLER_OK,
            proc_root=proc_root,
            detail=(
                f"the scan ran against {proc_root} and found no live Telegram "
                f"server. Nothing can conflict with nothing. ({SCOPE_NOTE})"
            ),
        )

    return PollerSingletonVerdict(
        state=POLLER_OK,
        pollers=pollers,
        disabled_pids=disabled,
        proc_root=proc_root,
        detail=(
            f"{len(pollers)} live server(s) under {proc_root}; "
            f"{distinct} hold a real bot token and no two hold the same one "
            f"({len(disabled)} carry a deliberately EMPTY token and hold none, "
            "so they cannot collide with anything). count(distinct "
            f"fingerprints) == count(token-holding servers). ({SCOPE_NOTE})"
        ),
    )


def check_poller_singleton(
    *,
    proc_root: Path | None = None,
    self_pid: int | None = None,
) -> PollerSingletonVerdict:
    """Is there MORE THAN ONE live Telegram poller per bot token on this host?

    Returns a three-valued verdict:

    * :data:`POLLER_OK` — every live poller resolved a token and no two share
      one, i.e. ``count(distinct fingerprints) == count(live pollers)``. Zero
      live pollers is OK: nothing can conflict.
    * :data:`POLLER_VIOLATION` — at least one fingerprint is held by two or
      more live pids. Both pids, the fingerprint, and the owning agents where
      determinable are reported.
    * :data:`POLLER_UNKNOWN` — the scan could not run, or a live poller's token
      could not be read. Never folded into OK.

    Read-only. Enumerates ``proc_root`` and returns; touches no process.
    """
    root = Path(proc_root) if proc_root is not None else Path("/proc")
    # stx-allow: fallback (reason: a proc root that cannot be enumerated is the
    # UNKNOWN verdict this function exists to be able to give — reporting zero
    # pollers because nobody looked is the exact collapse this detector
    # refuses, so the failure must become a state, not an exception.)
    try:
        pollers = scan_live_pollers(proc_root=root, self_pid=self_pid)
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        return PollerSingletonVerdict(
            state=POLLER_UNKNOWN,
            scanned=False,
            proc_root=str(root),
            detail=(
                f"{root} could not be enumerated ({exc}), so no poller was "
                "observed at all. Nothing was learned; this is not an "
                "all-clear."
            ),
        )
    return verdict_for(pollers, proc_root=str(root))


__all__ = [
    "POLLER_OK",
    "POLLER_UNKNOWN",
    "POLLER_VIOLATION",
    "SCOPE_NOTE",
    "DuplicatePollers",
    "PollerSingletonVerdict",
    "check_poller_singleton",
    "group_duplicates",
    "verdict_for",
]
