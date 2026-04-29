"""Layer 4 — loop: throttled daemon for sac auto-accept.

Walks the registered agent list (or a single named agent) and for each:
  capture → classify → respond

Throttling rules:
  - Same agent: minimum 5 s between consecutive send-accept actions
  - Daemon: 30 s wait when state is unchanged (noise suppression)
  - Default tick: 60 s
"""

from __future__ import annotations

import logging
import os
import signal
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_DEFAULT_TICK_S = 60
_MIN_SEND_INTERVAL_S = 5
_UNCHANGED_STATE_WAIT_S = 30

_PID_DIR = Path(
    os.environ.get(
        "SCITEX_AGENT_CONTAINER_REGISTRY_DIR",
        Path.home() / ".scitex" / "agent-container" / "registry",
    )
)


# ---------------------------------------------------------------------------
# PID file helpers (daemon process supervision)
# ---------------------------------------------------------------------------


def _pid_path(name: str) -> Path:
    _PID_DIR.mkdir(parents=True, exist_ok=True)
    return _PID_DIR / f"auto-accept-{name}.pid"


def write_pid(name: str) -> None:
    _pid_path(name).write_text(str(os.getpid()))


def read_pid(name: str) -> int | None:
    p = _pid_path(name)
    if not p.exists():
        return None
    try:
        return int(p.read_text().strip())
    except Exception:
        return None


def clear_pid(name: str) -> None:
    p = _pid_path(name)
    if p.exists():
        p.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Single-agent daemon loop
# ---------------------------------------------------------------------------


def run_daemon(
    name: str,
    *,
    tick_s: float = _DEFAULT_TICK_S,
    min_send_interval_s: float = _MIN_SEND_INTERVAL_S,
    unchanged_wait_s: float = _UNCHANGED_STATE_WAIT_S,
    capture_fn: Callable[[str], str] | None = None,
    classify_fn: Callable[[str], tuple[str, str]] | None = None,
    respond_fn: Callable[[str, str, str], bool] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> None:
    """Daemon loop for agent *name*. Blocks until SIGTERM / SIGINT.

    Parameters (all injectable for testing):
        capture_fn:   pane_capture(name) -> str
        classify_fn:  _classify_pane_state(text) -> (state, snippet)
        respond_fn:   respond(name, state, pane_text) -> bool
        sleep_fn:     time.sleep override
    """
    from .agent_meta import _classify_pane_state as _classify
    from .auto_accept import respond as _respond
    from .runtimes.pane_capture import pane_capture as _capture

    cap = capture_fn or (lambda n: _capture(n))
    clf = classify_fn or _classify
    rsp = respond_fn or (lambda n, s, t: _respond(n, s, t))
    slp = sleep_fn or time.sleep

    last_send_at: float = 0.0
    last_state: str = ""

    write_pid(name)
    logger.info("[auto-accept daemon] started for agent=%s tick=%ss", name, tick_s)

    _stop = False

    def _handle_signal(signum, frame):
        nonlocal _stop
        logger.info("[auto-accept daemon] received signal %s — stopping", signum)
        _stop = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not _stop:
            try:
                pane_text = cap(name)
                state, _snippet = clf(pane_text)

                state_changed = state != last_state
                now = time.monotonic()

                if not state_changed:
                    logger.debug("[%s] state=%s unchanged — sleeping %ss", name, state, unchanged_wait_s)
                    slp(unchanged_wait_s)
                    continue

                last_state = state

                if state in ("compose_pending_unsent", "y_n_prompt"):
                    elapsed_since_send = now - last_send_at
                    if elapsed_since_send < min_send_interval_s:
                        remaining = min_send_interval_s - elapsed_since_send
                        logger.debug(
                            "[%s] throttle: last send was %.1fs ago — waiting %.1fs",
                            name, elapsed_since_send, remaining,
                        )
                        slp(remaining)
                        continue

                sent = rsp(name, state, pane_text)
                if sent:
                    last_send_at = time.monotonic()

            except Exception as exc:
                logger.error("[auto-accept daemon] error on agent %s: %s", name, exc)

            slp(tick_s)
    finally:
        clear_pid(name)
        logger.info("[auto-accept daemon] stopped for agent=%s", name)


# ---------------------------------------------------------------------------
# One-shot: capture → classify → respond
# ---------------------------------------------------------------------------


def send_accept_once(
    name: str,
    *,
    capture_fn: Callable[[str], str] | None = None,
    classify_fn: Callable[[str], tuple[str, str]] | None = None,
    respond_fn: Callable[[str, str, str], bool] | None = None,
) -> tuple[str, bool]:
    """One-shot: capture → classify → respond. Returns (state, sent)."""
    from .agent_meta import _classify_pane_state as _classify
    from .auto_accept import respond as _respond
    from .runtimes.pane_capture import pane_capture as _capture

    cap = capture_fn or (lambda n: _capture(n))
    clf = classify_fn or _classify
    rsp = respond_fn or (lambda n, s, t: _respond(n, s, t))

    pane_text = cap(name)
    state, _snippet = clf(pane_text)
    sent = rsp(name, state, pane_text)
    return state, sent
