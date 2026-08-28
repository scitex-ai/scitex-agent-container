"""``POST /agents/<name>/restart`` handler for ``sac listen``.

The container-side mirror of the spawn bypass (:mod:`._agent_exec`
``agents_start``), but for the *restart* lifecycle verb: an agent
running INSIDE a SIF cannot resolve a peer's LOCAL registry row +
local ``state.db`` (``sac agents restart`` hard-fails with "not found
in registry" from inside the container). This handler lets the
container ask the HOST listen to run the restart on the bare host —
where the registry row, the agent's runtime dir, and that node's
``sac listen`` token actually live.

Shape mirrors ``agents_start`` exactly:

  * resolve the caller identity from the body's ``caller`` field (the
    ``request.state.authenticated_node`` branch below outlived the
    per-node bearer feature removed 2026-08-28 and no longer fires),
  * run :func:`._acl.check_lineage_acl` (the MANAGE gate — self /
    lineage-descendant / developer-group / standard-fleet mesh) BEFORE
    any runtime work; deny → 403 with the ACL reason verbatim,
  * on allow, shell the canonical ``sac agents restart <name> --yes
    --json`` on the bare host (same host-shell path ``agents_start``
    uses for ``sac agents start``) and return its rc + stdout + stderr
    as JSON. A non-zero rc is surfaced as 502 (the gate passed but the
    bare-host restart itself failed), never swallowed.

SELF-RESTART special case (caller IS the target): shelling the restart
SYNCHRONOUSLY would DEADLOCK — the stop-half cannot complete while the
CALLING process is still blocked awaiting this HTTP response, so the
start-half sees "already running -> no-op" and returns a confusing 502
(incident 2026-07-12). When the resolved ``caller`` equals ``name`` the
handler instead spawns a fully-detached, deferred bounce (``setsid`` +
``start_new_session``) that sleeps a few seconds so THIS response flushes
first, then force-bounces the agent, and returns ``202`` with
``self_restart="scheduled"`` immediately. An external / admin restart
(caller is None, or caller != name) keeps the synchronous path unchanged.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import subprocess

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._sac_binary import SacBinaryNotFoundError, sac_binary
from ._acl import check_lineage_acl, deny_response

__all__ = ["agent_restart"]


# Delay (seconds) before a DETACHED self-restart bounce actually fires.
# Long enough for THIS handler's 202 to flush over the wire to the caller's
# MCP client and for that restart tool-call to unwind CLEANLY before the
# caller's own process is bounced — the self-restart deadlock is precisely
# that the caller cannot die while it is still awaiting this very response.
# Short enough that the heal is prompt (the corrected-dependency use case
# wants the fresh process back quickly). 3s sits comfortably above a
# localhost/LAN round-trip yet well under any human's patience.
_SELF_RESTART_DELAY_S = 3


def _build_detached_restart_argv(
    sac_bin: str,
    name: str,
    *,
    fresh: bool,
    delay_s: int,
    log_path: str,
) -> list[str]:
    """Build the fully-detached, deferred self-restart command (PURE — no I/O).

    Returned as a ``setsid sh -c '<inner>'`` argv so the constructed command
    is unit-assertable WITHOUT spawning anything. ``<inner>`` is::

        sleep <delay_s>; ( echo <marker>; date -Is; <bounce> ) >> <log> 2>&1

    * ``sleep <delay_s>`` defers the bounce past this handler's 202 flush so
      the caller's restart tool-call returns cleanly before the caller is
      bounced (see :data:`_SELF_RESTART_DELAY_S`).
    * ``<bounce>`` is the FORCED restart. ``sac agents restart`` exposes no
      ``--force`` flag (confirmed: ``cli_pkg/lifecycle/_restart.py`` defines
      none) and its start-leg runs without force, so a still-running agent
      trips "already running -> no-op -> use --force" — the exact confusing
      502 of the deadlock. The deterministic stop-if-running bounce is
      instead ``sac agents start <name> --force`` (the mechanism the
      ``fresh`` path already uses): ``--force`` stops any live instance
      first, and with NO session flag the session then follows the SPEC
      policy — byte-identical to what a plain ``sac agents restart``
      resolves (``_lifecycle/_stop.py::agent_restart`` calls
      ``agent_start(session_override=None)``) — so a resuming (non-fresh)
      restart is preserved. ``--fresh`` is appended only for a fresh
      (no-resume) bounce, mirroring the synchronous fresh path verbatim.
    * stdout+stderr are appended to ``log_path`` (NEVER ``/dev/null``) so the
      bounce that necessarily outlives this process is debuggable post-hoc.

    ``setsid`` (util-linux — on every host; the twin TTL-stop relies on it
    too) starts a new session so the child survives BOTH this handler's
    return AND the caller's imminent death.
    """
    bounce = [sac_bin, "agents", "start", name, "--force"]
    if fresh:
        bounce.append("--fresh")
    bounce.append("--json")
    bounce_str = " ".join(shlex.quote(tok) for tok in bounce)
    marker = shlex.quote(
        f"=== sac self-restart name={name} fresh={fresh} delay={int(delay_s)}s ==="
    )
    inner = (
        f"sleep {int(delay_s)}; "
        f"( echo {marker}; date -Is; {bounce_str} ) >> {shlex.quote(log_path)} 2>&1"
    )
    return ["setsid", "sh", "-c", inner]


def _spawn_detached(argv: list[str], *, env: dict[str, str]) -> None:
    """Fire-and-forget spawn of ``argv``, fully detached from this process.

    ``start_new_session=True`` (belt-and-suspenders with the ``setsid`` in
    ``argv``) severs the child from this handler's session / process-group so
    it survives the caller's imminent bounce; stdin is ``/dev/null`` and the
    child's stdout/stderr are already redirected to the log file inside the
    shell command, so they are ``DEVNULL`` here. A dedicated module-level
    seam so tests record the argv WITHOUT forking a real bouncer — the same
    save/restore-seam idiom ``_listen/_restart.py`` uses for ``_kill`` /
    ``_run_subprocess``.
    """
    subprocess.Popen(
        argv,
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )


async def agent_restart(request: Request) -> JSONResponse:
    """POST /agents/<name>/restart — restart the named agent on the host.

    The container-side mirror of the spawn bypass: the in-SIF
    :mod:`._lifecycle._restart_client` POSTs here so the restart runs
    on the bare host (where the registry row + runtime dir live)
    instead of failing "not found in registry" inside the container.

    Identity + ACL (mirrors ``agents_start`` / ``agent_delete``):

      * ``request.state.authenticated_node`` WAS the resolved per-node
        identity. Nothing has set it since the per-node bearer feature
        was removed 2026-08-28 (nothing ever minted one), so it reads
        ``None`` on every request — the host-wide bearer / operator
        path, always allowed by :func:`check_lineage_acl`.
      * The optional body ``caller`` field is therefore the ONLY caller
        identity this route sees. It carries the requesting node's name
        (same self-claimed caveat as ``agents_start``'s ``caller``) so
        the manage gate can apply the standard-fleet mesh for an
        admin-forwarded request. The precedence below is kept as-is: a
        resolved identity would still win over the body claim, and this
        is where one would arrive.
      * :func:`check_lineage_acl` is the MANAGE gate: self /
        lineage-descendant / developer-group / standard-fleet mesh.
        Deny → 403 + ``{"error","kind":"acl_deny","reason"}``.

    On allow, shell ``sac agents restart <name> --yes --json`` on the
    bare host and return its rc + stdout + stderr. rc != 0 → 502.

    Self-restart (resolved ``caller`` == ``name``): the synchronous shell
    would deadlock (the caller cannot die while awaiting this response), so
    the bounce is instead handed to a detached, deferred child and the
    handler returns ``202`` + ``self_restart="scheduled"`` at once. Honours
    ``fresh``: the detached child force-bounces with ``sac agents start
    <name> --force`` (resume, spec-policy session) or ``--force --fresh``.
    """
    name = request.path_params["name"]

    # Body is optional (the client may POST an empty body when it relies
    # on a per-node bearer for identity). A malformed body is tolerated:
    # the per-node bearer is the authoritative identity source.
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (reason: empty/malformed body → no caller claim; identity falls back to the per-node bearer below)
        body = {}
    if not isinstance(body, dict):
        body = {}
    claimed_caller = body.get("caller")
    if claimed_caller is not None and not isinstance(claimed_caller, str):
        return JSONResponse(
            {"error": "'caller' must be a string if present"}, status_code=400
        )

    # Resolve the effective caller for the MANAGE gate. A per-node bearer
    # (authenticated_node) would be the authoritative, unspoofable
    # identity, and would still win here — but nothing sets it since the
    # never-armed per-node bearer feature was removed 2026-08-28, so in
    # practice the body ``caller`` claim is what the gate sees, letting an
    # administrative forwarder name the on-behalf-of node for the mesh.
    authenticated = getattr(request.state, "authenticated_node", None)
    caller = authenticated if authenticated is not None else claimed_caller

    decision, reason = check_lineage_acl(caller=caller, target=name)
    if decision == "deny":
        return deny_response(reason or "lineage ACL deny")

    # ``fresh`` (optional): start a NEW Claude session instead of a resuming
    # restart — the deterministic recovery for an agent wedged on a boot prompt
    # whose queued-input buffer returns on every plain restart. Default (absent
    # / falsey) keeps the byte-identical plain-restart argv below.
    fresh = bool(body.get("fresh"))

    try:
        sac_bin = sac_binary()
    except SacBinaryNotFoundError as exc:
        # Resolution-time failure (bug root cause, see _sac_binary.py):
        # surface a structured, diagnosable error instead of building an
        # unresolvable argv that would later die deep inside a subprocess
        # call as an opaque FileNotFoundError / 500. Shape mirrors
        # ``host_exec``'s error responses (``_host_exec.py``).
        return JSONResponse(
            {"name": name, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    # ``agents`` (plural) is the canonical group; ``--yes`` skips the
    # interactive guard (this POST IS the confirmation) and ``--json``
    # gives a parseable envelope — exactly the argv ``agent_restart`` MCP
    # tool + cross-host dispatch already run. Strip the in-SIF env markers
    # so a listen running inside a parent SIF doesn't re-broker the child
    # restart back to itself (same recursion guard as ``agents_start``).
    child_env = dict(os.environ)
    child_env.pop("APPTAINER_CONTAINER", None)
    child_env.pop("SINGULARITY_CONTAINER", None)

    # SELF-RESTART (resolved caller IS the target): a synchronous
    # ``sac agents restart <self>`` DEADLOCKS — the stop-half cannot complete
    # while the CALLING process is still blocked awaiting THIS response, so the
    # start-half sees "already running -> no-op" and returns a confusing 502
    # (incident 2026-07-12: scitex-dev self-restarting to reload a corrected
    # dependency). Hand the bounce to a fully-detached, deferred child that
    # (a) survives the caller's death, (b) sleeps so THIS 202 flushes + the
    # caller's tool-call unwinds FIRST, then (c) force-bounces — and return 202
    # immediately. ``caller`` is the already-resolved identity (an unspoofable
    # per-node bearer, else the body ``caller`` claim); an external / admin
    # restart (caller is None, OR caller != name) is UNAFFECTED and keeps the
    # byte-identical synchronous path below.
    if caller is not None and caller == name:
        from .._lifecycle._session_reset import _runtime_state_dir

        log_path = _runtime_state_dir(name) / "self-restart.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:  # stx-allow: fallback (reason: the log dir is best-effort — a failed mkdir must not abort the heal; the bounce still fires and its own shell redirect surfaces any write failure)
            pass
        detached_argv = _build_detached_restart_argv(
            sac_bin,
            name,
            fresh=fresh,
            delay_s=_SELF_RESTART_DELAY_S,
            log_path=str(log_path),
        )
        try:
            _spawn_detached(detached_argv, env=child_env)
        except OSError as exc:
            # Launch-time failure of the detached bouncer itself — surface it
            # structured (never a fake "scheduled"), same shape as the
            # resolution-/launch-failure branches on the synchronous path.
            return JSONResponse(
                {"name": name, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(
            {
                "name": name,
                "self_restart": "scheduled",
                "fresh": fresh,
                "detail": (
                    f"self-restart of {name!r} scheduled (detached, "
                    f"~{_SELF_RESTART_DELAY_S}s); this process will be bounced "
                    f"shortly. The call returned cleanly; the bounce happens "
                    f"after."
                ),
                "log": str(log_path),
            },
            status_code=202,
        )

    if fresh:
        # New session, no resume: stop-then-start fresh. ``start`` accepts
        # --force (stop if running), --fresh (never --continue) and --json.
        inner_argv = [sac_bin, "agents", "start", name, "--force", "--fresh", "--json"]
    else:
        inner_argv = [sac_bin, "agents", "restart", name, "--yes", "--json"]

    try:
        proc = await asyncio.create_subprocess_exec(
            *inner_argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except OSError as exc:
        # Launch-time failure (e.g. the resolved sac_bin vanished between
        # resolution and exec, or any other subprocess-creation error).
        # Never let this propagate as an unhandled exception → opaque
        # framework 500; surface it structured, same shape as the
        # resolution-failure branch above / host_exec's error responses.
        return JSONResponse(
            {"name": name, "error": f"{type(exc).__name__}: {exc}"},
            status_code=500,
        )
    out, err = await proc.communicate()
    stdout = out.decode("utf-8", errors="replace") if out else ""
    stderr = err.decode("utf-8", errors="replace") if err else ""
    returncode = proc.returncode if proc.returncode is not None else -1

    payload = {
        "name": name,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if returncode != 0:
        # The gate passed but the bare-host restart itself failed (e.g.
        # the agent has no registry row AND no resolvable spec). Surface
        # it as 502 with the rc + stderr — never a fake success.
        return JSONResponse(payload, status_code=502)
    return JSONResponse(payload, status_code=200)
