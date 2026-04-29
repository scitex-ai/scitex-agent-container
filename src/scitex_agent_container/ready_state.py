"""Claude Code ready-state detector (todo#291).

Polls a tmux pane until the Claude Code CLI appears booted and quiescent,
*then* the caller is safe to flush its ``startup_commands``. Without this
gate, ``delay: N`` races the boot and the role prompt is silently dropped
(observed on WSL / Spartan).

Public API:
    wait_for_ready(...)
    ReadyTimeout

Design:
- Poll ``tmux capture-pane`` (via an injectable ``capture_fn``) every
  ``poll_interval`` seconds.
- Ready iff ALL ``patterns`` match against the *tail* of the latest
  capture AND the last ``idle_ticks`` captures are byte-identical.
- Only the last ~40 lines count, so a banner that scrolled off does not
  satisfy the gate.
- Stdlib only.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

_TAIL_LINES = 40


class ReadyTimeout(RuntimeError):
    """Raised when the caller asks for a strict failure on timeout."""


def _default_capture(pane_target: str) -> str:
    """Fallback tmux capture when caller does not inject one."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", pane_target, "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout or ""
    except (subprocess.SubprocessError, OSError) as exc:  # stx-allow: fallback (reason: subprocess execution failure)
        logger.debug("tmux capture-pane failed for %s: %s", pane_target, exc)
        return ""


def _tail(text: str, n: int = _TAIL_LINES) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) <= n:
        return text
    return "\n".join(lines[-n:])


def _all_patterns_match(tail_text: str, compiled: Iterable[re.Pattern[str]]) -> bool:
    for pat in compiled:
        if not pat.search(tail_text):
            return False
    return True


def wait_for_ready(
    agent_name: str,
    pane_target: str,
    patterns: list[str],
    idle_ticks: int = 3,
    poll_interval: float = 0.5,
    timeout: float = 60.0,
    capture_callback: Optional[Callable[[str], None]] = None,
    capture_fn: Optional[Callable[[str], str]] = None,
    time_fn: Callable[[], float] = time.monotonic,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> bool:
    """Block until the pane looks ready, or return False on timeout.

    Parameters
    ----------
    agent_name:
        For logging only.
    pane_target:
        tmux target passed to ``capture_fn`` / ``tmux capture-pane -t``.
    patterns:
        List of regex strings. ALL must match against the tail of the
        pane capture (``re.MULTILINE`` enabled so anchors like ``^> $``
        work on the multi-line buffer).
    idle_ticks:
        Require this many consecutive byte-identical captures before
        declaring ready. Must be >= 1.
    poll_interval:
        Seconds between polls.
    timeout:
        Hard wall-clock cap. Returns False on expiry and invokes
        ``capture_callback`` with the final tail.
    capture_callback:
        Optional ``callable(tail_text)`` invoked once on timeout.
    capture_fn:
        Override for the tmux capture call (used in tests). Default
        shells out to ``tmux capture-pane``.
    time_fn / sleep_fn:
        Injectable clocks for tests.
    """
    # Backward-compat / fast-path: no patterns → nothing to wait for.
    if not patterns:
        return True

    idle_ticks = max(1, int(idle_ticks))
    compiled = [re.compile(p, re.MULTILINE) for p in patterns]
    capture = capture_fn or _default_capture

    start = time_fn()
    deadline = start + float(timeout)
    history: list[str] = []
    last_tail = ""
    poll_count = 0

    while True:
        now = time_fn()
        if now >= deadline:
            logger.warning(
                "ready_state timeout after %.1fs for %s (pane=%s, polls=%d)",
                now - start,
                agent_name,
                pane_target,
                poll_count,
            )
            if capture_callback is not None:
                try:
                    capture_callback(last_tail)
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    logger.exception("capture_callback raised; ignoring")
            return False

        try:
            content = capture(pane_target)
        except subprocess.CalledProcessError as exc:  # stx-allow: fallback (reason: subprocess execution failure)
            logger.debug("capture_fn raised CalledProcessError: %s", exc)
            content = ""
        except Exception as exc:  # pragma: no cover - defensive  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            logger.debug("capture_fn raised %s: %s", type(exc).__name__, exc)
            content = ""

        tail = _tail(content)
        last_tail = tail
        poll_count += 1

        history.append(tail)
        # Keep only the window we need.
        if len(history) > idle_ticks:
            history = history[-idle_ticks:]

        pane_quiet = (
            len(history) == idle_ticks
            and all(h == history[-1] for h in history)
        )

        if pane_quiet and _all_patterns_match(tail, compiled):
            logger.info(
                "ready_state OK for %s (pane=%s, polls=%d, elapsed=%.1fs)",
                agent_name,
                pane_target,
                poll_count,
                time_fn() - start,
            )
            return True

        sleep_fn(poll_interval)
