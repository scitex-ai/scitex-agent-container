"""A ZERO MUST NAME ITS CAUSE — splitting *deaf* from *stopped*.

``inbox_subscribers == 0`` is published on every ``GET /agents`` row by
:mod:`._reachability`, and that module is emphatic that the number is
CONFOUNDED: a zero means the agent's inbox adapter is detached **or** that the
agent is not running at all, and the broker cannot tell you which. It says so,
in its own docstring, and then hands the caller the zero anyway.

Every consumer since has had to remember that warning, and the record says they
do not:

* **2026-07-14** — three agents independently read a wall of zeros as a deaf
  fleet and escalated a P0. Every agent in their lists was simply STOPPED.
  Three agreeing reports were one instrument read three times.
* **2026-08-12** — measured on this host: 15 registry rows, 9 reporting
  ``inbox_reachable: unreachable``, and **all 9 had no tmux session and no
  ``sac mcp channel`` process**. They were stopped, not deaf. A fourth agent
  reported the same zeros as evidence that "reach decays with uptime".

The confound is not a documentation problem. A field whose correct reading
requires remembering a docstring will be misread, because the surface that
publishes it — the peer-discovery route an agent consults *before handing over
work* — is exactly where a caller is in a hurry.

So this module resolves the confound at the source, by pairing the count with
the one instrument that IS independent of the broker's bookkeeping: **the
host's tmux session table** (``._lifecycle._verdict_tmux``). Two observations,
one verdict:

======================  ====================  ==========================
session observed        inbox_subscribers     ``fault``
======================  ====================  ==========================
present                 ``0``                 :data:`FAULT_DEAF_INBOX`
absent (tui runtime)    anything              :data:`FAULT_NOT_RUNNING`
anything else                                 ``None``
======================  ====================  ==========================

:data:`FAULT_DEAF_INBOX` is the fault this fleet could not previously NAME: an
agent that is *running* and *unreachable at once*. It is worse than a stopped
one, because every surface reports it green, work is routed to it, and the work
evaporates.

Two rules inherited from :mod:`._reachability`, and not negotiable here
-----------------------------------------------------------------------
1. **Only a POSITIVE observation convicts.** A snapshot we could not take
   (wedged tmux, or the container-blindness trap) yields no fault at all —
   never :data:`FAULT_NOT_RUNNING`. "I could not look" is not a death
   certificate, and this fleet has already paid for rendering it as one.
2. **A fault is a REPORT, never a trigger.** Nothing here may be wired to a
   restart. :data:`FAULT_NOT_RUNNING` describes a stale registry row, whose
   remedy is a deliberate operator action; :data:`FAULT_DEAF_INBOX` describes a
   *live session* whose adapter is detached, and restarting on it would destroy
   the healthy session it just correctly identified.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from ._reachability import UNKNOWN

__all__ = [
    "FAULT_DEAF_INBOX",
    "FAULT_NOT_RUNNING",
    "annotate_faults",
    "annotate_status_fault",
    "classify_fault",
    "session_snapshot",
]

log = logging.getLogger(__name__)

#: A live session whose inbox adapter is attached to nothing. The agent is up,
#: answering, holding a tmux session — and every ``a2a_send`` aimed at it fans
#: out to zero subscribers. THE fault this module exists to name.
FAULT_DEAF_INBOX = "deaf_inbox"

#: The registry row outlived the process. Not a delivery fault at all: there is
#: nothing to deliver TO. Named separately because the remedy is the opposite of
#: the deaf case — waiting will never help, since no adapter will reconnect.
FAULT_NOT_RUNNING = "not_running"

_DETAIL = {
    FAULT_DEAF_INBOX: (
        "RUNNING BUT DEAF — a live tmux session was observed for this agent AND "
        "its inbox stream has 0 subscribers. It is up, but every a2a_send aimed "
        "at it fans out to nobody. Messages are NOT lost (sac listen persists "
        "them to channel_events and the stream replays undelivered rows on the "
        "adapter's next connect), so do not re-send. Do NOT restart on this "
        "signal alone: the session is healthy and a restart would destroy it. "
        "Reach the agent by a rail that does not use its inbox adapter (a "
        "scitex-cards card assigned to it), or ask the operator."
    ),
    FAULT_NOT_RUNNING: (
        "NOT RUNNING ON {host} — this registry row declares an agent, and no "
        "live tmux session for it exists ON THIS HOST. The probe ran and "
        "observed a real absence HERE; it says nothing about any other host, "
        "because it can only see {host}'s sessions. If the agent is pinned "
        "elsewhere the row outlived the process ON THIS HOST ONLY — ASK THAT "
        "HOST before concluding it is down; `sac agents start <name>` run "
        "there reports whether it is already running. Once that is ruled out, "
        "the row outlived the process: its 0 inbox subscribers mean 'nobody "
        "is home', NOT 'a detached adapter', and nothing will reconnect to "
        "drain its queue until someone deliberately starts it."
    ),
}

#: Resolved once. ``gethostname`` is cheap, but this is formatted per ROW on a
#: fleet-sized response and the value cannot change within a process.
_THIS_HOST: str | None = None


def _this_host() -> str:
    """This daemon's hostname, for naming the population a probe covered.

    Falls back to the literal ``"this host"`` so the message degrades to the
    previous, still-honest phrasing rather than raising inside an ADVISORY
    overlay that must never fail the status route.
    """
    global _THIS_HOST
    if _THIS_HOST is None:
        try:
            import socket

            _THIS_HOST = socket.gethostname() or "this host"
        except Exception:  # stx-allow: fallback (reason: the fault overlay is advisory; a hostname lookup must never fail the status route)
            _THIS_HOST = "this host"
    return _THIS_HOST


def session_snapshot() -> dict | None:
    """One batched reading of the host's live tmux sessions, or ``None``.

    ``None`` means we were not in a position to look, and every downstream
    verdict must then be "no fault" rather than "everything is absent". Note
    the listen daemon runs on the HOST, which is precisely why it can take this
    reading at all — the same probe from inside a container sees an empty
    fleet and would slander all 15 rows at once.
    """
    try:
        from .._lifecycle._verdict_tmux import observed_session_snapshot

        return observed_session_snapshot()
    except Exception as exc:  # stx-allow: fallback (reason: a probe that blew up observed NOTHING — it must yield "could not look", never a fleet-wide absence verdict)
        log.warning(
            "inbox fault: could not take a tmux session snapshot (reporting NO "
            "faults rather than convicting every row of being absent): %s",
            exc,
        )
        return None


def _runtime_is_tui(name: str) -> bool | None:
    """Does ``name``'s spec declare the tmux-based ``tui`` runtime? Ternary.

    Delegates to the canonical answer
    (:func:`._agent_exec_liveness._runtime_writes_apptainer_pidfile`) rather
    than parsing the spec a second time, so this module and the spawn probe can
    never drift about which runtime an agent has. That function returns
    ``False`` for "writes no apptainer pidfile", which IS the tui runtime;
    ``True`` (apptainer) and ``None`` (unresolvable spec) both mean tmux cannot
    speak for this agent.
    """
    from ._agent_exec_liveness import _runtime_writes_apptainer_pidfile

    writes = _runtime_writes_apptainer_pidfile(name)
    if writes is None:
        return None
    return writes is False


def _session_present(
    row: Mapping[str, Any],
    snapshot: Mapping[str, Any],
    runtime_is_tui_fn: Callable[[str], bool | None],
) -> bool | None:
    """Is this row's tmux session live? ``None`` when absence proves nothing.

    Presence is read straight from the batched snapshot and needs no knowledge
    of the agent's runtime: a live ``tui-<name>`` session is positive evidence
    whoever launched it.

    ABSENCE is the asymmetric half. A non-TUI agent (apptainer, claude-session)
    legitimately holds no tmux session, so "not in the snapshot" is its NORMAL
    state and convicting on it would stamp :data:`FAULT_NOT_RUNNING` on every
    healthy container agent — the same shape as the pidfile probe that once
    convicted every TUI agent in the fleet, run in the opposite direction. So
    absence only counts when the spec POSITIVELY declares a tui runtime.
    """
    name = row.get("name")
    if not isinstance(name, str) or not name:
        return None
    if f"tui-{name}" in snapshot:
        return True
    if runtime_is_tui_fn(name) is True:
        return False
    return None


def classify_fault(
    row: Mapping[str, Any],
    *,
    snapshot: Mapping[str, Any] | None,
    runtime_is_tui_fn: Callable[[str], bool | None] | None = None,
) -> str | None:
    """Return this row's ``fault``, or ``None``.

    ``snapshot=None`` (we could not look) always returns ``None``: rule 1.

    A row whose ``inbox_reachable`` is :data:`._reachability.UNKNOWN` also
    returns ``None``. That value already encodes "this agent's bus is not ours
    to observe" — a remote host's agent, served by a different broker — and
    reusing it keeps the locality decision in the one module that owns it
    instead of re-deriving it here and risking drift.

    ``runtime_is_tui_fn`` is the seam for the spec lookup, so the RULE can be
    driven from a hand-rolled resolver without an on-disk spec, a tmux server
    or a listen daemon.
    """
    if snapshot is None:
        return None
    if row.get("inbox_reachable") == UNKNOWN:
        return None

    count = row.get("inbox_subscribers")
    observed = isinstance(count, int) and not isinstance(count, bool)

    # A LIVE SUBSCRIBER OUTRANKS A MISSING SESSION. The broker watched
    # something attach to this agent's inbox stream, which is first-hand proof
    # that a process is home; "no tmux session named tui-<name>" is an absence,
    # and an absence never beats a positive reading. They can legitimately
    # disagree — a session renamed out from under us, an adapter started
    # outside the tmux pane — and when they do, the fault must be NEITHER,
    # because a subscribed agent is by definition not deaf and demonstrably
    # not gone.
    if observed and count >= 1:
        return None

    alive = _session_present(row, snapshot, runtime_is_tui_fn or _runtime_is_tui)
    if alive is False:
        return FAULT_NOT_RUNNING
    if alive is not True:
        return None
    return FAULT_DEAF_INBOX if observed else None


def annotate_faults(
    rows: list[dict[str, Any]],
    *,
    snapshot: Mapping[str, Any] | None,
    runtime_is_tui_fn: Callable[[str], bool | None] | None = None,
) -> list[dict[str, Any]]:
    """Add ``fault`` / ``fault_detail`` to every row (idempotent, additive).

    ``fault`` is ALWAYS emitted — ``None`` for a healthy row — so the response
    shape stays uniform and a consumer can branch on the key's VALUE rather
    than on its presence. ``fault_detail`` is emitted only alongside a fault,
    and says what to do about it: a named fault whose remedy the reader has to
    guess is how a correct signal still ends in the wrong action.
    """
    out: list[dict[str, Any]] = []
    for row in rows:
        fault = classify_fault(
            row, snapshot=snapshot, runtime_is_tui_fn=runtime_is_tui_fn
        )
        new = dict(row)
        new["fault"] = fault
        if fault is not None:
            # `.format` on a template with no placeholders is a no-op, so the
            # DEAF entry is unaffected and needs no branch here.
            new["fault_detail"] = _DETAIL[fault].format(host=_this_host())
        out.append(new)
    return out


def annotate_status_fault(body: dict[str, Any]) -> dict[str, Any]:
    """:func:`annotate_faults` for a SINGLE-agent status body.

    ``GET /agents/<name>/status`` answered HTTP 200 with a full body — port,
    turn_url, ``inbox_reachable: unreachable`` — for agents that had not
    existed for two days, and carried no field anywhere saying so. This is the
    seam that fixes that, and it is the same classifier the list route uses, so
    the two surfaces cannot disagree about one agent.

    Advisory, like every path in this module: a failure returns the body
    unchanged rather than taking down the route the whole fleet uses to decide
    whether a peer is real.
    """
    try:
        return annotate_faults([body], snapshot=session_snapshot())[0]
    except Exception as exc:  # stx-allow: fallback (reason: the fault overlay is advisory — it must never fail the status route)
        log.warning(
            "agent_status: fault classification failed (returning the body "
            "without the `fault` overlay): %s",
            exc,
        )
        return body
