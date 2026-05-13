"""Starlette app factory for ``sac listen``.

Hosts symmetric ``/v1/sac/agents/...`` and ``/v1/a2a/...`` namespaces as
designed in REQUIREMENT_SUMMARY.md §4. v1 endpoints:

    GET    /v1/health
    GET    /v1/sac/agents
    POST   /v1/sac/agents                       (create/start from spec)
    GET    /v1/sac/agents/<name>/status
    GET    /v1/sac/agents/<name>/tail           (SSE stream of session.jsonl)
    POST   /v1/sac/agents/<name>/send           (prompt or key)
    GET    /v1/sac/agents/<name>/card           (A2A-compatible card)
    DELETE /v1/sac/agents/<name>

The ``/v1/a2a/...`` mirror registers the same handlers under the A2A
protocol-compat prefix. No backward-compat for the legacy ``/v1/sac/``
paths — those are dropped wholesale (operator-stated stance).
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import shutil
import subprocess
import urllib.error as _urlerror
import urllib.request as _urlrequest
from datetime import datetime
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from .._runners._session_state import read_session_id, state_dir_for
from .._state.registry import Registry
from ..config import load_config
from ..config._resolve import resolve_config
from ._inline_spec import materialize_inline_spec
from .auth import BearerAuthMiddleware

# Re-exported under the module's public surface so unit tests can patch
# them as ``scitex_agent_container._listen.server._urlrequest.urlopen``
# / ``._urlerror.URLError`` without forcing every call site to alias.
__all__ = ["create_app", "_urlrequest", "_urlerror"]


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


# --- Handlers --------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "sac-listen", "v": 1})


async def list_agents(_request: Request) -> JSONResponse:
    """List agents the local Registry knows about."""
    try:
        reg = Registry()
        rows = reg.list_all()
    except Exception as exc:  # stx-allow: fallback (reason: surface a JSON error to the caller rather than ASGI 500 stack)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"agents": rows})


async def agent_status(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    return JSONResponse(
        {
            "name": name,
            "spec_path": str(spec_path),
            "workdir": cfg.expanded_workdir,
            "session_id": sid,
            "state_dir": str(sd),
        }
    )


from ._forward import forward_to_live_runner  # noqa: E402


async def agent_send(request: Request) -> Response:
    """POST /v1/sac/agents/<name>/send.

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
        SIGINT the live runner pid (best-effort). ESC / C-c / SIGINT
        accepted; unknown keys → 400. No live runner → 404.
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
        key = body.get("key")
        # Supported: ESC / C-c / SIGINT — all map to SIGINT on the
        # runner pid, which interrupts the current turn without
        # killing the agent.
        if key not in ("ESC", "C-c", "SIGINT"):
            return JSONResponse(
                {
                    "error": (
                        f"unsupported key={key!r}; expected one of "
                        "'ESC', 'C-c', 'SIGINT'"
                    )
                },
                status_code=400,
            )
        import signal as _signal

        sd = state_dir_for(name)
        pid_file = sd / "pid"
        if not pid_file.is_file():
            return JSONResponse(
                {"error": f"agent {name!r} has no live session"},
                status_code=404,
            )
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, _signal.SIGINT)
        except (OSError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        return JSONResponse(
            {
                "name": name,
                "route": "interrupt",
                "pid": pid,
                "signal": "SIGINT",
            }
        )
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


# --- tail (SSE over session.jsonl) ----------------------------------------


def _parse_iso_ts(value: str) -> datetime | None:
    """Best-effort ISO-8601 parser. Returns None on failure."""
    if not isinstance(value, str) or not value:
        return None
    s = value.rstrip("Z")
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def _record_ts(record: dict) -> datetime | None:
    """Pluck a timestamp from a session.jsonl record; ``ts`` or ``timestamp``."""
    for key in ("ts", "timestamp"):
        raw = record.get(key)
        if raw is None:
            continue
        parsed = _parse_iso_ts(raw) if isinstance(raw, str) else None
        if parsed is not None:
            return parsed
    return None


def _runtime_session_jsonl(name: str) -> Path:
    """Per-agent session.jsonl path. Patchable in tests."""
    return (
        Path(os.path.expanduser("~"))
        / ".scitex"
        / "agent-container"
        / "runtime"
        / name
        / "session.jsonl"
    )


async def _stream_tail(path: Path, since: datetime | None, follow: bool):
    """Yield SSE frames for each line of ``path``; tail when follow=True.

    Each frame: ``data: {"line_no": N, "record": <obj>}``. Heartbeats
    ``: keep-alive`` every 15s during follow when idle.
    """
    line_no = 0
    seen_since = since is None  # if no since filter, include from line 0
    # If the file doesn't exist yet, in follow=true we still want to
    # wait for it to appear; in non-follow mode we close immediately.
    if not path.is_file():
        if not follow:
            return

    # Open once, read to EOF, then (if follow) keep polling.
    while not path.is_file():
        await asyncio.sleep(0.5)

    last_heartbeat = asyncio.get_event_loop().time()
    with path.open("r", encoding="utf-8") as fh:
        while True:
            line = fh.readline()
            if line:
                line_no += 1
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                except _json.JSONDecodeError:
                    # Malformed line — surface as a string payload.
                    record = {"raw": line}

                if since is not None:
                    rec_ts = _record_ts(record) if isinstance(record, dict) else None
                    if rec_ts is None:
                        # No timestamp on record: include only after we've
                        # already crossed the since boundary.
                        if not seen_since:
                            continue
                    elif rec_ts < since:
                        continue
                    else:
                        seen_since = True

                payload = _json.dumps({"line_no": line_no, "record": record})
                yield _sse_frame(None, payload)
                last_heartbeat = asyncio.get_event_loop().time()
                continue

            # EOF
            if not follow:
                return
            # Heartbeat every 15s of idle.
            now = asyncio.get_event_loop().time()
            if now - last_heartbeat >= 15.0:
                yield b": keep-alive\n\n"
                last_heartbeat = now
            try:
                await asyncio.sleep(0.5)
            except (asyncio.CancelledError, GeneratorExit):
                raise


async def agent_tail(request: Request) -> Response:
    """GET /v1/sac/agents/<name>/tail?since=<iso>&follow=<bool>.

    Server-Sent Events stream of the per-agent ``session.jsonl`` lines
    at ``~/.scitex/agent-container/runtime/<name>/session.jsonl``.
    """
    name = request.path_params["name"]
    since_raw = request.query_params.get("since")
    follow_raw = request.query_params.get("follow", "false")
    follow = str(follow_raw).lower() in ("1", "true", "yes")
    since = _parse_iso_ts(since_raw) if since_raw else None

    path = _runtime_session_jsonl(name)
    if not follow and not path.is_file():
        return JSONResponse(
            {"error": f"no session.jsonl for {name!r}"}, status_code=404
        )

    return StreamingResponse(
        _stream_tail(path, since, follow),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def agents_start(request: Request) -> JSONResponse:
    """POST /v1/sac/agents — start one or more agents.

    Body shapes:

        # Start a pre-registered spec (existing on disk):
        {"name": "<existing-spec-name>"}

        # Register-and-start an ad-hoc spec in one call:
        {
            "name": "<name>",
            "spec": {"apiVersion": "scitex-agent-container/v3",
                     "kind": "Agent",
                     "spec": {...}},
            "overwrite": false   # optional; default false → 409 on clash
        }
    """
    try:
        body = await request.json()
    except (
        Exception
    ):  # stx-allow: fallback (reason: malformed JSON → 400 instead of 500)
        return JSONResponse({"error": "body must be JSON"}, status_code=400)
    name = body.get("name")
    if not isinstance(name, str) or not name:
        return JSONResponse(
            {"error": "missing or empty 'name' string"}, status_code=400
        )

    inline_spec = body.get("spec")
    if inline_spec is not None:
        err = materialize_inline_spec(
            name, inline_spec, overwrite=bool(body.get("overwrite"))
        )
        if err is not None:
            return err

    sac_bin = shutil.which("sac") or "sac"
    proc = await asyncio.to_thread(
        subprocess.run,
        [sac_bin, "agent", "start", name],
        capture_output=True,
        text=True,
        check=False,
    )
    return JSONResponse(
        {
            "name": name,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        status_code=200 if proc.returncode == 0 else 502,
    )


async def agent_card(request: Request) -> JSONResponse:
    """GET /v1/sac/agents/<name>/card (mirrored at /v1/a2a/agents/<name>/card).

    Returns an A2A-compatible AgentCard built from the agent's v3 spec.
    """
    import yaml

    from ..a2a._card import project_card

    name = request.path_params["name"]
    try:
        spec_path = resolve_config(name)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    try:
        with open(spec_path, encoding="utf-8") as fh:
            v3 = yaml.safe_load(fh) or {}
    except OSError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    base_url = str(request.base_url).rstrip("/") + "/v1/a2a"
    card = project_card(name, v3, base_url)
    return JSONResponse(card)


async def agent_delete(request: Request) -> JSONResponse:
    """DELETE /v1/sac/agents/<name> — stop the agent."""
    name = request.path_params["name"]
    sd = state_dir_for(name)
    pid_file = sd / "pid"
    if not pid_file.is_file():
        return JSONResponse({"error": "no pid file"}, status_code=404)
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"name": name, "stopped": True, "pid": pid})


# --- App factory -----------------------------------------------------------


def _v1_agent_routes(prefix: str) -> list[Route]:
    """Build the agent route set under a given prefix.

    Used to register identical handlers at both ``/v1/sac/agents`` and
    ``/v1/a2a/agents`` per the symmetric-namespace requirement.
    """
    return [
        Route(f"{prefix}", list_agents, methods=["GET"]),
        Route(f"{prefix}", agents_start, methods=["POST"]),
        Route(f"{prefix}/{{name}}/status", agent_status, methods=["GET"]),
        Route(f"{prefix}/{{name}}/tail", agent_tail, methods=["GET"]),
        Route(f"{prefix}/{{name}}/send", agent_send, methods=["POST"]),
        Route(f"{prefix}/{{name}}/card", agent_card, methods=["GET"]),
        Route(f"{prefix}/{{name}}", agent_delete, methods=["DELETE"]),
    ]


def create_app(*, token: str) -> Starlette:
    """Build the Starlette app with bearer auth and v1 routes.

    Two symmetric prefixes share identical handlers:
        - /v1/sac/agents/...   sac-native verbs
        - /v1/a2a/agents/... A2A-protocol-compat mirror
    """
    routes: list[Route] = [Route("/v1/health", health, methods=["GET"])]
    routes += _v1_agent_routes("/v1/sac/agents")
    routes += _v1_agent_routes("/v1/a2a/agents")
    app = Starlette(routes=routes)
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
