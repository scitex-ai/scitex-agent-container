"""Agent prompt/key send route for ``sac listen`` (extracted from server.py).

Hosts the claude-execution surface — sending a prompt or a key to an
agent — plus the claude-binary resolver and the SSE streaming helper
they depend on:

    POST /agents/<name>/send   → :func:`agent_send`

The ``type: key`` branch is delegated to :mod:`._agent_exec_keys`
(cancel-keys → SIGINT, every other named key / sequence → tmux
send-keys). The ``POST /agents`` spawn route lives in
:mod:`._agent_start` and is re-exported here so ``server.py``'s route
registration (:func:`_v1_agent_routes`) and the historical
``from ..._listen.server import agent_send`` / ``agents_start`` import
paths keep working unchanged.
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import shutil
import subprocess

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._runners._session_state import read_session_id, state_dir_for
from ..config import load_config
from ..config._resolve import resolve_config
from ._agent_exec_keys import _handle_key_send
from ._agent_start import agents_start
from ._forward import forward_to_live_runner

__all__ = [
    "_find_claude_binary",
    "agent_send",
    "agents_start",
]


def _find_claude_binary() -> str:
    """Same resolver as send_cmds — bundled SDK copy first, then PATH."""
    bundled = (
        "/opt/venv-sac/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
    )
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude binary not found")
    return found


async def agent_send(request: Request) -> Response:
    """POST /agents/<name>/send.

    Body discriminator (per REQUIREMENT_SUMMARY §4.2):
        {"type":"prompt","prompt":"...","options":{...}}
        {"type":"key","key":"ESC"}

    Back-compat (this commit only): a body without ``type`` is treated
    as ``{type: "prompt", ...}`` so existing callers keep working.

    Routing for ``type: prompt``:
        1. If the agent has ``spec.a2a.port`` set and its inbound HTTP
           is reachable, forward the turn into the live in-memory
           runner inbox.
        2. Otherwise fall back to ``claude --resume <sid> -p`` —
           short-lived re-launch against the persisted session.jsonl.

    Routing for ``type: key``:
        Cancel keys (ESC / C-c / SIGINT) SIGINT the live runner pid
        (best-effort) to interrupt the turn. Every OTHER named key or
        key sequence (Enter, Up, Down, Tab, digits, …) is delivered to
        the agent's tmux session via send-keys. Unknown key names →
        400 with the valid set listed; no live runner → 404.
    """
    name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (reason: malformed JSON → 400 with explanation rather than ASGI 500)
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    # Default to prompt when ``type`` is absent — back-compat shim
    # documented in REQUIREMENT_SUMMARY §4.2.
    type_ = body.get("type", "prompt")
    if type_ == "key":
        return _handle_key_send(name, body)
    if type_ != "prompt":
        return JSONResponse(
            {"error": f"unknown type {type_!r}; expected 'prompt' or 'key'"},
            status_code=400,
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return JSONResponse(
            {"error": "missing or empty 'prompt' string"}, status_code=400
        )

    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)

    # 1) Try live-runner route first.
    options = body.get("options") or {}
    live = await forward_to_live_runner(cfg, name, prompt, options)
    if live is not None:
        return live

    # 2) Fall back to short-lived re-launch.
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    if not sid:
        return JSONResponse(
            {"error": f"no session_id recorded for {name!r}"}, status_code=409
        )

    try:
        claude_bin = _find_claude_binary()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    argv = [claude_bin, "--resume", sid, "-p", prompt]
    if "model" in options:
        argv += ["--model", str(options["model"])]
    if "max_turns" in options:
        argv += ["--max-turns", str(options["max_turns"])]

    workdir = cfg.expanded_workdir or os.getcwd()

    # SSE branch: client opted in via Accept: text/event-stream. Stream
    # claude's stdout line-by-line as SSE frames.
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        argv += ["--output-format", "stream-json", "--include-partial-messages"]
        return StreamingResponse(
            _stream_claude(argv, workdir, name, sid),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # Buffered branch (default): run to completion, return one JSON blob.
    proc = await asyncio.to_thread(
        subprocess.run,
        argv,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return JSONResponse(
        {
            "name": name,
            "session_id": sid,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    )


def _sse_frame(event: str | None, data: str) -> bytes:
    """Encode one SSE frame. ``event`` is optional; ``data`` is one line."""
    head = f"event: {event}\n" if event else ""
    return (head + f"data: {data}\n\n").encode("utf-8")


async def _stream_claude(argv: list[str], workdir: str, name: str, sid: str):
    """Run claude as an async subprocess and yield SSE frames."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        yield _sse_frame("error", _json.dumps({"error": str(exc)}))
        return

    yield _sse_frame("start", _json.dumps({"name": name, "session_id": sid}))

    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            yield _sse_frame(None, line.decode("utf-8", "replace").rstrip("\n"))
        rc = await proc.wait()
        yield _sse_frame("done", _json.dumps({"returncode": rc}))
    except (asyncio.CancelledError, GeneratorExit):
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
        raise

