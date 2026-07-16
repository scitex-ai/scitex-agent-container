"""Rate limits: a restart LOOP is worse than a down agent.

A down agent is one dead agent. A reconciler that restarts it every five
minutes forever is a host-wide resource fire that also buries the real
cause under a wall of identical log lines — and it never converges,
because whatever killed the agent is still there. So every constant here
exists to make this enforcer GIVE UP and ask a human instead of trying
harder.

The numbers are not invented: they are the ones already proven in
production by ``~/.scitex/agent-container/bin/auth-heal.py``, the fleet's
only other auto-restarter, which has been bouncing auth-stuck agents on a
cron since 2026-06-01. Sharing its constants means the two restarters
back off on the same timescale instead of harmonising into a beat.

Persistence: the pass is a SHORT-LIVED cron process, so its memory of
"when did I last restart X" cannot live in RAM (that is precisely how
``health_monitor``'s in-process retry counter evaporated). It lives in a
small JSON file next to auth-heal's own state, written atomically.

WHY NOT ``restart.max_retries``
-------------------------------
The spec field exists and this module deliberately ignores it.
``max_retries`` was designed for :func:`.._lifecycle.health.health_monitor`
— an in-process supervisor with a SESSION-scoped counter that resets to 0
on any healthy check and gives up permanently at the cap. Neither half of
that translates to a stateless cron pass: there is no session to scope the
count to, and "gives up permanently" across process boundaries would mean
an agent that crashed 3 times last March may never be restarted again.
A sliding time window is the honest cross-process equivalent, so that is
what this uses. ``max_retries`` stays unimplemented rather than being
silently reinterpreted into something its name does not mean.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

__all__ = [
    "Budget",
    "BudgetCheck",
    "DEBOUNCE_S",
    "DEFAULT_PASS_CAP",
    "HistoryRead",
    "HistoryState",
    "MAX_RESTARTS_PER_AGENT_PER_HOUR",
    "history_path",
    "load_history",
    "read_history",
    "save_history",
]

#: Minimum seconds between two auto-restarts of the SAME agent. Mirrors
#: ``auth-heal.py``'s ``RESTART_DEBOUNCE_S``. Also the boot grace: an agent
#: restarted 3 minutes ago that is not back yet is still BOOTING, and
#: restarting it again would kill the recovery in progress.
DEBOUNCE_S = 1_800.0

#: Hard ceiling per agent per rolling hour. ``DEBOUNCE_S`` alone already
#: implies <=2/h, so this is belt AND braces — it is reachable on its own
#: (restarts at T-3500s and T-1900s: the debounce passes, the hour does
#: not), and it is the leg that turns a persistently-dying agent into a
#: BOARD CARD instead of an infinite 30-minute bounce.
MAX_RESTARTS_PER_AGENT_PER_HOUR = 2

#: Rolling window the per-agent count is measured over.
_WINDOW_S = 3_600.0

#: Most restarts ONE pass may perform, across the whole fleet. The blast
#: radius of a single bad tick: if a tmux hiccup ever made the fleet look
#: dead, this is the difference between 10 needless restarts and 93. The
#: remainder is not lost, only deferred — the next pass (5-10 min later)
#: picks it up, so a genuine fleet-wide outage still drains, just slowly
#: enough for a human to notice and hit the brakes.
DEFAULT_PASS_CAP = 10


#: Explicit override for WHERE the history lives, so the operator can pin
#: it into a failure domain that does NOT die with the thing it watches.
#: The default sits under ``$SCITEX_DIR`` — which on at least one host
#: (Spartan) is a SYMLINK into a project whose membership can be revoked,
#: taking ``~/.scitex`` to permission-denied for every fresh process. State
#: that shares a failure domain with the fault it is meant to catch is not
#: state, it is a coincidence.
_HISTORY_ENV = "SAC_RECONCILE_HISTORY"


def history_path() -> Path:
    """Where the restart history lives. Resolved PER CALL, never cached.

    A module-level constant here would be baked at import from an env var
    that tests set afterwards — the exact trap
    :mod:`.._state.state_paths` documents having paid for (a fixture that
    set ``$HOME`` and silently did nothing, leaving the suite reading real
    fleet state).
    """
    override = os.environ.get(_HISTORY_ENV)
    if override:
        return Path(override).expanduser()
    from .._state.state_paths import runtime_root

    return runtime_root() / "fleet-reconcile-history.json"


class HistoryState(str, Enum):
    """Could we read our own memory — and is that answer trustworthy?

    FOUR states, not two, and the distinction is the whole point.
    ``path.read_text()`` raising is NOT one condition: a file that was never
    written and a file we are FORBIDDEN to read are opposite facts, and
    ``except OSError: return {}`` renders them identical.

    That collapse is not theoretical. This history is the ONLY memory of
    what we have already restarted, so "no memory" silently disarms BOTH
    the debounce and the hourly cap: every corpse becomes restartable on
    every 5-minute tick, forever. A permission error on one small JSON file
    would therefore turn the enforcer into precisely the restart LOOP this
    module exists to prevent — and it would do it quietly, because an empty
    dict looks exactly like a healthy first run.
    """

    OK = "ok"  # read and parsed — the budget is enforceable
    FIRST_RUN = "first-run"  # genuinely absent AND we proved we can create it
    DENIED = "denied"  # it (or its dir) is there but not ours to read/write
    UNREADABLE = "unreadable"  # present but corrupt — we HAVE a memory, unusable


@dataclass(frozen=True)
class HistoryRead:
    """The history, plus whether we may act on it at all."""

    state: HistoryState
    history: dict[str, list[float]]
    detail: str = ""

    @property
    def enforceable(self) -> bool:
        """May the pass restart anything on the strength of this read?

        ONLY when we hold a trustworthy memory. ``DENIED`` / ``UNREADABLE``
        mean the rate limits cannot be enforced, and an UNENFORCEABLE budget
        is not a budget — proceeding would be a restart loop wearing a
        budget's clothes. A first run is fine: absent-and-creatable really
        does mean nothing has been restarted yet.
        """
        return self.state in (HistoryState.OK, HistoryState.FIRST_RUN)


def _prove_writable(path: Path) -> HistoryRead:
    """A missing history is a normal first run — IF we can really create it.

    ``FileNotFoundError`` is itself ambiguous: the file was never written
    (fine), or the whole tree it lives in has vanished/been revoked (very
    much not fine, and indistinguishable from the outside). So we do not
    assume — we PROVE, by writing the empty history now. The probe IS the
    operation, which is the only proof that cannot be stale: ``os.access``
    would answer from the permission bits and can still be wrong about NFS,
    ACLs and revoked mounts.
    """
    # stx-allow: fallback (reason: this IS the writability probe — the raise is the answer, converted to a DENIED verdict the caller must alarm on rather than silently treat as an empty memory)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    except OSError as exc:
        return HistoryRead(
            HistoryState.DENIED,
            {},
            f"{path} does not exist and CANNOT be created ({exc}) — so a "
            f"restart could not be recorded, and an unrecordable restart is "
            f"an unbounded one",
        )
    return HistoryRead(HistoryState.FIRST_RUN, {}, "")


def read_history(path: Path) -> HistoryRead:
    """Read ``{agent: [restart_epoch, ...]}`` — three-state honest.

    Never raises. Never invents an empty memory out of a failure to read
    one: see :class:`HistoryState` for why that distinction is the
    difference between an enforcer and a restart loop.
    """
    # stx-allow: fallback (reason: each failure mode maps to a DISTINCT state the caller must handle — this is the three-state read itself, not a swallow)
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return _prove_writable(path)
    except PermissionError as exc:
        return HistoryRead(
            HistoryState.DENIED,
            {},
            f"cannot READ {path} ({exc}) — the memory of what we have "
            f"already restarted EXISTS but is not ours to read. Treating "
            f"that as 'nothing restarted yet' would disarm the debounce and "
            f"the hourly cap on every tick",
        )
    except OSError as exc:
        return HistoryRead(HistoryState.UNREADABLE, {}, f"cannot read {path} ({exc})")

    # stx-allow: fallback (reason: a corrupt history is UNREADABLE, not empty — we demonstrably HAVE a memory and cannot parse it, so the budget is unenforceable)
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return HistoryRead(
            HistoryState.UNREADABLE,
            {},
            f"{path} is present but not parseable JSON ({exc}) — we HAVE a "
            f"restart memory and cannot read it. Delete the file to reset "
            f"the budget deliberately",
        )
    if not isinstance(parsed, dict):
        return HistoryRead(
            HistoryState.UNREADABLE,
            {},
            f"{path} is valid JSON but not an object — refusing to guess",
        )
    out: dict[str, list[float]] = {}
    for name, stamps in parsed.items():
        if isinstance(stamps, list):
            out[str(name)] = [float(t) for t in stamps if isinstance(t, (int, float))]
    return HistoryRead(HistoryState.OK, out)


def load_history(path: Path) -> dict[str, list[float]]:
    """The plain history dict. Prefer :func:`read_history` — it is honest.

    Kept as the ergonomic reader for callers that have ALREADY established
    the read is enforceable. It cannot distinguish a denied read from a
    first run, so it must never gate a restart on its own.
    """
    return read_history(path).history


def save_history(path: Path, history: dict[str, list[float]], *, now: float) -> None:
    """Write the history atomically, pruned to the rolling window.

    Pruning at ``_WINDOW_S`` is safe because it is the LONGER of the two
    horizons this module reasons over (``DEBOUNCE_S`` is 1800s), so nothing
    the guards need is ever discarded. tmp+replace so a crash mid-write
    cannot leave a truncated file that reads back as "no memory".
    """
    pruned = {
        name: sorted(t for t in stamps if now - t < _WINDOW_S)
        for name, stamps in history.items()
    }
    pruned = {name: stamps for name, stamps in pruned.items() if stamps}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(pruned, indent=2, sort_keys=True))
    tmp.replace(path)


@dataclass(frozen=True)
class BudgetCheck:
    """May we restart this agent? — and, when not, WHY in plain words."""

    allowed: bool
    reason: str
    detail: str


@dataclass
class Budget:
    """The rolling per-agent restart history, plus this pass's own cap.

    Not thread-safe and not meant to be: one pass, one process, one
    sequential sweep.
    """

    history: dict[str, list[float]]
    pass_cap: int = DEFAULT_PASS_CAP
    spent: int = 0

    def _recent(self, name: str, now: float) -> list[float]:
        return [t for t in self.history.get(name, []) if now - t < _WINDOW_S]

    def check(self, name: str, now: float) -> BudgetCheck:
        """Three guards, checked cheapest-blast-radius first."""
        recent = self._recent(name, now)
        if recent:
            since = now - max(recent)
            if since < DEBOUNCE_S:
                return BudgetCheck(
                    False,
                    "debounce",
                    f"restarted {int(since)}s ago and the debounce is "
                    f"{int(DEBOUNCE_S)}s — it is either still booting or "
                    f"something is killing it faster than we can fix it; "
                    f"either way another restart now would not help",
                )
        if len(recent) >= MAX_RESTARTS_PER_AGENT_PER_HOUR:
            return BudgetCheck(
                False,
                "over-budget",
                f"already auto-restarted {len(recent)}x in the last hour "
                f"(cap {MAX_RESTARTS_PER_AGENT_PER_HOUR}) and it is STILL "
                f"down — restarting is not fixing this and a loop is worse "
                f"than a down agent. A human needs to look",
            )
        if self.spent >= self.pass_cap:
            return BudgetCheck(
                False,
                "pass-cap",
                f"this pass has already restarted {self.spent} agent(s) (cap "
                f"{self.pass_cap}) — refusing to storm the host on one tick. "
                f"The next pass will pick this up",
            )
        return BudgetCheck(True, "within-budget", "")

    def record(self, name: str, now: float) -> None:
        """Remember a restart we actually performed. Spends pass budget."""
        self.history.setdefault(name, []).append(now)
        self.spent += 1
