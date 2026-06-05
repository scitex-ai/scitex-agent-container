"""Agent prompt/key + start routes for ``sac listen`` (extracted from server.py).

Hosts the claude-execution surface — sending a prompt or interrupt key to
an agent and starting an agent — plus the claude-binary resolver and the
SSE streaming helper they depend on:

    POST /agents/<name>/send   → :func:`agent_send`
    POST /agents               → :func:`agents_start`

Split out of :mod:`scitex_agent_container._listen.server` (which grew
past the per-file line cap). ``server.py`` re-imports the handlers so
route registration (:func:`_v1_agent_routes`) and the historical
``from ..._listen.server import agent_send`` import path keep working
unchanged. No behaviour change — this is a pure extraction of one
cohesive responsibility (driving the claude binary / starting agents)
away from the host control-plane wiring that stays in ``server.py``.
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
from ._acl import check_spawn, deny_response
from ._forward import forward_to_live_runner
from ._inline_spec import materialize_inline_spec

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


async def agents_start(request: Request) -> JSONResponse:
    """POST /agents — start one or more agents.

    Body shapes:

        # Start a pre-registered spec (existing on disk):
        {"name": "<existing-spec-name>", "caller": "<sender-name>"}

        # Register-and-start an ad-hoc spec in one call:
        {
            "name": "<name>",
            "caller": "<sender-name>",
            "spec": {"apiVersion": "scitex-agent-container/v3",
                     "kind": "Agent",
                     "spec": {...}},
            "overwrite": false   # optional; default false → 409 on clash
        }

    WI-2 spawn-permission gate (limited scope per lead 2026-05-20):
    the optional ``caller`` field carries the spawning node's name
    (same self-claimed-identity caveat as ``message:send``'s
    ``metadata.from_agent``). The gate is **root-only** today —
    a node with no parent in the ``lineage`` table may spawn; a
    child gets a clear 403. ``caller`` omitted = administrative /
    human-operator path → allowed.

    On allow, the parent → child edge is recorded in ``lineage``
    so the new agent inherits the spawner's group.
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

    # WI-2 spawn-permission gate.
    caller = body.get("caller")
    if caller is not None and not isinstance(caller, str):
        return JSONResponse(
            {"error": "'caller' must be a string if present"}, status_code=400
        )
    decision, reason = check_spawn(caller=caller)
    if decision == "deny":
        return deny_response(reason or "spawn denied")

    inline_spec = body.get("spec")
    if inline_spec is not None:
        # PR-2 — pass ``caller`` through so the bind translate can
        # look up the parent agent's host-side bind map and rewrite
        # in-SIF ``/work/...`` sources before the PR-1 preflight
        # runs. Caller absent → translate disabled, preflight
        # enforces directly (the operator/admin path).
        err = materialize_inline_spec(
            name,
            inline_spec,
            overwrite=bool(body.get("overwrite")),
            caller=caller,
        )
        if err is not None:
            return err

    # Record lineage on allowed-spawn so the new child inherits the
    # caller's group. ``caller=None`` → no lineage record (admin /
    # operator path; the new agent starts as a root).
    if caller:
        from .._state.state_db_nodes import record_lineage as _record_lineage

        try:
            _record_lineage(child=name, parent=caller)
        except ValueError as exc:
            # Idempotent same-parent re-record is fine; a re-parent
            # to a different caller is loudly rejected.
            return JSONResponse({"error": str(exc)}, status_code=409)

    sac_bin = shutil.which("sac") or "sac"
    # ``agents`` (plural) is the canonical command group; the singular
    # ``agent`` form was removed in the F-CS13 rename and the host CLI
    # no longer exposes it (verified 2026-06-01 by the SAC-from-SAC
    # live test: the singular form returned "Error: No such command
    # 'agent'." with rc=2, breaking every brokered spawn from inside
    # a SIF). Using the canonical plural is what every other CLI
    # call site already does — see ``cli_pkg/fleet_group.py``
    # ``[remote_sac, "agents", "start", name]``.
    from datetime import datetime, timezone

    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # --broker-self recursive re-entry fix (clew dogfood repro
    # 2026-06-06, lead msg d8f61055): when the listen runs inside a
    # parent SAC's SIF on a SLURM allocation, this handler's child
    # `sac agents start` inherits APPTAINER_CONTAINER /
    # SINGULARITY_CONTAINER from the listen's env. That makes
    # is_in_sif() return True in the child, which re-enters
    # maybe_broker_in_sif_spawn → tries to broker the spawn to
    # *this same listen* → recursive InSifBrokerError loop.
    #
    # The listen IS the host-side spawn boundary: the child should
    # take the bare-host path (direct apptainer-exec the sibling
    # SIF), not pretend it is still inside a parent SIF. Strip the
    # in-SIF env markers so is_in_sif() returns False on the child.
    # No other downstream sac code reads these env vars except the
    # broker probe, so the strip is safe; apptainer ITSELF re-sets
    # APPTAINER_CONTAINER inside the child SIF it execs.
    child_env = dict(os.environ)
    child_env.pop("APPTAINER_CONTAINER", None)
    child_env.pop("SINGULARITY_CONTAINER", None)
    proc = await asyncio.to_thread(
        subprocess.run,
        [sac_bin, "agents", "start", name],
        capture_output=True,
        text=True,
        check=False,
        env=child_env,
    )
    if proc.returncode != 0:
        # PR-1 — stillborn agent observability. The subprocess can exit
        # non-zero for many reasons, including apptainer FATAL on a bind
        # source the host can't see (the clew capsule case). Write a
        # ``runtime_dir/STARTUP_FAILED`` marker so:
        #   * a subsequent ``DELETE`` returns 410 Gone with the failure
        #     payload instead of 404 "no pid file",
        #   * ``GET .../status`` can surface ``status="startup_failed"``
        #     instead of vacuous registry-only fields,
        #   * future fleet-GC automation can drop the stillborn cleanly.
        # The marker write is best-effort: a write failure (e.g. /state
        # filesystem RO) MUST NOT shadow the underlying spawn failure.
        # stx-allow: fallback (reason: marker is observability metadata
        # only; failing here would only obscure the real start failure)
        try:
            from .._lifecycle._startup_failed import write_marker
            from .._runners._session_state import state_dir_for

            runtime_dir = state_dir_for(name)
            write_marker(
                runtime_dir,
                started_at=started_at,
                phase="container_creation",
                exit_code=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
            )
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass

    return JSONResponse(
        {
            "name": name,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        status_code=200 if proc.returncode == 0 else 502,
    )
