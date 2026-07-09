#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Codex app-server -> Anthropic Messages API adapter (experimental spike).

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

Module layout
-------------
* ``_codex_driver.py`` — the ``codex app-server`` JSON-RPC driver
  (:func:`iter_codex_events` + :func:`run_codex_turn`). Faithful reproduction
  of the proven probe ``~/.scitex/agent-container/runtime/tmp/codex_appserver_test.py``.
* ``_codex_sse.py`` — pure Anthropic-SSE mapping
  (:func:`codex_stream_to_anthropic_sse` + :func:`format_sse_frame`).
* this file — Anthropic<->prompt translation + the HTTP server that ties the
  two together (non-streaming JSON and ``stream:true`` SSE).

Supported
---------
* NON-STREAMING ``POST /v1/messages`` -> a single Anthropic Messages JSON body.
* STREAMING ``POST /v1/messages`` with ``"stream": true`` -> ``text/event-stream``
  emitting the Anthropic event sequence (``message_start`` ->
  ``content_block_start`` -> ``content_block_delta`` x N -> ``content_block_stop``
  -> ``message_delta`` -> ``message_stop``), driven off the codex
  ``item/agentMessage/delta`` notifications one delta -> one SSE frame.

MVP simplifications (documented, deliberate)
--------------------------------------------
This is an experimental spike, NOT the production adapter:

* TEXT-ONLY. Only text content blocks are handled (streaming + non-streaming).
* Conversation flattening. The Anthropic ``system`` + ``messages[]`` array is
  flattened into ONE role-tagged plain-text transcript sent as the single
  ``turn/start`` input. No structured 1:1 thread/turn mapping.
* Thread-per-request. A new ``codex app-server`` process + thread is spawned
  for every HTTP request (streaming and non-streaming), then torn down. No
  thread reuse / no multi-turn state kept between requests.
* ``usage`` token counts are ESTIMATED (~4 chars/token) UNLESS codex reports a
  real count on ``turn/completed`` (then that real ``output_tokens`` is used in
  the streaming ``message_delta``).
* NO tool_use / tool_result mapping. Anthropic ``tool_use`` blocks and codex
  tool/exec items are NOT bridged; tool payloads are rendered as text only.
* NO auth / rate-limit / rich error mapping on the HTTP endpoint. The endpoint
  is unauthenticated and binds loopback by default. A codex failure becomes a
  generic Anthropic ``error`` (502 JSON, or an ``error`` SSE event mid-stream).

Remaining production follow-ups (NOT done here)
-----------------------------------------------
1. tool_use / tool_result bridging: map Anthropic tool_use blocks <-> codex
   tool/exec items so agentic tool loops work through the adapter.
2. Warm app-server + thread reuse: keep one long-lived ``codex app-server`` and
   reuse a thread per conversation instead of spawn-per-request (latency + auth
   amortisation). Track thread ids keyed by the client conversation.
3. Full usage accounting: map codex ``turn/completed`` usage onto Anthropic
   ``usage.input_tokens`` too (streaming currently reports only real output
   tokens when codex supplies them; input tokens stay estimated).
4. Error + rate-limit mapping: translate codex ``turn/failed`` / auth-expired /
   quota errors to the correct Anthropic HTTP status + ``error.type`` (and the
   correct SSE error framing) instead of a generic ``api_error``.
5. Endpoint auth: require the ``x-api-key`` / ``authorization`` header the
   Anthropic client already sends, and bind non-loopback only behind that.
6. Coordinate with the ecosystem's LLM plumbing (scitex-genai / litellm) rather
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

    # streaming round-trip
    curl -N http://127.0.0.1:8787/v1/messages -H 'content-type: application/json' \
      -d '{"model":"gpt-5.5","stream":true,
           "messages":[{"role":"user","content":"Reply with one word: pong"}]}'

    # built-in smoke tests (spin up the server in-process, one round-trip each)
    python -m scitex_agent_container._experimental.codex_anthropic_adapter smoke
    python -m scitex_agent_container._experimental.codex_anthropic_adapter stream-smoke

Experimental: this module lives under ``_experimental/`` and is NOT imported by
the package, wired into any spec, or covered by the CLI. It is a spike.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from ._codex_driver import (
    DEFAULT_MODEL,
    TURN_TIMEOUT_S,
    CodexError,
    _estimate_tokens,
    iter_codex_events,
    run_codex_turn,
)
from ._codex_sse import (
    codex_stream_to_anthropic_sse,
    format_sse_frame,
    parse_sse_stream,
)

# Re-export the driver/SSE public API so existing
# ``from ...codex_anthropic_adapter import run_codex_turn`` imports keep working.
__all__ = [
    "CodexError",
    "run_codex_turn",
    "iter_codex_events",
    "codex_stream_to_anthropic_sse",
    "format_sse_frame",
    "flatten_request",
    "build_anthropic_response",
    "serve",
    "smoke",
    "stream_smoke",
    "main",
]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


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
                parts.append(
                    f"[tool_result {_content_to_text(block.get('content', ''))}]"
                )
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


def build_anthropic_response(
    reply_text: str, model: str, prompt: str
) -> dict[str, Any]:
    """Wrap the codex reply in a non-streaming Anthropic Messages response."""
    import uuid

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": reply_text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            # MVP: estimated, not codex-reported. See follow-up #3.
            "input_tokens": _estimate_tokens(prompt),
            "output_tokens": _estimate_tokens(reply_text),
        },
    }


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    server_version = "codex-anthropic-adapter/0.2"

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter default log
        sys.stderr.write("[adapter] " + (fmt % args) + "\n")

    def _stream(self, prompt: str, model: str, codex_model: str) -> None:
        """Emit the Anthropic SSE event sequence for a ``stream:true`` request."""
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "keep-alive")
        self.end_headers()
        codex_events = iter_codex_events(prompt, model=codex_model)
        try:
            for etype, data in codex_stream_to_anthropic_sse(
                codex_events, model=model, prompt=prompt
            ):
                self.wfile.write(format_sse_frame(etype, data).encode())
                self.wfile.flush()
        except (CodexError, FileNotFoundError) as exc:
            # message_start already went out; surface the failure as an SSE
            # error event per the Anthropic streaming error convention.
            self.wfile.write(
                format_sse_frame(
                    "error",
                    {"type": "error", "error": {"type": "api_error", "message": str(exc)}},
                ).encode()
            )
            self.wfile.flush()
        except Exception as exc:  # noqa: BLE001 — MVP generic mapping
            self.wfile.write(
                format_sse_frame(
                    "error",
                    {"type": "error", "error": {"type": "api_error", "message": repr(exc)}},
                ).encode()
            )
            self.wfile.flush()

    def do_POST(self) -> None:  # noqa: N802 (stdlib signature)
        if self.path.rstrip("/") != "/v1/messages":
            self._json(
                404,
                {"type": "error", "error": {"type": "not_found_error", "message": self.path}},
            )
            return
        try:
            length = int(self.headers.get("content-length", 0))
            body = json.loads(self.rfile.read(length) or b"{}")
        except Exception as exc:  # malformed body
            self._json(
                400,
                {"type": "error", "error": {"type": "invalid_request_error", "message": str(exc)}},
            )
            return

        model = body.get("model") or DEFAULT_MODEL
        # MVP: codex app-server drives its own model; we default gpt-5.5 unless
        # the client model name looks like a gpt-* codex model.
        codex_model = model if str(model).startswith("gpt-") else DEFAULT_MODEL
        prompt = flatten_request(body)

        if body.get("stream"):
            self._stream(prompt, model=model, codex_model=codex_model)
            return

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
    """Build (but do not start) the adapter's threading HTTP server."""
    return ThreadingHTTPServer((host, port), _Handler)


# --------------------------------------------------------------------------- #
# Smoke tests: start the server in-process, POST a request, print the reply
# --------------------------------------------------------------------------- #
def smoke(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Non-streaming round-trip: one Anthropic request -> printed reply text."""
    import urllib.request

    httpd = serve(host, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
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


def stream_smoke(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> int:
    """Streaming round-trip: POST ``stream:true`` -> print the SSE event order."""
    import urllib.request

    httpd = serve(host, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        req_body = json.dumps(
            {
                "model": DEFAULT_MODEL,
                "max_tokens": 64,
                "stream": True,
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
            ctype = resp.headers.get("content-type", "")
            raw = resp.read().decode()
        print(f"=== content-type: {ctype} ===")
        events = parse_sse_stream(raw)
        text = "".join(
            data["delta"]["text"]
            for etype, data in events
            if etype == "content_block_delta"
        )
        print("=== SSE event order ===")
        print(" -> ".join(etype for etype, _ in events))
        print("\n=== reassembled assistant reply ===")
        print(text)
        return 0
    finally:
        httpd.shutdown()


def main(argv: Optional[list[str]] = None) -> int:
    """CLI entry point: ``serve`` (default) / ``smoke`` / ``stream-smoke``."""
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
    if cmd in ("stream-smoke", "stream_smoke"):
        return stream_smoke(host, port)
    if cmd == "serve":
        httpd = serve(host, port)
        print(
            f"[adapter] serving Anthropic Messages -> codex on http://{host}:{port}",
            file=sys.stderr,
        )
        print(
            f"[adapter] point clients at ANTHROPIC_BASE_URL=http://{host}:{port}",
            file=sys.stderr,
        )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            httpd.shutdown()
        return 0
    print(f"unknown command: {cmd!r} (use 'serve' / 'smoke' / 'stream-smoke')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
