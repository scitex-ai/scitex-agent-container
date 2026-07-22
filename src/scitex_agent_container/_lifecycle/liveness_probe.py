"""Functional-liveness probe state machine for Claude Code agents.

A pane-diff ("did the terminal bytes change?") is a false positive
for liveness because channel notifications (from other agents via
the MCP sidecar) scroll into the pane even when the local Claude
process is frozen. This module implements a stronger signal: the
agent is asked to echo back a random nonce, and this observer
watches for that echo in subsequent pane captures.

**Scope — observer only.** This module classifies state from pane
captures. It never sends keystrokes. The actor that issues
``Repeat <nonce>`` to the agent lives in a separate module
(tentatively ``auto_response.py``) so operators who do not want
automated actions can import this observer without pulling in the
actor code path.

States
------
- ``PENDING``  — probe issued, nonce not yet echoed, no timeout.
- ``ALIVE``    — the nonce appears in the pane in a position that
  is distinct from the user-sent prompt line (at least two
  distinct occurrences of the nonce in the captured text).
- ``BUSY``     — the pane tail shows an in-progress marker
  ("Working…", "Ruminating…", tool-call banner) at the moment of
  classification. A busy agent is alive in the process sense but
  is still working on a prior turn; the caller should defer
  re-probing rather than declare it silent.
- ``SILENT``   — deadline reached without a nonce echo and the
  pane tail does not show a busy marker. This is the "agent
  appears frozen / crashed" outcome.

Design rules
------------
- **Non-agentic.** Pure functions + a polling loop. No LLM calls.
- **Injection everywhere.** ``capture_fn``, ``time_fn``, ``sleep_fn``
  are parameters so tests stay deterministic.
- **Stdlib only.** ``secrets`` for the nonce, no requests, no psutil.
- **Zero coupling.** scitex-agent-container does not know about
  any external orchestrator — consumers wrap this observer.
"""

from __future__ import annotations

import logging
import secrets
import time
from enum import Enum
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# The TUI is responsive (not mid-turn) when these markers are absent
# from the tail. Keep the list tight — false positives turn ALIVE
# probes into BUSY, which delays re-probing unnecessarily.
DEFAULT_BUSY_MARKERS: tuple[str, ...] = (
    "Working\u2026",  # "Working…"
    "Ruminating\u2026",
    "Thinking\u2026",
    "esc to interrupt",  # the "esc to interrupt" line accompanies active generation
)

# Tail window for busy-marker classification. Matches the window used
# by ``agent_meta._classify_pane_state`` so the two classifiers stay
# consistent about what "tail" means.
_BUSY_TAIL_CHARS = 2000

# Minimum nonce occurrences in the pane to declare ALIVE. The user's
# own "Repeat <nonce>" prompt contributes one occurrence; a genuine
# echo from the agent adds at least one more.
_MIN_ECHO_OCCURRENCES = 2


class ProbeState(Enum):
    """Outcome of a liveness-probe evaluation."""

    PENDING = "pending"
    ALIVE = "alive"
    BUSY = "busy"
    SILENT = "silent"


def generate_nonce(n_bytes: int = 4) -> str:
    """Return an unambiguous hex nonce (default 8 chars = 4 bytes).

    Hex-only keeps the nonce visually unambiguous in a monospace
    TUI where 0/O and 1/l/I collisions plague base64 nonces.
    """
    return secrets.token_hex(n_bytes)


def pane_has_nonce_echo(
    pane_text: str,
    nonce: str,
    *,
    min_occurrences: int = _MIN_ECHO_OCCURRENCES,
) -> bool:
    """True iff the nonce appears at least ``min_occurrences`` times.

    Rationale: the user-sent prompt ``Repeat <nonce>`` contributes
    exactly one occurrence once rendered. Any further occurrence is
    the agent's echo — the exact phrasing ("nonce", "the code
    <nonce>", "yes, <nonce>") is immaterial because the pane→LLM→
    pane round-trip has completed either way.

    ``min_occurrences`` is tunable to make the rule stricter if the
    calling environment soft-wraps long lines and duplicates the
    nonce across wraps.
    """
    if not pane_text or not nonce:
        return False
    return pane_text.count(nonce) >= min_occurrences


def pane_is_busy(
    pane_text: str,
    *,
    markers: tuple[str, ...] = DEFAULT_BUSY_MARKERS,
    tail_chars: int = _BUSY_TAIL_CHARS,
) -> bool:
    """True if the tail of the pane shows an in-progress marker.

    Only checks the last ``tail_chars`` to avoid flagging a
    historical "Working…" that scrolled away long ago.
    """
    if not pane_text:
        return False
    tail = pane_text[-tail_chars:]
    return any(marker in tail for marker in markers)


def classify_probe(
    pane_text: str,
    nonce: str,
    *,
    is_timeout: bool = False,
    busy_markers: tuple[str, ...] = DEFAULT_BUSY_MARKERS,
    min_occurrences: int = _MIN_ECHO_OCCURRENCES,
) -> ProbeState:
    """Single-capture classifier — pure function of the current pane.

    The ``wait_for_nonce_echo`` polling loop calls this repeatedly.
    Exposing it independently means callers can also classify a
    one-off snapshot (e.g. from a registry cache) without running
    the loop.
    """
    if pane_has_nonce_echo(pane_text, nonce, min_occurrences=min_occurrences):
        return ProbeState.ALIVE
    if is_timeout:
        if pane_is_busy(pane_text, markers=busy_markers):
            return ProbeState.BUSY
        return ProbeState.SILENT
    return ProbeState.PENDING


def wait_for_nonce_echo(
    agent_name: str,
    pane_target: str,
    nonce: str,
    *,
    poll_interval: float = 2.0,
    timeout: float = 30.0,
    capture_fn: Optional[Callable[[str], str]] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
    busy_markers: tuple[str, ...] = DEFAULT_BUSY_MARKERS,
    min_occurrences: int = _MIN_ECHO_OCCURRENCES,
) -> tuple[ProbeState, float]:
    """Poll the pane until the nonce is echoed or the deadline hits.

    The actor (``send_text_and_submit(session, "Repeat <nonce>")``)
    must have been invoked BEFORE this function — this loop only
    observes. Keeping them split lets operators run the observer
    against a pane where the probe was sent by a different process
    (e.g. manually, or from a cron) without this module needing to
    know how to send keystrokes.

    Parameters
    ----------
    agent_name:
        For logging only.
    pane_target:
        Passed verbatim to ``capture_fn``.
    nonce:
        The token to watch for. Typically from ``generate_nonce()``.
    poll_interval:
        Seconds between pane captures.
    timeout:
        Hard wall-clock deadline. On expiry the loop does one final
        classify pass (to mark BUSY if the pane is actively working)
        and returns.
    capture_fn:
        ``callable(pane_target) -> pane_text``. REQUIRED if the
        caller wants real captures; defaults to a noop that returns
        ``""`` so unit tests without a pane fail loudly.
    time_fn / sleep_fn:
        Injected clocks for tests.
    busy_markers, min_occurrences:
        Forwarded to ``classify_probe``.

    Returns
    -------
    (final_state, elapsed_seconds)
    """

    def _noop_capture(_target: str) -> str:
        return ""

    capture = capture_fn if capture_fn is not None else _noop_capture

    start = time_fn()
    deadline = start + float(timeout)
    poll_count = 0
    last_tail = ""

    while True:
        now = time_fn()
        expired = now >= deadline
        # stx-allow: fallback (reason: tmux/screen capture can fail transiently; treating pane as empty lets the probe loop continue safely)
        try:
            pane = capture(pane_target) or ""
        except Exception as exc:  # pragma: no cover - defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            logger.debug("capture_fn raised %s: %s", type(exc).__name__, exc)
            pane = ""
        poll_count += 1
        last_tail = pane
        state = classify_probe(
            pane,
            nonce,
            is_timeout=expired,
            busy_markers=busy_markers,
            min_occurrences=min_occurrences,
        )
        if state is ProbeState.ALIVE:
            elapsed = time_fn() - start
            logger.info(
                "probe ALIVE for %s (pane=%s, polls=%d, elapsed=%.1fs)",
                agent_name,
                pane_target,
                poll_count,
                elapsed,
            )
            return state, elapsed
        if expired:
            elapsed = time_fn() - start
            logger.warning(
                "probe %s for %s (pane=%s, polls=%d, elapsed=%.1fs, last_tail_len=%d)",
                state.value.upper(),
                agent_name,
                pane_target,
                poll_count,
                elapsed,
                len(last_tail),
            )
            return state, elapsed
        sleep_fn(poll_interval)
