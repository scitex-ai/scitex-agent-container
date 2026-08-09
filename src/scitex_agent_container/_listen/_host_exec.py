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
4. Execute the child — no shell, argv list only. Capture stdout/stderr, honour
   an optional timeout, return exit_code + duration. See "WHY THIS ENDPOINT
   WEDGED" below for the three guards that bound it.
5. Audit log every invocation as one JSONL line to
   ``~/.scitex/agent-container/runtime/logs/host_exec.log`` — {ts, caller,
   caller_group, argv, cwd, exit_code, duration_s, timed_out}. The operator
   accepted that the log records without preventing (Q1a/Q2a); it is the
   forensic trail.

No shell, no argv-string form. The body's ``argv`` MUST be a non-empty list of
strings. Anything else 400s. Guards against accidental shell-injection when
downstream consumers pass user input.

WHY THIS ENDPOINT WEDGED (INCIDENT 2026-07-17) — AND WHY "ADD A TIMEOUT" WAS
THE WRONG DIAGNOSIS
---------------------------------------------------------------------------
Measured during the outage: ``GET /v1/health`` answered 200 in 0.016s while
``POST /v1/host_exec`` returned **0 bytes at both 20s and 100s**. The endpoint
was read as "host_exec has no timeout". *It had one* — a 300s default, passed
to the child and empirically effective. The timeout was not missing; it was
**unreachable**.

The child timeout lives INSIDE the worker thread. This handler dispatched via
``asyncio.to_thread``, i.e. the event loop's SHARED default
``ThreadPoolExecutor``. When that pool has no free worker, the dispatch queues
and **the thread never starts — so the child never starts, so the child's
timeout never starts.** The handler then waits forever with nothing to bound
it, which is precisely "0 bytes, at any deadline you care to pick", while
``/v1/health`` (which never touches the pool) stays instant.

That is not a new theory. ``_lifecycle/_off_loop.py``'s module docstring names
*this file* as one of the unbounded ``to_thread`` callers that "queue behind
the wedged threads and hang FOREVER", and the #647 changelog entry records the
same fingerprint (health 200 while restarts timed out) from starvation by
zombie heartbeat threads. The fact was written down; this handler was never
changed to match it.

Hence the guards below, in the order they catch things:

1. DEDICATED THREAD, BOUNDED END-TO-END (:func:`_off_loop.run_blocking`) —
   a private daemon thread, never the shared pool, so a drained pool cannot
   delay this handler and this handler cannot drain the pool. The bound covers
   dispatch + exec, so it holds even when the child never starts. This is the
   guard that would have prevented the outage; the child timeout could not.
2. PROCESS-GROUP KILL, SIGTERM FIRST — ``start_new_session=True`` puts the
   child in its own process group and the timeout path kills the GROUP.
   ``subprocess.run``'s own timeout only SIGKILLs the direct child; measured, a
   grandchild (``bash -c 'sleep 60 & cat'``) SURVIVED and kept running. Killing
   the pid alone leaves the grandchildren that are usually the actual problem.
   The group gets SIGTERM plus a bounded grace BEFORE the SIGKILL: SIGKILL is
   uncatchable, so a bare one skipped the child's own cleanup and stranded
   ``.git/index.lock`` files on shared checkouts (INCIDENT 2026-07-18 — one
   lock broke the once-a-minute pull sweep for 83 minutes). See
   ``_TERM_GRACE_S``.
3. ``stdin=DEVNULL`` — no child invoked here can block on stdin (``git``
   without ``-F <file>``, an ssh passphrase prompt, ``apt``, a pager,
   ``read``). Measured: a stdin-reading child gets EOF in 0.02s instead of
   hanging. This does NOT break the ``echo <b64> | base64 -d | bash`` delivery
   shape — that outer bash takes its script from ``-c`` and the inner pipeline
   builds its own stdin internally; both forms verified identical. No caller
   can send stdin anyway: the body has no ``stdin`` field.

A timeout that returns EMPTY is worthless — an empty body is indistinguishable
from success-with-no-output, a network fault, or a dead listener (all four had
to be separated by hand during the incident, and the first reading was wrong).
Every failure here returns a TYPED, LOUD error naming the deadline, the argv,
and the caller.

CONCURRENCY: this endpoint does NOT serialise. There is no global lock and no
in-flight cap; concurrent callers run concurrently, each on its own thread.
Stated explicitly because an undocumented global lock is exactly how the
starvation above stayed invisible. ``GET /v1/host_exec/inflight`` reports what
is currently running so a caller sees "N running, oldest 42s" instead of
inferring from silence.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._lifecycle._off_loop import run_blocking
from .._state.state_db_groups import resolve_group
from ._acl import deny_response, resolve_group_name
from ._host_exec_child import (
    _POST_KILL_DRAIN_S,
    _TERM_GRACE_S,
    ChildOutcome,
    _run_child,
)
from ._host_exec_inflight import (
    InflightExec,
    host_exec_inflight,
    inflight_snapshot,
    next_exec_id,
    register_inflight,
    unregister_inflight,
)
from ._plane_restart_detach import (
    PLANE_RESTART_DELAY_S,
    plane_restart_log_path,
    spawn_detached_plane_command,
)
from ._plane_targeting_argv import targets_listen_plane

# Operator-scoped groups (2026-07-01 Q1a + researcher; ``privileged`` added
# 2026-07-02 per operator request). Members of these groups are permitted to
# broker arbitrary commands as the operator's uid on the host. The
# ``privileged`` group (grant / dotfiles / claude-code-telegrammer) was added
# so those agents can run host ops and manage the fleet flexibly.
ELIGIBLE_GROUPS: frozenset[str] = frozenset({"developer", "researcher", "privileged"})

# Structured audit log — one JSONL entry per invocation. Path is fixed (matches
# the other runtime logs). Test seam: monkeypatch ``_audit_log_path``.
_AUDIT_LOG_PATH = (
    Path.home() / ".scitex" / "agent-container" / "runtime" / "logs" / "host_exec.log"
)

# Guardrails
_MAX_TIMEOUT_S: float = 3600.0  # 1h — image builds can take a while
_DEFAULT_TIMEOUT_S: float = 300.0

# Slack between the CHILD deadline (enforced inside the worker thread) and the
# WATCHDOG deadline (enforced on the event loop by ``run_blocking``). The
# watchdog only fires when the child timeout ITSELF failed to return — an
# unkillable D-state child, or a drain that outlived _POST_KILL_DRAIN_S. It
# must exceed the whole orderly teardown or it would pre-empt the path that can
# still report the child's partial output.
#
# DERIVED, never hardcoded: the orderly timeout path now costs the SIGTERM
# grace (added for the stranded-index.lock incident, 2026-07-18) PLUS the drain
# ceiling. Writing this as a literal is how the watchdog would silently start
# pre-empting the teardown the next time either constant is retuned.
_WATCHDOG_MARGIN_S: float = _TERM_GRACE_S + _POST_KILL_DRAIN_S + 10.0


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
    # Diagnostics only — never consulted for the allow/deny decision, so a
    # test that injects `group_resolver` alone keeps its existing behaviour.
    group_detail=resolve_group,
    audit_writer=_append_audit,
) -> JSONResponse:
    """``POST /v1/host_exec`` — see module docstring for the full contract.

    Body: ``{"argv": [str, ...], "cwd"?: str, "timeout_s"?: float, "env"?:
    {str: str}, "caller"?: str}``. There is deliberately no ``stdin`` field —
    the child always runs with stdin on /dev/null.
    Response 200: ``{"exit_code": int, "stdout": str, "stderr": str,
    "duration_s": float, "timed_out": bool, "killed_process_group": bool}``.
    A child that overran its ``timeout_s`` is a 200 with ``timed_out: true``
    (it ran, it was killed, and its partial output is real) — never an empty
    body.
    Errors: 400 (bad body), 403 (ACL deny — group not eligible), 500 (exec
    error not otherwise mapped), 504 (the watchdog fired — the child's own
    deadline failed to return; ``watchdog_fired: true``).
    """
    # ---- 1. Body ------------------------------------------------------------
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (empty/malformed JSON body → 400 below)
        body = None
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

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
        # The DECISION stays with group_resolver (a plain string compare, and
        # the seam tests inject through). Only the EXPLANATION is enriched:
        # `group ''` is three different situations wearing one face, and the
        # operator reading this denial has to pick between "label the agent"
        # and "you are looking at the wrong database". Saying which costs one
        # query on a path that is already failing.
        try:
            detail = group_detail(name=caller).describe()
        except Exception:  # noqa: BLE001 - never let diagnostics break a deny
            detail = f"group {group!r}"
        return deny_response(
            reason=(
                f"host_exec is restricted to groups {sorted(ELIGIBLE_GROUPS)}; "
                f"caller {caller!r} has {detail}"
            )
        )

    # ---- 3. Execute --------------------------------------------------------
    merged_env: dict[str, str] | None
    if extra_env is not None:
        merged_env = os.environ.copy()
        merged_env.update(extra_env)
    else:
        merged_env = None  # inherit parent env

    # DON'T RUN A COMMAND THAT WOULD KILL THIS REQUEST'S OWN SERVER.
    #
    # This endpoint is served BY the sac listen daemon, so `systemctl restart
    # sac-listen` (or `sac listen restart`) run INLINE kills the process group
    # answering the call. Measured 2026-08-09: exit_code -15, empty stdout, no
    # status — while the restart had actually SUCCEEDED. The caller cannot tell
    # "succeeded and killed my reporter" from "failed", so it retries, and a
    # retry restarts a healthy plane. scitex-storage reported the CLI form of
    # this on 2026-07-28: such a restart "must report ACCEPTED, else callers
    # retry and STACK restarts."
    #
    # So schedule it DETACHED and answer 202 — the same mechanism
    # `_agent_restart.py` already uses for agent self-restart (setsid + a short
    # delay so THIS response flushes before the bounce), rather than inventing a
    # third variant of "don't decapitate yourself".
    verdict = targets_listen_plane(argv)
    if verdict.targets_plane:
        log_path = plane_restart_log_path()
        try:
            spawn_detached_plane_command(argv, env=merged_env, log_path=log_path)
        except OSError as exc:
            # Launch failure of the detached child itself — surface it typed,
            # never a fake "scheduled".
            return JSONResponse(
                {
                    "error": f"{type(exc).__name__}: {exc}",
                    "argv": argv,
                    "detached": False,
                },
                status_code=500,
            )
        return JSONResponse(
            {
                "status": "scheduled",
                "argv": argv,
                "caller": caller,
                "reason": verdict.reason,
                "delay_s": PLANE_RESTART_DELAY_S,
                "log": log_path,
                "detail": (
                    "This command would restart the listen daemon that is serving "
                    "the request, so it was NOT run inline — running it inline "
                    "returns exit_code -15 with no output and no way to tell "
                    f"success from failure. It is scheduled detached in "
                    f"~{PLANE_RESTART_DELAY_S}s. Verify with an INDEPENDENT probe "
                    "(e.g. GET /v1/health) rather than by re-running this command."
                ),
            },
            status_code=202,
        )

    started = time.monotonic()
    timed_out = False
    watchdog_fired = False
    killed_process_group = False
    stdout = ""
    stderr = ""
    exit_code = -1
    exec_error: str | None = None

    entry = InflightExec(
        exec_id=next_exec_id(),
        caller=caller,
        argv=tuple(argv),
        timeout_s=timeout_s,
        started_monotonic=started,
    )
    register_inflight(entry)
    try:
        # Dispatch to a DEDICATED daemon thread, bounded end-to-end.
        #
        # NOT `asyncio.to_thread`: that uses the event loop's SHARED default
        # ThreadPoolExecutor, and when the pool is drained the dispatch queues
        # — the thread never starts, so the child never starts, so the CHILD'S
        # OWN TIMEOUT NEVER STARTS, and this handler waits forever returning 0
        # bytes while /v1/health stays instant. That is the 2026-07-17 outage,
        # and it is why "the child has a timeout" was never a defence: the
        # timeout was inside the thing that never ran. `_off_loop.run_blocking`
        # uses a private daemon thread (immune to a drained pool, and it cannot
        # drain the pool for others) and bounds the WHOLE dispatch on the event
        # loop, which holds even when the child never starts.
        #
        # Two deadlines, deliberately: the child's `timeout_s` is the normal
        # path (kills the process group, reports partial output); the watchdog
        # at +_WATCHDOG_MARGIN_S catches the case where that path ITSELF wedged.
        outcome = await run_blocking(
            _run_child,
            argv,
            cwd=cwd_raw,
            child_timeout_s=timeout_s,
            env=merged_env,
            timeout_s=timeout_s + _WATCHDOG_MARGIN_S,
        )
        exit_code = outcome.exit_code
        stdout = outcome.stdout
        stderr = outcome.stderr
        timed_out = outcome.timed_out
        killed_process_group = outcome.killed_process_group
    except asyncio.TimeoutError:
        # The child's own deadline failed to return (unkillable D-state child,
        # or an escaped grandchild holding the pipe past the drain ceiling).
        # Fail LOUD and typed — never an empty body, which the caller cannot
        # distinguish from success-with-no-output or a dead listener.
        watchdog_fired = True
        timed_out = True
        exec_error = (
            f"host_exec watchdog fired after "
            f"{timeout_s + _WATCHDOG_MARGIN_S:.1f}s (child timeout_s={timeout_s:.1f}s "
            f"did not return): argv={argv!r} caller={caller!r}. The child was "
            f"abandoned on its own thread; it starves no other caller."
        )
    except FileNotFoundError as exc:
        # argv[0] not found — surface a clear error instead of a raw 500.
        exec_error = f"command not found: {exc}"
    except Exception as exc:  # stx-allow: fallback (exec-layer errors must surface as a response, not a raw 500)
        exec_error = f"exec error: {type(exc).__name__}: {exc}"
    finally:
        unregister_inflight(entry.exec_id)

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
            "watchdog_fired": watchdog_fired,
            "killed_process_group": killed_process_group,
            "exec_error": exec_error,
        }
    )

    if exec_error is not None:
        return JSONResponse(
            {
                "error": exec_error,
                "exit_code": exit_code,
                "duration_s": duration_s,
                "timed_out": timed_out,
                "watchdog_fired": watchdog_fired,
            },
            status_code=504 if watchdog_fired else 500,
        )
    return JSONResponse(
        {
            "exit_code": exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "duration_s": duration_s,
            "timed_out": timed_out,
            "killed_process_group": killed_process_group,
        }
    )


__all__ = [
    "ELIGIBLE_GROUPS",
    "ChildOutcome",
    "InflightExec",
    "host_exec",
    "host_exec_inflight",
    "inflight_snapshot",
]
