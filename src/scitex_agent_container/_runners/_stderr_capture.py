# -*- coding: utf-8 -*-
"""Capture the claude subprocess's stderr so SDK failures stay legible.

The ``claude-agent-sdk`` only PIPES the subprocess stderr when the caller
registers a ``stderr`` callback on ``ClaudeAgentOptions``; otherwise the
stderr fd is inherited and discarded, and a process failure surfaces as
``ProcessError(stderr="Check stderr output for details")`` — a useless
placeholder that drops the actual reason (e.g. "No conversation found for
session <id>" on a stale ``--resume``, or a Python traceback from a hook).

This module gives the conversation runner the two pieces it needs:

* :class:`StderrCapture` — a bounded ring buffer plus a per-line callback
  to register on ``ClaudeAgentOptions(stderr=...)``.
* :func:`enrich_detail_with_stderr` — fold the captured text into the
  exception detail, replacing the SDK placeholder with the real reason.

A best-effort :func:`write_stderr_log` persists the full captured stream
to a dedicated log so the actionable tail is never lost even when the
``detail`` written to ``session.jsonl`` is bounded.

Pure stdlib — no SDK import — so it is unit-testable against the stderr
of a real subprocess without the optional ``claude-agent-sdk`` present.
"""

from __future__ import annotations

import collections
from pathlib import Path

__all__ = [
    "StderrCapture",
    "enrich_detail_with_stderr",
    "is_sdk_stderr_placeholder",
    "write_stderr_log",
]

# The SDK invokes the callback once per stderr line. A dead-session resume
# or auth failure emits a handful of lines; cap the ring so a runaway
# subprocess can't bloat memory while still keeping the tail — where the
# actual error and any traceback live.
_MAX_LINES = 400

# Placeholder the SDK substitutes when it did NOT pipe stderr. Any detail
# containing this carries no real stderr, so we prefer our own buffer.
_SDK_PLACEHOLDER = "Check stderr output for details"

# Marker the SDK uses to introduce the (placeholder) stderr tail on a
# ProcessError message: "<message> (exit code: N)\nError output: <stderr>".
_ERROR_OUTPUT_MARKER = "Error output:"

_STDERR_LOG_NAME = "runner-stderr.log"


class StderrCapture:
    """Bounded collector for the claude subprocess's stderr lines.

    Register :attr:`callback` on ``ClaudeAgentOptions(stderr=...)``; the
    SDK then pipes the subprocess stderr and feeds each line here. After a
    failure, read :meth:`text` to recover the real stderr.

    Parameters
    ----------
    max_lines : int
        Keep at most this many most-recent lines (the tail). Older lines
        are dropped once the ring is full.
    """

    def __init__(self, max_lines: int = _MAX_LINES) -> None:
        self._lines: "collections.deque[str]" = collections.deque(maxlen=max_lines)

    def callback(self, line: str) -> None:
        """SDK per-line stderr hook. Must never raise into the SDK reader.

        The SDK's stderr reader task invokes this synchronously per line;
        an exception here would be swallowed by the SDK but could still
        interrupt the line loop, so we guard defensively.
        """
        try:
            stripped = line.rstrip("\n")
            if stripped:
                self._lines.append(stripped)
        except Exception:  # stx-allow: fallback (reason: SDK stderr reader must not crash on a bad line)
            pass

    def text(self) -> str:
        """Return the joined captured stderr (most recent ``max_lines``)."""
        return "\n".join(self._lines)

    def __bool__(self) -> bool:
        return bool(self._lines)


def is_sdk_stderr_placeholder(detail: str | None) -> bool:
    """Return ``True`` when ``detail`` carries the SDK's stderr placeholder.

    The placeholder means the SDK never piped real stderr, so the caller
    should substitute its own captured buffer.
    """
    return _SDK_PLACEHOLDER in (detail or "")


def enrich_detail_with_stderr(detail: str | None, captured: str | None) -> str:
    """Fold captured stderr into ``detail`` so the real reason is legible.

    Parameters
    ----------
    detail : str or None
        The stringified exception (typically ``str(ProcessError)``).
    captured : str or None
        Text accumulated by :class:`StderrCapture`.

    Returns
    -------
    str
        - If ``captured`` is empty → ``detail`` unchanged.
        - If ``detail`` carries the SDK placeholder → the bogus
          ``Error output: Check stderr output for details`` tail is
          replaced with the real captured stderr.
        - Else if ``captured`` is already a substring of ``detail`` (the
          SDK surfaced it) → ``detail`` unchanged.
        - Otherwise → ``detail`` with the captured stderr appended on a
          labelled line, so nothing the SDK omitted is lost.
    """
    captured = (captured or "").strip()
    detail = detail or ""
    if not captured:
        return detail
    if is_sdk_stderr_placeholder(detail):
        head, _, _tail = detail.partition(_ERROR_OUTPUT_MARKER)
        head = head.rstrip()
        return f"{head}\n{_ERROR_OUTPUT_MARKER} {captured}" if head else captured
    if captured in detail:
        return detail
    return f"{detail}\nCaptured stderr: {captured}"


def write_stderr_log(state_dir: Path, captured: str | None) -> Path | None:
    """Append the full captured stderr to ``runner-stderr.log``.

    Returns
    -------
    pathlib.Path or None
        The log path on success; ``None`` when nothing was captured or
        the write fails. A logging failure must never mask the original
        error, so write errors are swallowed (and returned as ``None``).
    """
    captured = (captured or "").strip()
    if not captured:
        return None
    state_dir = Path(state_dir)
    path = state_dir / _STDERR_LOG_NAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(captured)
            if not captured.endswith("\n"):
                fh.write("\n")
        return path
    except Exception:  # stx-allow: fallback (reason: best-effort log; a write failure must not mask the original error)
        return None


# EOF
