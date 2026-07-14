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
   from it: 0 subscribers means an inbox adapter is detached, NOT that the
   agent is dead, and auto-restarting on it would destroy a healthy session.
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
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):  # stx-allow: fallback (reason: an unobservable listen must yield UNKNOWN — never a false 'unreachable' verdict against a healthy agent)
        return None, UNKNOWN

    if not isinstance(body, dict):
        return None, UNKNOWN

    count = body.get("inbox_subscribers")
    if not isinstance(count, int) or isinstance(count, bool):
        # Field absent (an older listen that predates the observation) or
        # not an int. We did not observe a zero — we observed nothing.
        return None, UNKNOWN

    return count, (REACHABLE if count >= 1 else UNREACHABLE)
