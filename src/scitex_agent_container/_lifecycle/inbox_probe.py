"""Ask ``sac listen`` whether an agent's inbox adapter is actually attached.

``sac agents health`` answers "is the process up?" (``health_check`` →
``runtime.is_running``). That is a PID-shaped question, and a deaf agent
passes it: its process is alive, its port is claimed, its registry row says
``active`` — and every ``a2a_send`` aimed at it lands on a bus with zero
subscribers and wakes nobody.

So health reported green for agents that could not receive a single message.
This probe adds the missing OBSERVATION next to the declaration, by asking
the one component that actually knows: the broker inside ``sac listen``
(via ``GET /agents/<name>/status``, which carries ``inbox_subscribers`` —
see ``_listen/_reachability.py``).

⚠️ ``inbox_subscribers == 0`` IS CONFOUNDED. READ THIS BEFORE YOU USE IT.
------------------------------------------------------------------------
A zero has **at least two** causes, and this probe **cannot tell them apart**:

    (a) the agent is ALIVE but its inbox adapter is DETACHED   (deaf), and
    (b) the agent IS NOT RUNNING AT ALL                        (dead).

A registry row **outlives the process**, so a stopped agent still appears in
the listing, still shows a pid and a port — and reports ``0`` exactly like a
deaf one. **A zero therefore tells you NOTHING about which of (a) or (b) holds.**

This is not hypothetical. On 2026-07-14 **three agents independently** read a
fleet-wide wall of zeros and each concluded "the fleet has gone deaf". All of
them were reading *this signal*. Every agent in their lists was simply
**stopped**. It cost an escalation to the operator and a P0 that did not exist.
Note the shape: three agreeing reports were **one instrument read three times**.

**To distinguish (a) from (b) you MUST corroborate with an instrument that does
not derive from ``sac listen``'s own bookkeeping.** Note that the ``instances``
row, ``runtime/<name>/heartbeat.json`` and this subscriber count *all* reflect
listen's BELIEF, not the agent's LIFE — they are far less independent than they
look. Genuinely independent: the **host tmux session** (the only signal that was
right that day), and **delivery** itself. NOT independent, and NOT a sensor at
all from inside a container: ``/proc/<pid>`` — the pid namespace differs, so it
reports ABSENT for demonstrably alive host processes.

Two rules this module will not break
------------------------------------
1. **It never invents a verdict.** Every failure to observe — no listen
   running, transport error, missing field, malformed body — returns
   :data:`UNKNOWN`, never :data:`UNREACHABLE`. "I could not check" is not
   evidence of deafness, and rendering it as such would slander healthy
   agents.
2. **It never feeds a restart.** The subscriber count is reported, and
   that is ALL it does. ``healthy`` (which gates ``sac agents health``'s
   exit code, and any automation keyed on it) is deliberately NOT derived
   from it: 0 subscribers means an inbox adapter is detached **or that the
   agent is not running** — never that a *living* agent is beyond saving —
   and auto-restarting on it would destroy a healthy session.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .._listen._reachability import REACHABLE, UNKNOWN, UNREACHABLE

__all__ = ["DEFAULT_LISTEN_URL", "probe_inbox_reachability"]

DEFAULT_LISTEN_URL = "http://127.0.0.1:7878"

# Short on purpose. This is an ADVISORY observation bolted onto a fast local
# command; it must never be the reason `sac agents health` feels slow or
# hangs. A listen that cannot answer in 2 s yields UNKNOWN, which is honest.
_TIMEOUT_S = 2.0


def _listen_base_url() -> str:
    return os.environ.get("SAC_LISTEN_BASE_URL", DEFAULT_LISTEN_URL).rstrip("/")


def probe_inbox_reachability(
    name: str,
    *,
    listen_url: str | None = None,
    bearer: str | None = None,
    timeout_s: float = _TIMEOUT_S,
) -> tuple[int | None, str]:
    """Return ``(inbox_subscribers, inbox_reachable)`` for ``name``.

    ``inbox_subscribers`` is the live SSE subscriber count on that agent's
    inbox stream, or ``None`` when it could not be observed.
    ``inbox_reachable`` is one of ``reachable`` / ``unreachable`` /
    ``unknown`` (the vocabulary in :mod:`_listen._reachability`).

    Never raises: every failure path degrades to ``(None, UNKNOWN)``.
    """
    base = (listen_url or _listen_base_url()).rstrip("/")
    token = bearer if bearer is not None else os.environ.get("SAC_LISTEN_BEARER")
    req = urllib.request.Request(f"{base}/agents/{name}/status", method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")

    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
            body = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        OSError,
    ):  # stx-allow: fallback (reason: an unobservable listen must yield UNKNOWN — never a false 'unreachable' verdict against a healthy agent)
        return None, UNKNOWN

    if not isinstance(body, dict):
        return None, UNKNOWN

    count = body.get("inbox_subscribers")
    if not isinstance(count, int) or isinstance(count, bool):
        # Field absent (an older listen that predates the observation) or
        # not an int. We did not observe a zero — we observed nothing.
        return None, UNKNOWN

    return count, (REACHABLE if count >= 1 else UNREACHABLE)
