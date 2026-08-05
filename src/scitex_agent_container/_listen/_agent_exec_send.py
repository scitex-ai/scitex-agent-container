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
import subprocess

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._runners._session_state import read_session_id, state_dir_for
from ..config import load_config
from ..config._resolve import resolve_config
from ._forward import forward_to_live_runner

__all__ = [
    "_find_claude_binary",
    "_sse_frame",
    "_stream_claude",
    "agent_send",
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
    #
    # BOUNDED ON PURPOSE. This ran with no timeout at all, so a claude
    # invocation that never returned held the request open forever while
    # every caller absorbed the wait privately and then blamed its own
    # 30s client deadline on a `sac listen` outage. An unbounded wait on
    # a subprocess is not patience, it is a hang with no upper bound and
    # no signal — the daemon looked healthy the whole time because, on
    # every other route, it was.
    try:
        timeout_s = _resume_timeout_s()
    except ValueError as exc:
        # Misconfiguration, not a transport fault — say so, and say which
        # variable, rather than dying as a bodyless ASGI 500.
        return JSONResponse(
            {"name": name, "kind": "bad_config", "error": str(exc)},
            status_code=500,
        )
    try:
        proc = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=workdir,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run kills the child before raising, so we are not
        # leaking a claude process here.
        return JSONResponse(
            {
                "name": name,
                "session_id": sid,
                "kind": "resume_timeout",
                "timeout_s": timeout_s,
                "error": (
                    f"the `claude --resume` re-launch for {name!r} did not "
                    f"finish within {timeout_s:g}s and was killed. The agent "
                    f"itself is untouched — this bounds the RE-LAUNCH, not "
                    f"the agent."
                ),
                "hint": (
                    f"Raise the bound with SAC_LISTEN_RESUME_TIMEOUT_S if "
                    f"long turns are expected here. If {name!r} is actually "
                    f"running, prefer its live rail instead of a re-launch: "
                    f"`sac a2a send {name} ...`."
                ),
                "stdout_tail": (exc.stdout or "")[-2_000:]
                if isinstance(exc.stdout, str)
                else "",
                "stderr_tail": (exc.stderr or "")[-2_000:]
                if isinstance(exc.stderr, str)
                else "",
            },
            status_code=504,
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
