"""Agent start route for ``sac listen`` (extracted from server.py).

Hosts the agent-start surface — POST /agents → :func:`agents_start` — which
shells the canonical ``sac agents start`` for the named agent, records spawn
lineage, and probes post-ack liveness. The credential-sensitive boot window is
serialized through :mod:`._credential_refresh_lock` so concurrent brokered
background spawns never race the shared OAuth token rotation.

The sibling ``POST /agents/<name>/send`` surface (claude-binary resolver + SSE
stream) lives in :mod:`._agent_exec_send`; the post-ack liveness probe lives in
:mod:`._agent_exec_liveness`. Both are re-imported here so server.py's
historical ``from ._agent_exec import agent_send, _find_claude_binary``
import path keeps working unchanged.
"""

from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import JSONResponse

from .._sac_binary import SacBinaryNotFoundError, sac_binary
from ._acl import check_spawn, deny_response
from ._agent_exec_liveness import (
    _POST_ACK_LIVENESS_TIMEOUT_S,
    _probe_post_ack_liveness,
)
from ._agent_exec_send import _find_claude_binary, agent_send
from ._inline_spec import materialize_inline_spec

__all__ = [
    "_find_claude_binary",
    "agent_send",
    "agents_start",
]


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

    # PR-α (lead msg d96a468c 2026-06-06): cohort one-shot diagnostic.
    # When the parent's ``sac agents start`` carried --foreground /
    # --one-shot, the spawn client (_lifecycle/_spawn_client.py) emits
    # these as body fields. Propagate to the inner argv so the host's
    # apptainer runtime takes the foreground/one-shot branch
    # (subprocess.run blocks until the capsule exits) — the capsule's
    # actual exit code + stderr then flow up into STARTUP_FAILED on
    # crash, instead of the background branch's "Popen + rc=0
    # immediately" lie. Each flag is independent + back-compat absent
    # (default False → no flag, pre-α behaviour).
    foreground = body.get("foreground", False)
    if not isinstance(foreground, bool):
        return JSONResponse(
            {"error": "'foreground' must be a boolean if present"},
            status_code=400,
        )
    one_shot = body.get("one_shot", False)
    if not isinstance(one_shot, bool):
        return JSONResponse(
            {"error": "'one_shot' must be a boolean if present"},
            status_code=400,
        )
    # Consent-propagation bug fix (2026-07-05, reported by
    # paper-scitex-clew): the ``sac agents start <name>`` subprocess this
    # handler shells below re-runs the SAME interactive
    # refuse-without-``--yes`` gate (``cli_pkg/lifecycle/_start_single.py::
    # should_preview_and_require_yes``) that the ORIGINAL in-SIF caller's
    # own ``-y`` already satisfied. Without this field, that consent never
    # reached this subprocess and every brokered start refused itself with
    # "refusing to start ... without --yes/-y" even though ``-y`` was
    # explicitly passed at the top of the call chain. Threaded through to
    # the inner argv (``--yes``) AND the child env
    # (``SAC_ASSUME_YES=1`` below) — belt-and-suspenders, since the env
    # var is also the documented escape valve for callers that can't
    # thread ``assume_yes`` through every layer. FAIL-LOUD invariant
    # preserved: an ABSENT field means no consent was given, so the
    # subprocess still hits the human-at-a-TTY default-refuse gate
    # exactly as before this fix.
    assume_yes = body.get("assume_yes", False)
    if not isinstance(assume_yes, bool):
        return JSONResponse(
            {"error": "'assume_yes' must be a boolean if present"},
            status_code=400,
        )
    # Silent-degradation fix (incident 2026-07-12, scitex-storage). An
    # in-SIF RESTART reaches ``agent_start(force=True)``, which brokers
    # here — and the broker used to DROP the force. This handler then
    # shelled a plain ``sac agents start <name>``, which saw the agent
    # already running, took the idempotent no-op branch, printed
    # "SUCC: <name> started" and exited 0. The caller was told the agent
    # had been restarted while NOTHING cycled: same pid, same stale
    # credentials. Honouring the field makes the brokered restart actually
    # tear the old runtime down. FAIL-LOUD invariant preserved: an ABSENT
    # field means no force was requested, so an ordinary brokered start
    # keeps its idempotent behaviour exactly as before.
    force = body.get("force", False)
    if not isinstance(force, bool):
        return JSONResponse(
            {"error": "'force' must be a boolean if present"},
            status_code=400,
        )
    profile = body.get("profile")
    if profile is not None and (
        not isinstance(profile, str) or not profile.strip()
    ):
        return JSONResponse(
            {"error": "'profile' must be a non-empty string if present"},
            status_code=400,
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
    # Consent-propagation fix (2026-07-05, paper-scitex-clew report): set
    # the env-var escape valve in ADDITION to the --yes flag below so the
    # inner subprocess's refuse-without-``--yes`` gate
    # (``should_preview_and_require_yes`` in cli_pkg/lifecycle/
    # _start_single.py) sees consent even if some intermediate wrapper
    # strips CLI flags. Absent when assume_yes is False — the default
    # refuse-without-consent behaviour is completely unchanged.
    if assume_yes:
        child_env["SAC_ASSUME_YES"] = "1"
    # PR-α: propagate --foreground / --one-shot to the inner argv per the
    # body fields validated above. Order matches the click signature on
    # `sac agents start` (flags before/after positional name are
    # equivalent, but keep the positional last for the canonical shape).
    inner_argv = [sac_bin, "agents", "start"]
    if foreground:
        inner_argv.append("--foreground")
    if one_shot:
        inner_argv.append("--one-shot")
    if assume_yes:
        inner_argv.append("--yes")
    # See the ``force`` validation above: without this the brokered restart
    # silently degraded into an idempotent no-op that still reported SUCC.
    if force:
        inner_argv.append("--force")
    if profile:
        inner_argv.extend(["--profile", profile])
    inner_argv.append(name)
    # Single-flight the OAuth-refresh boot window (card
    # sac-multi-start-queue-oauth): concurrent brokered background spawns share
    # ~/.claude/.credentials.json and would race the in-container token
    # rotation -> the loser's freshly-rotated token is clobbered -> 401 ->
    # mass re-login. run_brokered_launch serializes background launches through
    # a blocking flock + bounded refresh-settle; foreground / one-shot bypass
    # it (single, interactive, long-lived — a fleet-wide lock held that long
    # would serialize everything for no safety gain).
    from ._credential_refresh_lock import run_brokered_launch

    try:
        proc = await run_brokered_launch(
            inner_argv,
            child_env,
            foreground=bool(foreground),
            one_shot=bool(one_shot),
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

        from .._lifecycle._start_decline import start_was_declined

        return JSONResponse(
            {
                "name": name,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "declined": start_was_declined(proc.stdout, proc.stderr),
            },
            status_code=502,
        )

    # Layer-3 fail-loud (clew dogfood repro 2026-06-06, lead msg
    # 57f1632a): `sac agents start <child>` returning rc=0 was being
    # treated as "instance is running and healthy", but the apptainer
    # exec the child spawned can die SILENTLY post-ack — empty
    # stdout.log, dead apptainer_pid, no fresh STARTUP_FAILED marker.
    # The "SUCC: started" body of this 200 response lied.
    #
    # Probe the runtime_dir for liveness: wait up to
    # ``_POST_ACK_LIVENESS_TIMEOUT_S`` for ``apptainer_pid`` to appear
    # AND for that pid to still be alive (``kill -0``). Three loud
    # failure modes:
    #
    #   * apptainer_pid never appears within the grace → the child
    #     subprocess returned 0 without invoking the apptainer
    #     runtime path (broken wrapper, missing config, etc.).
    #     kind = "post_ack_no_apptainer_pid"
    #   * apptainer_pid appears but the pid is dead by the time we
    #     check → the SIF instance came up and immediately died.
    #     kind = "post_ack_apptainer_pid_dead"
    #   * apptainer_pid was alive at grace-deadline → return 200/SUCC
    #     (the existing happy path).
    #
    # Loud failures write a fresh STARTUP_FAILED with the new kind
    # AND downgrade the response to 502 so the operator-side recv
    # path can show a real diagnostic instead of a misleading SUCC.
    #
    # PR-α: skip the probe when foreground=True. The inner subprocess
    # already blocked (apptainer runtime's foreground branch is
    # subprocess.run, not Popen) and explicitly does NOT write
    # apptainer_pid — the probe would always report
    # post_ack_no_apptainer_pid on a successful one-shot run. The real
    # signal is the inner rc captured above; rc!=0 already wrote
    # STARTUP_FAILED with stderr_tail (the cohort one-shot diagnostic).
    if foreground:
        return JSONResponse(
            {
                "name": name,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
            },
            status_code=200,
        )
    from .._lifecycle._startup_failed import write_marker
    from .._runners._session_state import state_dir_for

    runtime_dir = state_dir_for(name)
    # Test escape hatch: ``SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S`` env
    # var lets the suite skip / shorten the probe (≤0 → skip entirely).
    # Production callers leave it unset and get the default grace window.
    try:
        env_timeout = float(
            os.environ.get(
                "SAC_LISTEN_POST_ACK_LIVENESS_TIMEOUT_S",
                str(_POST_ACK_LIVENESS_TIMEOUT_S),
            )
        )
    except ValueError:
        env_timeout = _POST_ACK_LIVENESS_TIMEOUT_S
    # Passing ``name`` is load-bearing: it lets the probe pick the check that is
    # VALID for this agent's runtime. Without it, the probe waits for an
    # ``apptainer_pid`` file that a ``tui`` agent — the fleet's DEFAULT runtime —
    # never writes, and then stamps ``startup_failed`` on a perfectly healthy
    # agent. See :mod:`._agent_exec_liveness`.
    liveness_failure = _probe_post_ack_liveness(
        runtime_dir,
        name=name,
        timeout_s=env_timeout,
    )
    if liveness_failure is not None:
        kind, hint = liveness_failure
        # stx-allow: fallback (reason: marker write is observability;
        # a write failure here must not shadow the underlying
        # post-ack-died signal that the listen is about to return)
        try:
            write_marker(
                runtime_dir,
                started_at=started_at,
                phase="post_ack_liveness",
                exit_code=0,
                stdout=proc.stdout or "",
                stderr=(proc.stderr or "")
                + f"\n\n[listen post-ack liveness probe] {kind}: {hint}\n",
                kind_override=kind,
            )
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            pass
        return JSONResponse(
            {
                "name": name,
                "returncode": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "post_ack_liveness": {"kind": kind, "hint": hint},
            },
            status_code=502,
        )

    return JSONResponse(
        {
            "name": name,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        },
        status_code=200,
    )
