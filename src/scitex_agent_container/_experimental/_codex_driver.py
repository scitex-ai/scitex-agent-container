#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""codex app-server driver for the Anthropic Messages adapter (experimental).

Extracted from ``codex_anthropic_adapter.py`` when SSE streaming pushed the
single file over the line limit. This module owns the JSON-RPC-over-stdio
conversation with ``codex app-server`` and nothing else:

* :func:`iter_codex_events` — the ONE driver, a generator that yields the
  app-server notifications (``item/agentMessage/delta`` / ``item/completed`` /
  ``turn/completed``) as they arrive. Both the non-streaming path
  (:func:`run_codex_turn`) and the SSE-streaming path consume this same
  generator, so the wire behaviour can never diverge between them.
* :func:`run_codex_turn` — non-streaming convenience: consume the generator
  and return the final reply text.
* per-event extraction helpers used by both the non-streaming path and the
  pure SSE mapper in ``_codex_sse.py``.

Faithful reproduction of the proven probe
``~/.scitex/agent-container/runtime/tmp/codex_appserver_test.py``.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Optional

DEFAULT_MODEL = "gpt-5.5"
# Upper bound for how long we wait for codex to finish a turn.
TURN_TIMEOUT_S = 120.0


class CodexError(RuntimeError):
    """Raised when the codex app-server turn fails or times out."""


def iter_codex_events(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_s: float = TURN_TIMEOUT_S,
):
    """Drive one ``codex app-server`` turn, yielding its JSON-RPC notifications.

    initialize -> initialized -> thread/start(model) -> turn/start(prompt),
    then yield each relevant app-server notification dict *as it arrives*:

    * ``item/agentMessage/delta``  — one per incremental reply chunk
    * ``item/completed`` / ``item/updated`` — the full agent-message item
    * ``turn/completed`` — the final event (generator returns after yielding it)

    Raises :class:`CodexError` on ``turn/failed`` / protocol error / timeout.
    Spawn-per-call is an MVP simplification (see the adapter module docstring).
    """
    proc = subprocess.Popen(
        ["codex", "app-server"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    lines: list[str] = []
    lock = threading.Lock()

    def reader(stream, tag: str) -> None:
        for ln in stream:
            with lock:
                lines.append(f"{tag} {ln.rstrip()}")

    threading.Thread(target=reader, args=(proc.stdout, "OUT"), daemon=True).start()
    threading.Thread(target=reader, args=(proc.stderr, "ERR"), daemon=True).start()

    def send(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    try:
        send(
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex-anthropic-adapter",
                        "title": "codex-anthropic-adapter",
                        "version": "0.1.0",
                    }
                },
            }
        )
        send({"method": "initialized", "params": {}})
        send({"method": "thread/start", "id": 1, "params": {"model": model}})

        thread_id: Optional[str] = None
        turn_sent = False
        seen = 0
        deadline = time.time() + timeout_s
        # Tight poll interval: keeps streamed deltas flowing to the client with
        # low latency (the reader threads buffer between polls, so nothing is
        # lost — this only bounds how quickly a buffered delta is surfaced).
        while time.time() < deadline:
            time.sleep(0.05)
            with lock:
                snapshot = lines[seen:]
                seen = len(lines)
            for raw in snapshot:
                if raw.startswith("ERR "):
                    continue
                if not raw.startswith("OUT "):
                    continue
                try:
                    msg = json.loads(raw[4:])
                except Exception:
                    continue

                # thread/start response -> fire the turn
                if (
                    msg.get("id") == 1
                    and isinstance(msg.get("result"), dict)
                    and not turn_sent
                ):
                    tid = (msg["result"].get("thread") or {}).get("id")
                    if tid:
                        thread_id = tid
                        send(
                            {
                                "method": "turn/start",
                                "id": 2,
                                "params": {
                                    "threadId": tid,
                                    "input": [{"type": "text", "text": prompt}],
                                },
                            }
                        )
                        turn_sent = True
                    continue

                method = msg.get("method")

                if method == "item/agentMessage/delta":
                    yield msg
                    continue

                if method in ("item/completed", "item/updated"):
                    yield msg
                    continue

                if method == "turn/completed":
                    yield msg
                    return

                if method == "turn/failed":
                    raise CodexError(
                        f"codex turn/failed: {json.dumps(msg.get('params') or {})[:500]}"
                    )

                if msg.get("error"):
                    raise CodexError(f"codex error: {json.dumps(msg['error'])[:500]}")

        with lock:
            err_lines = [l for l in lines if l.startswith("ERR ")][-5:]
        raise CodexError(
            "codex turn timed out after "
            f"{timeout_s:.0f}s (thread_id={thread_id}, turn_sent={turn_sent}); "
            f"stderr tail: {err_lines}"
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


# --------------------------------------------------------------------------- #
# per-event extraction helpers (shared by the non-streaming path + SSE mapper)
# --------------------------------------------------------------------------- #
def _event_delta_text(msg: dict[str, Any]) -> str:
    """Extract the incremental text from an ``item/agentMessage/delta`` event."""
    params = msg.get("params") or {}
    d = params.get("delta") or params.get("text") or ""
    return d if isinstance(d, str) else ""


def _event_completed_text(msg: dict[str, Any]) -> Optional[str]:
    """Extract the full agent-message text from an ``item/completed`` event."""
    item = (msg.get("params") or {}).get("item") or {}
    if item.get("type") in ("agentMessage", "assistantMessage"):
        txt = item.get("text")
        if isinstance(txt, str) and txt:
            return txt
    return None


def _event_output_tokens(msg: dict[str, Any]) -> Optional[int]:
    """Best-effort real output-token count from a ``turn/completed`` event.

    codex's usage shape is not contractually pinned here, so probe a few
    plausible locations and fall back to ``None`` (caller then estimates).
    """
    params = msg.get("params") or {}
    for container in (params, params.get("turn") or {}, params.get("usage") or {}):
        if not isinstance(container, dict):
            continue
        usage = (
            container.get("usage")
            if isinstance(container.get("usage"), dict)
            else container
        )
        for key in ("output_tokens", "outputTokens", "completion_tokens"):
            val = usage.get(key)
            if isinstance(val, int):
                return val
    return None


def _estimate_tokens(text: str) -> int:
    """Crude token estimate (~4 chars/token). MVP placeholder for real usage."""
    if not text:
        return 0
    return max(len(text) // 4, len(text.split()))


def run_codex_turn(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_s: float = TURN_TIMEOUT_S,
) -> str:
    """Drive one ``codex app-server`` turn and return the agent's reply text.

    Non-streaming: consumes :func:`iter_codex_events`, preferring the full
    ``item/completed`` text and falling back to accumulated deltas. On a
    timeout (``CodexError``) any partial reply is salvaged, else re-raised.
    """
    reply_text: Optional[str] = None
    delta_buf: list[str] = []
    try:
        for msg in iter_codex_events(prompt, model=model, timeout_s=timeout_s):
            method = msg.get("method")
            if method == "item/agentMessage/delta":
                delta_buf.append(_event_delta_text(msg))
            elif method in ("item/completed", "item/updated"):
                txt = _event_completed_text(msg)
                if txt is not None:
                    reply_text = txt
            elif method == "turn/completed":
                break
    except CodexError:
        # timed out / errored — salvage any partial reply, else fail loudly
        if reply_text is not None:
            return reply_text
        if delta_buf:
            return "".join(delta_buf)
        raise
    if reply_text is not None:
        return reply_text
    return "".join(delta_buf)
