#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch + host-listen broker for ``sac agents restart``.

Everything here is about reaching a restart that does NOT live in this
process:

  * **Cross-host** — an active ``state.db.instances`` row whose ``host``
    is a peer routes the restart over ssh to that node
    (:func:`_dispatch_remote_restart`).
  * **Host-listen broker** — a ``sac`` running inside an apptainer SIF
    cannot perform a restart at all, so it asks the bare host's
    ``sac listen`` to run it (:func:`must_broker_to_host`,
    :func:`brokered_restart`), exactly like the spawn broker.

Split out of ``_restart.py`` (which had grown past the 512-line limit)
so the orchestrator + CLI surface stays readable. ``_restart.py``
re-exports these names, so existing imports keep resolving.
"""

from __future__ import annotations

import json as _json
import logging
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from ..._state.host_config import build_ssh_argv
from ..._state.state_db import record_instance_start, record_instance_stop

logger = logging.getLogger(__name__)

__all__ = [
    "_dispatch_remote_restart",
    "_restart_via_host_bypass",
    "brokered_restart",
    "log_restart_decision",
    "must_broker_to_host",
]


# One JSONL line per restart decision + outcome. Same convention (and
# same parent directory) as the listen daemon's ``host_exec.log``. The
# operator's standing rule is that a control-plane decision nobody
# recorded is a decision nobody can debug: before this existed, the fact
# that an in-container restart had sent NO request anywhere was written
# down NOWHERE — the host's listen log can only show what ARRIVED, so a
# request that was never made left no trace at all.
#
# Note the path is runtime-root-relative and therefore per-side: a
# brokered restart writes the DECISION line inside the container and the
# host's own CLI writes its LOCAL line on the host. That is intentional —
# each side records what it actually did.
_DECISION_LOG_NAME = "restart_decision.log"


def _decision_log_path() -> Path:
    """Resolve the decision log path AT CALL TIME.

    Deliberately not a module-level constant: ``runtime_base_dir()`` reads
    ``SCITEX_AGENT_CONTAINER_RUNTIME_DIR`` (and ``$HOME``) on every call,
    and a constant computed at import would freeze whichever value was in
    effect when the module first loaded — which is exactly how a test that
    relocates the runtime root ends up writing to the operator's REAL one.
    """
    from ..._runtime_paths import runtime_base_dir

    return runtime_base_dir() / "logs" / _DECISION_LOG_NAME


def log_restart_decision(**entry: Any) -> None:
    """Append one JSONL decision/outcome record. Best-effort, never fatal.

    A failed log write must not turn a working restart into an error, so
    the miss is reported on the module logger and the restart continues.
    """
    entry.setdefault("ts", time.time())
    path = _decision_log_path()
    logger.info("restart decision: %s", entry)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(_json.dumps(entry, sort_keys=True, default=str) + "\n")
    except Exception as exc:  # stx-allow: fallback (best-effort audit log; must never shadow the real restart result)
        logger.warning("restart decision log append failed at %s: %s", path, exc)


def _dispatch_remote_restart(peer: str, row: dict, peers: dict, name: str) -> dict:
    """SSH into ``peer`` and run ``sac agents restart <name> --yes --json``.

    The remote restart closes the agent's old instance row and opens a
    fresh one on the peer. Mirror that on the lead side: close the stale
    lead-side row (``record_instance_stop``) and open a new ``remote``
    row carrying the peer-reported bound port so cross-host listings and
    ``resolve_peer_url`` keep pointing at the right node + port.

    Raises ``RuntimeError`` with the full ssh argv + stderr on failure
    (no-silent-fallback rule). Returns the parsed JSON envelope from the
    peer's stdout.
    """
    ssh_argv = build_ssh_argv(
        peer,
        ["sac", "agents", "restart", name, "--yes", "--json"],
        peers,
    )
    result = subprocess.run(
        ssh_argv,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Remote `sac agents restart {name}` failed on {peer!r} "
            f"(rc={result.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    try:
        envelope = _json.loads(result.stdout)
    except _json.JSONDecodeError as exc:
        raise RuntimeError(
            f"Remote `sac agents restart {name}` on {peer!r} returned "
            f"non-JSON stdout (peer sac may be too old to support "
            f"--json; pull latest on the peer):\n"
            f"stdout (first 500 chars):\n{result.stdout[:500]}\n"
            f"json error: {exc}"
        ) from exc

    # Close the stale lead-side row, then open a fresh remote row so the
    # restarted agent stays addressable cross-host.
    instance_id = row.get("id")
    if instance_id:
        record_instance_stop(str(instance_id), exit_reason="restarted")
    bound = envelope.get("a2a_port") if isinstance(envelope, dict) else None
    record_instance_start(
        name=name,
        host=peer,
        a2a_port=bound,
        bound_port=bound,
        remote=True,
    )
    return envelope if isinstance(envelope, dict) else {}


def must_broker_to_host() -> bool:
    """True iff THIS process cannot perform an agent restart itself.

    ONE question, asked identically by every restart path: *am I inside
    an apptainer SIF?*

    An agent's process, its tmux session, its runtime state dir and the
    registry row naming them all live on the BARE HOST. A ``sac`` running
    inside a SIF has none of them: it gets a private ``state.db``, a
    private tmux server, a private ``$HOME``, and no way to ``apptainer
    exec`` a replacement (nested apptainer is unsupported on the shape
    sac targets). So an in-SIF restart can only be PERFORMED by asking
    the host's ``sac listen`` to run it.

    This is the same rule the START path already uses
    (``_in_sif_broker.maybe_broker_in_sif_spawn``) and the same one the
    host's restart handler assumes when it strips ``APPTAINER_CONTAINER``
    / ``SINGULARITY_CONTAINER`` from the child it shells
    (``_listen/_agent_restart.py``) so the host-side restart cannot
    broker back to itself. Keying on the SIF markers rather than on
    ``SAC_LISTEN_BASE_URL`` is what makes that recursion guard work: the
    listen daemon's own environment carries the base URL and the child
    inherits it, so a base-URL predicate would send the host's restart
    straight back to the host in a loop.

    WHAT THIS REPLACED, AND WHY (P0, 2026-07-20)
    --------------------------------------------

    The plain path used to decide by EXCEPTION: a ``_should_try_host_bypass``
    helper inspected the local restart's error message and brokered only
    when it contained the literal substring ``"not found in registry"``.
    That predicate was unreachable in practice. Local resolution has two
    legs — a registry row OR a resolvable spec file — and specs are
    bind-mounted into every container, so the spec leg succeeded, local
    resolution never raised, the handler was never consulted, and the
    in-container restart proceeded locally where it could not touch the
    host's tmux session. It then printed ``Agent 'x' restarted`` and
    exited 0 having done nothing. The broker fired only for agents that
    did not exist at all.

    Two separate faults, both fixed by asking the question directly:

      * gating CONTROL FLOW on a substring of an error message is
        fragile — rewording the message silently disables the broker; and
      * it INVERTED the logic — the fallback required a failure that the
        silent success prevented.

    ``--fresh`` used a second, different predicate (a bare "does a listen
    base URL resolve"). Both paths now ask this one.

    Fail-loud: when this returns True but the container has no reachable
    listen, :func:`brokered_restart` raises ``RestartRequestError`` from
    the client's own base-URL/transport check. There is deliberately no
    fall-through to a local restart — that local restart is precisely the
    silent no-op being fixed.
    """
    from ..._lifecycle._in_sif_broker import is_in_sif

    return is_in_sif()


def _restart_via_host_bypass(name: str, fresh: bool = False) -> dict:
    """Broker the restart to the HOST listen and return its JSON envelope.

    Mirrors the spawn broker (``agent_spawn`` → ``request_spawn``): the
    in-SIF client POSTs to ``{SAC_LISTEN_BASE_URL}/agents/<name>/restart``
    and the host runs ``sac agents restart <name> --yes`` (or, when
    ``fresh``, ``sac agents start <name> --force --fresh``) on the bare host
    (manage-gated by ``check_lineage_acl``). A :class:`RestartRequestError`
    (missing base URL / transport / 401 / 403 / 5xx) propagates so the
    CLI's outer ``except`` surfaces it fail-loud.
    """
    from ..._lifecycle._restart_client import request_restart

    return request_restart(name, fresh=fresh)


def _parse_host_cli_envelope(stdout: Any) -> dict:
    """Return the host CLI's ``--json`` envelope parsed out of ``stdout``.

    The host runs ``sac agents restart <name> --yes --json``, whose last
    stdout line is the envelope. Scanning from the end (rather than
    parsing the whole buffer) tolerates any banner a future host prints
    ahead of it. Returns ``{}`` when nothing parses — the caller treats
    that as "the host reported no verdict", never as a verdict.
    """
    if not isinstance(stdout, str):
        return {}
    for raw in reversed(stdout.splitlines()):
        line = raw.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = _json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def brokered_restart(name: str, *, fresh: bool = False) -> tuple[dict, bool]:
    """Ask the host to restart ``name``; return ``(envelope, ok)``.

    The verdict is the HOST'S, never one invented here. The host CLI runs
    the same ``sac agents restart`` code path this module belongs to, and
    that path verifies its own postcondition against the agent's real
    ``instance_id`` marker (:mod:`._restart_verify`). Re-deriving the
    verdict on this side is not possible and must not be faked: inside a
    SIF the runtime dir resolves to the container's private ``$HOME``,
    which holds none of the host fleet's state, so a container-side probe
    of a host agent could only ever answer "no evidence".

    Three shapes come back:

      * ``self_restart == "scheduled"`` (HTTP 202) — the caller IS the
        target, so the host handed the bounce to a detached child that
        fires AFTER this response flushes. Nothing has cycled YET and
        nothing can be verified from here; report it as ``scheduled``
        with ``verified: null`` rather than claiming either outcome.
      * a host envelope carrying ``restarted`` / ``verified`` — relayed
        verbatim; an explicit ``False`` on either is a FAILED restart.
      * no parseable envelope — fall back to the process ``returncode``
        alone and say so in ``verified_reason``. ``rc`` is a real signal
        (the host CLI exits 1 on any failed restart) but it is NOT a
        postcondition, so it never yields ``verified: true``.
    """
    envelope = _restart_via_host_bypass(name, fresh=fresh)
    out: dict = {
        "name": name,
        "dispatched": False,
        "via": "host-listen",
        "host_response": envelope,
    }
    if fresh:
        out["fresh"] = True

    if envelope.get("self_restart") == "scheduled":
        out["restarted"] = True
        out["scheduled"] = True
        out["verified"] = None
        out["verified_reason"] = (
            f"self-restart of {name!r} was SCHEDULED on the host (detached, "
            f"deferred) so this process can return before it is bounced; the "
            f"cycle has not happened yet and cannot be verified from here"
        )
        return out, True

    rc = envelope.get("returncode")
    host_cli = _parse_host_cli_envelope(envelope.get("stdout"))
    host_restarted = host_cli.get("restarted")
    out["restarted"] = rc == 0 and host_restarted is not False

    if "verified" in host_cli:
        out["verified"] = host_cli["verified"]
        out["verified_reason"] = host_cli.get("verified_reason")
        out["run_before"] = host_cli.get("run_before")
        out["run_after"] = host_cli.get("run_after")
        if host_cli["verified"] is False:
            out["restarted"] = False
    else:
        out["verified"] = None
        out["verified_reason"] = (
            f"the host returned rc={rc} but no postcondition verdict for "
            f"{name!r} (its sac may predate `verified`); rc alone proves the "
            f"host CLI RAN, not that the agent cycled"
        )

    return out, bool(out["restarted"])
