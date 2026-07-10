# -*- coding: utf-8 -*-
"""Supervisor-loop helpers for the claude-session conversation runner.

Extracted from :mod:`._session_conversation` (which was over the
project's per-file line cap) so that module stays focused on the
``run_conversation`` orchestration itself. These four functions are
pure relocations — none call back into ``run_conversation`` or hold
module-level state, they take everything they need as parameters — so
the split is not a behavioural change. ``_session_conversation``
re-exports all four under their original names so every existing
import site (``claude_session.py``'s ``_drain_failed_inbox``
re-export, the test suite's direct ``_resume_candidate`` import, the
``_wake_on_inbound`` behaviour exercised indirectly through
``run_conversation``) keeps resolving unchanged.

* :func:`_maybe_compact` — proactive auto-compaction for
  provider-backed agents on a small context window.
* :func:`_drain_failed_inbox` — resolve pending turn futures with a
  failure so producers don't hang.
* :func:`_wake_on_inbound` — watch the inbox mid-turn, interrupt the
  SDK client when a new envelope arrives.
* :func:`_resume_candidate` — pick the resume session_id for a
  supervised restart attempt (walks session_id_history latest-first).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from ._session_state import read_session_id, read_session_id_history

logger = logging.getLogger(__name__)

__all__ = [
    "_drain_failed_inbox",
    "_maybe_compact",
    "_resume_candidate",
    "_wake_on_inbound",
]


async def _maybe_compact(client: Any, *, name: str) -> None:
    """Proactively summarize the conversation when context nears the model
    window. Provider-backed agents on a small window (e.g. Qwen36 at 128k
    via the LiteLLM shim) overflow because the CLI's native auto-compaction
    can't know a PROXIED model's real context size, so it never compacts
    before the model rejects the prompt (cohort-A Qwen de-risk 2026-06-24:
    122881 input + 8192 output > 131072 = ContextWindowExceededError).

    Gated by ``SAC_AUTO_COMPACT_TOKENS`` (0/unset = off, so the Anthropic
    path keeps the CLI's own window-aware auto-compaction). When >0, read
    the live ``totalTokens`` from ``client.get_context_usage()`` — the
    ABSOLUTE count, NOT the CLI's ``percentage``/``maxTokens`` which assume
    the wrong window for a proxied model — and inject ``/compact`` once it
    crosses the threshold. Best-effort: any failure here must never kill a
    live solve.
    """
    try:
        threshold = int(os.environ.get("SAC_AUTO_COMPACT_TOKENS", "0") or "0")
    except ValueError:
        threshold = 0
    if threshold <= 0:
        return
    try:
        usage = await client.get_context_usage()
    except Exception as exc:  # stx-allow: fallback (reason: usage probe is best-effort; never fail a turn on it)
        logger.debug("auto-compact: get_context_usage failed (skipping): %s", exc)
        return
    total = usage.get("totalTokens") if isinstance(usage, dict) else None
    if not isinstance(total, (int, float)) or total < threshold:
        return
    max_tokens = usage.get("maxTokens") if isinstance(usage, dict) else None
    pct = usage.get("percentage") if isinstance(usage, dict) else None
    logger.warning(
        "auto-compact: totalTokens=%d >= %d for %s (maxTokens=%s pct=%s) — injecting /compact",
        int(total),
        threshold,
        name,
        max_tokens,
        pct,
    )
    try:
        await client.query("/compact")
        async for _msg in client.receive_response():
            pass
        logger.info("auto-compact: /compact completed for %s", name)
    except Exception as exc:  # stx-allow: fallback (reason: compaction is best-effort; a failed /compact must not kill the solve)
        logger.warning("auto-compact: /compact failed for %s: %s", name, exc)


def _drain_failed_inbox(inbox: "asyncio.Queue", exc: BaseException) -> None:
    """Resolve pending turn futures with the failure so producers don't hang."""
    from ._session_inbox import TurnEnvelope

    while not inbox.empty():
        try:
            env = inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if isinstance(env, TurnEnvelope) and not env.response.done():
            env.response.set_exception(exc)


async def _wake_on_inbound(inbox, client) -> None:
    """Watch ``inbox`` while a turn is in flight; interrupt the SDK
    when the next envelope arrives so the consumer loop can dequeue it.

    Pairs 1:1 with each ``_drive_turn`` invocation (#41, lead a2a
    ``f39bdcc5`` + ``b4e223e0``). The conversation loop spawns this as
    a sibling task, lets it await on the inbox's non-destructive
    "queue is non-empty" event, and cancels it in ``finally`` so it
    cannot leak across turn boundaries.

    Semantics:

    * ``await inbox.wait_for_item()`` returns when the queue's
      ``_not_empty`` event fires — i.e. the next envelope has been put
      AND has not yet been consumed by the conversation loop. Because
      this loop's ``inbox.get()`` already consumed the CURRENT
      envelope, the wake fires only on a NEW envelope queued mid-turn.
    * On wake, ``await client.interrupt()`` asks the SDK to stop the
      current iterator. The SDK guarantee (verified empirically in
      :mod:`_session_turn`) is that any in-flight assistant text /
      tool-result is already captured in ``chunks`` by the time the
      iterator returns — so the interrupt does NOT corrupt or tear
      the current turn. The new envelope is then processed as a
      clean FOLLOW-UP message in the next loop iteration.
    * Exceptions from ``interrupt`` are swallowed (logged WARNING)
      because the wake task must NEVER kill the conversation loop;
      worst case the original turn finishes naturally and the new
      envelope is processed one turn-cap later.

    The wake task EXITS after one interrupt — there's only ever one
    in-flight turn, and the consumer loop will spawn a fresh wake
    task for the next turn.
    """
    try:
        await inbox.wait_for_item()
    except asyncio.CancelledError:
        # Normal path when the turn completed before any new envelope
        # arrived — the consumer cancels us in ``finally``.
        raise
    try:
        await client.interrupt()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # stx-allow: fallback (reason: wake task is best-effort; never let SDK interrupt failure kill the conversation loop — the original turn will finish naturally instead)
        logger.warning(
            "wake-on-inbound: client.interrupt() failed (turn will "
            "finish naturally before the new envelope is processed): %s",
            exc,
        )


def _resume_candidate(
    state_dir: Path,
    *,
    attempt: int,
    fallback: str | None,
) -> str | None:
    """Pick the resume session_id for supervisor restart ``attempt``.

    Walks the append-only ``session_id_history`` from latest to oldest:
    ``attempt`` 0 → latest, 1 → next-older, etc. This lets a supervised
    restart retry a prior still-on-disk id when the latest one was
    forked or aged out and its resume is rejected.

    - ``attempt`` within the history range → that id (latest-first).
    - ``attempt == 0`` with no history yet → ``read_session_id`` if
      present else ``fallback`` (preserves the pre-history behaviour for
      the very first start, before any turn has recorded an id).
    - ``attempt`` beyond the history → ``None`` (history exhausted →
      fresh start, resume disabled).
    """
    history = read_session_id_history(state_dir)
    candidates = list(reversed(history))  # latest-first
    if attempt < len(candidates):
        return candidates[attempt]
    if attempt == 0:
        return read_session_id(state_dir) or fallback
    return None
