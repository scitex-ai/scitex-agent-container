#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex app-server -> Anthropic Messages API adapter (MVP / proof-of-concept).

Architecture
------------

    Claude Code (or any Anthropic Messages client)
        │   POST /v1/messages   (Anthropic Messages shape)
        │   ANTHROPIC_BASE_URL=http://127.0.0.1:8787
        ▼
    THIS adapter  (stdlib http.server, no new deps)
        │   JSON-RPC 2.0 over stdio
        ▼
    `codex app-server`   (spawned as a subprocess)
        │   OAuth read automatically from ~/.codex/auth.json
        ▼
    OpenAI Codex engine (gpt-5.5)

The point this proves: the "engine swap" path works end-to-end. A client that
only knows how to speak the Anthropic Messages API (the Claude Code box) can be
pointed at this adapter via ``ANTHROPIC_BASE_URL`` and have its turns answered
by the Codex engine, reusing the operator's existing ``~/.codex`` OAuth login.
No API key, no re-auth: ``codex app-server`` reads ``~/.codex/auth.json`` itself.

The codex driver here is a faithful reproduction of the proven probe at
``~/.scitex/agent-container/runtime/tmp/codex_appserver_test.py`` (which showed
gpt-5.5 returning "pong" in ~2s over the app-server protocol).

MVP simplifications (documented, deliberate)
--------------------------------------------
This is an MVP / POC, NOT the production adapter. It is intentionally minimal:

* NON-STREAMING, TEXT-ONLY. ``stream:true`` is ignored (we always return a
  single non-streaming JSON body). Only text content blocks are handled.
* Conversation flattening. The Anthropic ``system`` + ``messages[]`` array is
  flattened into ONE role-tagged plain-text transcript that is sent as the
  single ``turn/start`` input. We do NOT map the structured message history
  onto codex threads/turns 1:1 — every request is a fresh codex thread.
* Thread-per-request. A new ``codex app-server`` process + thread is spawned
  for every HTTP request, then torn down. No thread reuse / no multi-turn state
  is kept between requests. (Production should keep a warm app-server and reuse
  a thread keyed by conversation.)
* ``usage`` token counts are ESTIMATED (~4 chars/token, whitespace-split
  fallback), not real. codex app-server does surface token usage in
  ``turn/completed`` / usage notifications; wiring that through is a follow-up.
* NO tool_use / tool_result mapping. Anthropic ``tool_use`` blocks and codex
  tool/exec items are NOT bridged. Tool-call payloads in the request are
  rendered as text only.
* NO auth / rate-limit / error mapping on the HTTP endpoint. The endpoint is
  unauthenticated and binds loopback by default. codex errors become a generic
  500-ish Anthropic ``error`` body.

Production follow-ups (do NOT do these in the MVP)
--------------------------------------------------
1. SSE streaming: honour ``stream:true`` and emit the Anthropic event sequence
   (``message_start`` -> ``content_block_start`` -> ``content_block_delta`` x N
   -> ``content_block_stop`` -> ``message_delta`` -> ``message_stop``) driven by
   the codex ``item/agentMessage/delta`` notifications.
2. tool_use / tool_result bridging: map Anthropic tool_use blocks <-> codex
   tool/exec items so agentic tool loops work through the adapter.
3. Warm app-server + thread reuse: keep one long-lived ``codex app-server`` and
   reuse a thread per conversation instead of spawn-per-request (latency + auth
   amortisation). Track thread ids keyed by the client conversation.
4. Real usage accounting from codex ``turn/completed`` usage fields, mapped to
   Anthropic ``usage.input_tokens`` / ``output_tokens``.
5. Error + rate-limit mapping: translate codex ``turn/failed`` / auth-expired /
   quota errors to the correct Anthropic HTTP status + ``error.type``.
6. Endpoint auth: require the ``x-api-key`` / ``authorization`` header the
   Anthropic client already sends, and bind non-loopback only behind that.
7. Coordinate with the ecosystem's LLM plumbing (scitex-genai / litellm) rather
   than a bespoke server: this adapter is the "prove the wire" spike, and the
   production version should slot into that routing layer, including multi-account
   ``~/.codex`` rotation (mirroring the sac account-rotation story).

Usage
-----
    # start the adapter (needs `codex` on PATH + ~/.codex/auth.json)
    python -m scitex_agent_container._experimental.codex_anthropic_adapter serve

    # point a client at it
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8787
    curl -s http://127.0.0.1:8787/v1/messages -H 'content-type: application/json' \
      -d '{"model":"gpt-5.5","max_tokens":64,
           "messages":[{"role":"user","content":"Reply with one word: pong"}]}'

    # built-in smoke test (spins up the server in-process, does one round-trip)
    python -m scitex_agent_container._experimental.codex_anthropic_adapter smoke

Experimental: this module lives under ``_experimental/`` and is NOT imported by
the package, wired into any spec, or covered by the CLI. It is a spike.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

DEFAULT_MODEL = "gpt-5.5"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# Upper bound for how long we wait for codex to finish a turn.
TURN_TIMEOUT_S = 120.0


# --------------------------------------------------------------------------- #
# codex app-server driver (reproduces the proven codex_appserver_test.py)
# --------------------------------------------------------------------------- #
class CodexError(RuntimeError):
    """Raised when the codex app-server turn fails or times out."""


def run_codex_turn(
    prompt: str,
    model: str = DEFAULT_MODEL,
    timeout_s: float = TURN_TIMEOUT_S,
) -> str:
    """Drive one ``codex app-server`` turn and return the agent's reply text.

    initialize -> initialized -> thread/start(model) -> turn/start(prompt),
    then collect the ``agentMessage`` reply from the ``item/completed``
    notification (falling back to accumulated ``item/agentMessage/delta``).

    Spawn-per-call is an MVP simplification (see module docstring).
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

    reply_text: Optional[str] = None
    delta_buf: list[str] = []
    error_detail: Optional[str] = None
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
        while time.time() < deadline:
            time.sleep(0.2)
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
                params = msg.get("params") or {}

                # streamed deltas — accumulate as a fallback reply source
                if method == "item/agentMessage/delta":
                    d = params.get("delta") or params.get("text") or ""
                    if isinstance(d, str):
                        delta_buf.append(d)
                    continue

                # completed item carries the full agent reply text
                if method in ("item/completed", "item/updated"):
                    item = params.get("item") or {}
                    if item.get("type") in ("agentMessage", "assistantMessage"):
                        txt = item.get("text")
                        if isinstance(txt, str) and txt:
                            reply_text = txt
                    continue

                if method == "turn/completed":
                    if reply_text is None and delta_buf:
                        reply_text = "".join(delta_buf)
                    if reply_text is not None:
                        return reply_text
                    # no text but turn is done — return whatever we have
                    return "".join(delta_buf)

                if method == "turn/failed":
                    error_detail = json.dumps(params)[:500]
                    raise CodexError(f"codex turn/failed: {error_detail}")

                if msg.get("error"):
                    error_detail = json.dumps(msg["error"])[:500]
                    raise CodexError(f"codex error: {error_detail}")

        # timed out — salvage any partial reply, else fail loudly
        if reply_text is not None:
            return reply_text
        if delta_buf:
            return "".join(delta_buf)
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
# Anthropic Messages <-> flattened-prompt translation
# --------------------------------------------------------------------------- #
def _content_to_text(content: Any) -> str:
    """Flatten an Anthropic message ``content`` (str OR block list) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "tool_use":  # MVP: render, don't bridge
                parts.append(
                    f"[tool_use name={block.get('name')} "
                    f"input={json.dumps(block.get('input', {}))}]"
                )
            elif btype == "tool_result":  # MVP: render, don't bridge
                parts.append(f"[tool_result {_content_to_text(block.get('content', ''))}]")
            else:
                parts.append(json.dumps(block))
        return "\n".join(p for p in parts if p)
    return str(content)


def flatten_request(body: dict[str, Any]) -> str:
    """Flatten Anthropic ``system`` + ``messages[]`` into one text prompt.

    MVP: a simple role-tagged transcript. System prompt first, then each turn
    prefixed with its role, then a trailing ``Assistant:`` cue. This preserves
    multi-turn context as plain text without mapping onto codex threads.
    """
    segments: list[str] = []

    system = body.get("system")
    if isinstance(system, list):  # system can be a content-block list too
        system = _content_to_text(system)
    if system:
        segments.append(f"System: {system}")

    for msg in body.get("messages", []) or []:
        role = msg.get("role", "user")
        text = _content_to_text(msg.get("content", ""))
        label = "User" if role == "user" else "Assistant"
        segments.append(f"{label}: {text}")

    segments.append("Assistant:")
    return "\n\n".join(segments)


def _estimate_tokens(text: str) -> int:
    """Crude token estimate (~4 chars/token). MVP placeholder for real usage."""
    if not text:
        return 0
    return max(len(text) // 4, len(text.split()))


def build_anthropic_response(
    reply_text: str, model: str, prompt: str
) -> dict[str, Any]:
    """Wrap the codex reply in a non-streaming Anthropic Messages response."""
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": reply_text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            # MVP: estimated, not codex-reported. See follow-up #4.
            "input_tokens": _estimate_tokens(prompt),
            "output_tokens": _estimate_tokens(reply_text),
        },
    }


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "codex-anthropic-adapter/0.1"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        sys.stderr.write("[adapter] " + (fmt % args) + "\n")

    def do_POST(self) -> None:  # noqa: N802 (stdlib signature)
        if self.path.rstrip("/") != "/v1/messages":
            self._json(404, {"type": "error", "error": {"type": "not_found_error", "message": self.path}})
            return
        try:
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # malformed body
            self._json(400, {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}})
            return

        model = body.get("model") or DEFAULT_MODEL
        # MVP: codex app-server drives its own model; we default gpt-5.5 unless
        # the client model name looks like a gpt-* codex model.
        codex_model = model if str(model).startswith("gpt-") else DEFAULT_MODEL
        prompt = flatten_request(body)

        try:
            reply = run_codex_turn(prompt, model=codex_model)
        except CodexError as exc:
            self._json(502, {"type": "error", "error": {"type": "api_error", "message": str(exc)}})
            return
        except FileNotFoundError:
            self._json(
                500,
                {"type": "error", "error": {"type": "api_error", "message": "`codex` binary not found on PATH"}},
            )
            return
        except Exception as exc:  # noqa: BLE001 — MVP generic mapping
            self._json(500, {"type": "error", "error": {"type": "api_error", "message": repr(exc)}})
            return

        self._json(200, build_anthropic_response(reply, model=model, prompt=prompt))


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer((host, port), _Handler)
    return httpd


# --------------------------------------------------------------------------- #
# Smoke test: start the server in-process, POST an Anthropic request, print reply
# --------------------------------------------------------------------------- #
def smoke(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    import urllib.request

    httpd = serve(host, port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req_body = json.dumps(
            {
                "model": DEFAULT_MODEL,
                "max_tokens": 64,
                "messages": [
                    {"role": "user", "content": "Reply with exactly one word: pong"}
                ],
            }
        ).encode()
        req = urllib.request.Request(
            f"http://{host}:{port}/v1/messages",
            data=req_body,
            headers={"content-type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=TURN_TIMEOUT_S + 10) as resp:
            payload = json.loads(resp.read())
        print("=== Anthropic-shaped response from codex engine ===")
        print(json.dumps(payload, indent=2))
        text = (payload.get("content") or [{}])[0].get("text", "")
        print("\n=== assistant reply ===")
        print(text)
        return 0
    finally:
        httpd.shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "serve"
    host = DEFAULT_HOST
    port = DEFAULT_PORT
    for a in argv[1:]:
        if a.startswith("--host="):
            host = a.split("=", 1)[1]
        elif a.startswith("--port="):
            port = int(a.split("=", 1)[1])

    if cmd == "smoke":
        return smoke(host, port)
    if cmd == "serve":
        httpd = serve(host, port)
        print(f"[adapter] serving Anthropic Messages -> codex on http://{host}:{port}", file=sys.stderr)
        print(f"[adapter] point clients at ANTHROPIC_BASE_URL=http://{host}:{port}", file=sys.stderr)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
        return 0
    print(f"unknown command: {cmd!r} (use 'serve' or 'smoke')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
