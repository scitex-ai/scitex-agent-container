#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cross-host dispatch + host-listen bypass for ``sac agents restart``.

Everything here is about reaching a restart that does NOT live in this
process:

  * **Cross-host** — an active ``state.db.instances`` row whose ``host``
    is a peer routes the restart over ssh to that node
    (:func:`_dispatch_remote_restart`).
  * **Host-listen bypass** — an in-SIF agent cannot see the bare host's
    registry, so a "not found in registry" miss is brokered to the host's
    ``sac listen`` instead (:func:`_should_try_host_bypass`,
    :func:`_restart_via_host_bypass`, :func:`_bypass_base_url_available`),
    exactly like the spawn bypass.

Split out of ``_restart.py`` (which had grown past the 512-line limit)
so the orchestrator + CLI surface stays readable. ``_restart.py``
re-exports these names, so existing imports keep resolving.
"""

from __future__ import annotations

import json as _json
import shlex
import subprocess

from ..._state.host_config import build_ssh_argv
from ..._state.state_db import record_instance_start, record_instance_stop

__all__ = [
    "_dispatch_remote_restart",
    "_should_try_host_bypass",
    "_restart_via_host_bypass",
    "_bypass_base_url_available",
]


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


def _should_try_host_bypass(exc: Exception) -> bool:
    """Return True iff a LOCAL restart failure should fall back to the host.

    The fallback fires only when BOTH hold:

      * the failure is the "not found in registry" local-resolution miss
        (an in-SIF agent cannot see a peer's bare-host registry row), and
      * ``SAC_LISTEN_BASE_URL`` is set (we are a container with the host
        listen reachable — the spawn bypass's precondition).

    Any other RuntimeError (a real restart fault on a resolvable agent)
    propagates unchanged so the bare-host operator path is untouched.
    """
    from ..._lifecycle._restart_client import RestartRequestError, _resolve_base_url

    if "not found in registry" not in str(exc):
        return False
    try:
        _resolve_base_url(None)
    except RestartRequestError:
        return False
    return True


def _restart_via_host_bypass(name: str, fresh: bool = False) -> dict:
    """Broker the restart to the HOST listen and return its JSON envelope.

    Mirrors the spawn bypass (``agent_spawn`` → ``request_spawn``): the
    in-SIF client POSTs to ``{SAC_LISTEN_BASE_URL}/agents/<name>/restart``
    and the host runs ``sac agents restart <name> --yes`` (or, when
    ``fresh``, ``sac agents start <name> --force --fresh``) on the bare host
    (manage-gated by ``check_lineage_acl``). A :class:`RestartRequestError`
    (transport / 401 / 403 / 5xx) propagates so the CLI's outer ``except``
    surfaces it fail-loud.
    """
    from ..._lifecycle._restart_client import request_restart

    return request_restart(name, fresh=fresh)


def _bypass_base_url_available() -> bool:
    """True iff a host-listen base URL resolves (we are an in-container agent).

    The fresh-restart path is bypass-only: it has nothing to broker to on a
    bare host, so the CLI fails loud there rather than silently doing a
    resuming restart.
    """
    from ..._lifecycle._restart_client import RestartRequestError, _resolve_base_url

    try:
        _resolve_base_url(None)
    except RestartRequestError:
        return False
    return True
