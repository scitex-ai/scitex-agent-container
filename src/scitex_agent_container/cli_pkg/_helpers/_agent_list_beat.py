"""Is this agent's heartbeat file still being WRITTEN?

Why a separate signal at all
----------------------------
:mod:`._agent_list_discover` synthesizes a ``defined`` row for every agent
that has a spec on disk but NO registry row, and states the premise inline:
"These agents are never LIVE". That premise is false. Measured on the live
fleet 2026-08-03, five agents were beating while rendered ``defined`` —
including ``scitex-agent-container`` itself, mid-execution, which is as
direct a refutation as the fleet can produce: the process generating the
reading was the process the reading denied.

An absent registry row means the registry forgot the agent. It does not
mean the agent stopped, and rendering it as a non-live pole asserts more
than we know.

Why the FILE MTIME and not the ``ts`` field inside it
-----------------------------------------------------
This is the whole reason this module exists, and it is counter-intuitive
enough to be worth stating: **the heartbeat record's own timestamp is not
when the heartbeat was written.** ``write_heartbeat`` accepts a ``ts``
override and the TUI runner passes the agent's *tmux pane-activity* epoch,
so ``ts`` answers "when did this pane last change", not "is this agent
alive". For an agent driven over a2a rather than by a human typing into
its pane, the two diverge without bound.

Measured the same day, on six agents that were unambiguously live:

    agent                    ts field    file mtime
    scitex-agent-container      3.4h         ~0s
    scitex-hpc                  3.0h         ~0s
    dotfiles                    3.2h         ~0s
    scitex-cards               10.0h         ~0s

``seconds_since_last_beat`` (12148.4 on a file written that second) and
``pid`` (``0``) are derived from the same stale epoch and are equally
unusable. The mtime is the only field that records the beat itself.

A threshold here is unusually safe, because the fleet is bimodal with a
wide empty gap — 11 files under 15 minutes (all within seconds), 56 files
over 24 hours, and NOTHING in between. Any cutoff from one minute to one
day yields the identical partition, so :data:`RECENT_BEAT_MAX_AGE_S` is
not a tuning knob balancing false positives against false negatives.

What this deliberately does NOT do
----------------------------------
A recent beat proves LIFE; a stale beat proves nothing. An agent killed
with SIGKILL leaves its last ``running`` record behind forever, so
"stale" must never be read as "dead" — 47 of the 90 ``defined`` rows carry
exactly such a fossil record, and promoting all of them would bury the
operator in UNKNOWNs and train them to ignore the view. Hence the
positive-only contract: this returns ``True`` only on evidence, and
``None`` for every "cannot tell".
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = ["RECENT_BEAT_MAX_AGE_S", "beat_is_recent"]

#: How recently the heartbeat FILE must have been written to count as
#: evidence of life. Generous next to the real cadence (seconds) and far
#: below the dead cluster (24h+); see the module docstring on why the gap
#: makes this insensitive to the exact value.
RECENT_BEAT_MAX_AGE_S = 900.0

#: The per-agent beat file, written by ``_session_state.write_heartbeat``.
BEAT_FILENAME = "heartbeat.json"


def beat_is_recent(
    name: str,
    *,
    now: float | None = None,
    max_age_s: float = RECENT_BEAT_MAX_AGE_S,
    state_dir: Path | None = None,
) -> bool | None:
    """``True`` if ``name``'s beat file was written within ``max_age_s``.

    Three-valued on purpose. ``None`` means "no evidence either way" — no
    beat file, or one whose mtime cannot be read — and must never be
    collapsed into ``False`` by a caller, because the caller's ``False``
    branch asserts a pole. ``False`` says only that the file is old, which
    is not a claim that the agent is gone.

    ``state_dir`` overrides the resolved location so a test can drive the
    real function against a real file instead of patching the resolver.
    """
    if state_dir is None:
        # runtime_base_dir() resolves PER CALL, deliberately: the sibling
        # constant ``_session_state.DEFAULT_STATE_ROOT`` snapshots it at
        # import, so a relocated runtime dir (or a test setting the env
        # var) would be invisible to anything keyed off the snapshot.
        # stx-allow: fallback (reason: an unresolvable root is NO evidence,
        # which is None — never False, which the caller reads as a pole)
        try:
            from ..._runtime_paths import runtime_base_dir

            state_dir = runtime_base_dir() / name
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            return None
    beat = Path(state_dir) / BEAT_FILENAME
    # stx-allow: fallback (reason: a racing writer/unreadable dir is an
    # UNKNOWN; see the three-valued contract above)
    try:
        age = (now if now is not None else time.time()) - beat.stat().st_mtime
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        return None
    # A beat stamped slightly in the future (clock skew between the writer
    # and this reader) is still a beat that just happened.
    return age <= max_age_s
