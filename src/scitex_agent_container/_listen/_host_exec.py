"""``POST /v1/host_exec`` — arbitrary host-command bypass for developer +
researcher agents.

Operator directive 2026-07-01: developer and researcher agents must be able to
run any command on the host, brokered through the listen daemon. Unblocks
in-container image builds (``sac image build``), cron/systemd apply
(``systemctl --user restart``, crontab edits), and other host-only ops that
otherwise require the operator's shell.

FLOW:
1. Bearer-authed by the outer middleware; the inner identity resolver has
   already populated ``request.state.authenticated_node`` (per-node bearer) or
   left it None (host-wide bearer / admin path).
2. Caller identity: prefer the authenticated node; fall back to an optional
   body ``caller`` claim (host-wide bearer path only — same caveat as the
   ``agents_start``/``agent_restart`` handlers).
3. GROUP GATE: resolve the caller's group via
   ``resolve_group_name`` and refuse with 403 unless it is one of
   ``ELIGIBLE_GROUPS`` (developer, researcher, privileged). The operator
   explicitly scoped arbitrary host-exec to developer + researcher
   (2026-07-01 Q1a) and added ``privileged`` on 2026-07-02.
4. Execute ``subprocess.run(argv, ...)`` — no shell, argv list only. Capture
   stdout/stderr, honour an optional timeout, return exit_code + duration.
5. Audit log every invocation as one JSONL line to
   ``~/.scitex/agent-container/runtime/logs/host_exec.log`` — {ts, caller,
   caller_group, argv, cwd, exit_code, duration_s, timed_out}. The operator
   accepted that the log records without preventing (Q1a/Q2a); it is the
   forensic trail.

No shell, no argv-string form. The body's ``argv`` MUST be a non-empty list of
strings. Anything else 400s. Guards against accidental shell-injection when
downstream consumers pass user input.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from ._acl import deny_response, resolve_group_name

# Operator-scoped groups (2026-07-01 Q1a + researcher; ``privileged`` added
# 2026-07-02 per operator request). Members of these groups are permitted to
# broker arbitrary commands as the operator's uid on the host. The
# ``privileged`` group (e.g. grant / dotfiles / channel-broker agents) was
# added so those agents can run host ops and manage the fleet flexibly.
ELIGIBLE_GROUPS: frozenset[str] = frozenset(
    {"developer", "researcher", "privileged"}
)

# Structured audit log — one JSONL entry per invocation. Path is fixed (matches
# the other runtime logs). Test seam: monkeypatch ``_audit_log_path``.
_AUDIT_LOG_PATH = (
    Path.home() / ".scitex" / "agent-container" / "runtime" / "logs" / "host_exec.log"
)

# Guardrails
_MAX_TIMEOUT_S: float = 3600.0  # 1h — image builds can take a while
_DEFAULT_TIMEOUT_S: float = 300.0


def _audit_log_path() -> Path:
    return _AUDIT_LOG_PATH


def _append_audit(entry: dict[str, Any]) -> None:
    """Append one JSONL line. Best-effort — a failed audit log MUST NOT break
    the response (the exec already ran; log the miss to stderr and continue)."""
    path = _audit_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as exc:  # stx-allow: fallback (best-effort audit; must not shadow the real exec result)
        # Log to stderr so the miss is visible in the listen journal, then
        # continue — the exec's real result still returns to the caller.
        import sys

        print(
            f"host_exec: audit log append failed at {path}: {exc}",
            file=sys.stderr,
        )


async def host_exec(
    request: Request,
    *,
    group_resolver=resolve_group_name,
    audit_writer=_append_audit,
) -> JSONResponse:
    """``POST /v1/host_exec`` — see module docstring for the full contract.

    Body: ``{"argv": [str, ...], "cwd"?: str, "timeout_s"?: float, "env"?:
    {str: str}, "caller"?: str}``.
    Response 200: ``{"exit_code": int, "stdout": str, "stderr": str,
    "duration_s": float, "timed_out": bool}``.
    Errors: 400 (bad body), 403 (ACL deny — group not eligible), 500 (exec
    error not otherwise mapped).
    """
    # ---- 1. Body ------------------------------------------------------------
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (empty/malformed JSON body → 400 below)
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "body must be a JSON object"}, status_code=400
        )

    argv = body.get("argv")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(a, str) for a in argv)
    ):
        return JSONResponse(
            {"error": "'argv' must be a non-empty list of strings (no shell form)"},
            status_code=400,
        )

    cwd_raw = body.get("cwd")
    if cwd_raw is not None and not isinstance(cwd_raw, str):
        return JSONResponse(
            {"error": "'cwd' must be a string if present"}, status_code=400
        )

    timeout_raw = body.get("timeout_s")
    if timeout_raw is None:
        timeout_s: float = _DEFAULT_TIMEOUT_S
    else:
        try:
            timeout_s = float(timeout_raw)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "'timeout_s' must be a number if present"}, status_code=400
            )
        if timeout_s <= 0 or timeout_s > _MAX_TIMEOUT_S:
            return JSONResponse(
                {"error": f"'timeout_s' must be in (0, {_MAX_TIMEOUT_S}]"},
                status_code=400,
            )

    env_raw = body.get("env")
    extra_env: dict[str, str] | None
    if env_raw is None:
        extra_env = None
    elif isinstance(env_raw, dict) and all(
        isinstance(k, str) and isinstance(v, str) for k, v in env_raw.items()
    ):
        extra_env = env_raw
    else:
        return JSONResponse(
            {"error": "'env' must be a {str: str} mapping if present"},
            status_code=400,
        )

    claimed_caller = body.get("caller")
    if claimed_caller is not None and not isinstance(claimed_caller, str):
        return JSONResponse(
            {"error": "'caller' must be a string if present"}, status_code=400
        )

    # ---- 2. Caller identity + group gate -----------------------------------
    authenticated = getattr(request.state, "authenticated_node", None)
    caller = authenticated if authenticated is not None else claimed_caller
    if not caller:
        # Neither the per-node bearer nor a claimed caller resolved — refuse
        # (arbitrary host-exec is never permitted without a resolvable caller).
        return deny_response(
            reason="host_exec requires a resolvable caller (per-node bearer or 'caller' body claim)"
        )

    group = group_resolver(name=caller)
    if group not in ELIGIBLE_GROUPS:
        return deny_response(
            reason=(
                f"host_exec is restricted to groups {sorted(ELIGIBLE_GROUPS)}; "
                f"caller {caller!r} resolves to group {group!r}"
            )
        )

    # ---- 3. Execute --------------------------------------------------------
    merged_env: dict[str, str] | None
    if extra_env is not None:
        merged_env = os.environ.copy()
        merged_env.update(extra_env)
    else:
        merged_env = None  # inherit parent env

    started = time.monotonic()
    timed_out = False
    stdout = ""
    stderr = ""
    exit_code = -1
    exec_error: str | None = None
    try:
        # Dispatch the blocking subprocess OFF the event loop. Running
        # subprocess.run() directly in this async handler blocks the SINGLE
        # uvicorn event loop for the command's whole lifetime — a long
        # host_exec (e.g. a ~13-min `sac image build`) then starves EVERY
        # other endpoint (a2a, health, spawn), the exact "agents can't reach
        # sac" outage (INCIDENT 2026-07-02). asyncio.to_thread keeps the loop
        # free to serve other requests concurrently; subprocess.TimeoutExpired
        # raised inside the thread still propagates through the await.
        completed = await asyncio.to_thread(
            subprocess.run,
            argv,
            cwd=cwd_raw,
            timeout=timeout_s,
            capture_output=True,
            text=True,
            env=merged_env,
            check=False,
        )
        exit_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else (
            exc.stdout.decode("utf-8", "replace") if exc.stdout else ""
        )
        stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else (
            exc.stderr.decode("utf-8", "replace") if exc.stderr else ""
        )
        exit_code = -1
    except FileNotFoundError as exc:
        # argv[0] not found — surface a clear error instead of a raw 500.
        exec_error = f"command not found: {exc}"
    except Exception as exc:  # stx-allow: fallback (exec-layer errors must surface as a response, not a raw 500)
        exec_error = f"exec error: {type(exc).__name__}: {exc}"

    duration_s = round(time.monotonic() - started, 3)

    # ---- 4. Audit + response ----------------------------------------------
    audit_writer(
        {
            "ts": time.time(),
            "caller": caller,
            "caller_group": group,
            "argv": argv,
            "cwd": cwd_raw,
            "timeout_s": timeout_s,
            "exit_code": exit_code,
            "duration_s": duration_s,
            "timed_out": timed_out,
            "exec_error": exec_error,
        }
    )

    if exec_error is not None:
        return JSONResponse(
            {
                "error": exec_error,
                "exit_code": exit_code,
                "duration_s": duration_s,
                "timed_out": False,
            },
            status_code=500,
        )
    return JSONResponse(
        {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_s": duration_s,
            "timed_out": timed_out,
        }
    )


__all__ = ["ELIGIBLE_GROUPS", "host_exec"]
