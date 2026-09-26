"""``POST /agents/<name>/send`` route + claude-binary resolver + SSE stream.

Split out of :mod:`scitex_agent_container._listen._agent_exec` to keep that
module under the per-file line cap. ``_agent_exec`` re-imports
``agent_send`` and ``_find_claude_binary`` (server.py imports them from
``_agent_exec``), so the public import paths are unchanged.

    POST /agents/<name>/send → :func:`agent_send`
"""

from __future__ import annotations

import asyncio
import json as _json
import os
import shutil

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .._runners._session_state import state_dir_for
from ..config import load_config
from ..config._resolve import resolve_config
from ._forward import forward_exchange_from_live_runner, forward_to_live_runner

__all__ = [
    "_find_claude_binary",
    "_sse_frame",
    "_stream_claude",
    "agent_send",
    "agent_exchange",
]


#: Bound on the ``claude --resume`` fallback, in seconds. Generous by
#: design — a real turn can take minutes, and the point of the bound is
#: that one EXISTS, not that it is tight. Override per-host with
#: ``SAC_LISTEN_RESUME_TIMEOUT_S``.
DEFAULT_RESUME_TIMEOUT_S = 300.0


def _resume_timeout_s() -> float:
    """Read the re-launch bound from the environment, or fail loud.

    A malformed value RAISES rather than quietly reverting to the
    default: an operator who writes ``SAC_LISTEN_RESUME_TIMEOUT_S=30s``
    has stated an intent, and silently ignoring it would restore the
    very "looks configured, isn't" shape this whole change is about.
    """
    raw = os.environ.get("SAC_LISTEN_RESUME_TIMEOUT_S")
    if raw is None or raw == "":
        return DEFAULT_RESUME_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        raise ValueError(
            f"SAC_LISTEN_RESUME_TIMEOUT_S={raw!r} is not a number of seconds. "
            f"Set it to a bare float (e.g. '300') or unset it to use the "
            f"default {DEFAULT_RESUME_TIMEOUT_S:g}s."
        ) from None
    if value <= 0:
        raise ValueError(
            f"SAC_LISTEN_RESUME_TIMEOUT_S={raw!r} must be > 0. A zero or "
            f"negative bound would kill every re-launch instantly. Unset it "
            f"to use the default {DEFAULT_RESUME_TIMEOUT_S:g}s."
        )
    return value


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

    Routing for ``type: prompt``: require the agent's live canonical
    ``/v1/turn`` endpoint. There is no process-resume or tmux fallback.

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

    # Public prompt delivery has exactly one transport. A missing live endpoint
    # is a refusal, never permission to launch a second Claude process against
    # the session store or to type into a tmux pane.
    return JSONResponse(
        {
            "name": name,
            "kind": "no_live_turn_endpoint",
            "error": (
                "public send requires the running agent's canonical /v1/turn "
                "exchange endpoint; no port is registered. SAC will not run "
                "claude --resume or inject tmux input as a fallback"
            ),
            "hint": (
                f"start or repair the agent, then verify `sac agents status {name}` "
                "reports a2a_port before resending"
            ),
        },
        status_code=409,
    )


async def agent_exchange(request: Request) -> Response:
    """Proxy a canonical exchange lookup for an in-SIF send client."""
    name = request.path_params["name"]
    exchange_id = request.path_params["exchange_id"]
    try:
        cfg = load_config(resolve_config(name))
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    return await forward_exchange_from_live_runner(cfg, name, exchange_id)


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
