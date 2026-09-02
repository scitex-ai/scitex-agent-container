#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reserved agent names — slots sac's own machinery already occupies.

:data:`~scitex_agent_container.runtimes._fleet_env.HOST_PROCESS_AGENT_NAME`
is the agent-name slot sac's host-side process uses in the per-agent
PostgreSQL role scheme: :func:`..runtimes._pg_identity_env.derive_pg_role`
composes ``<host_user>__<name>`` for agents and for the host process alike,
so an AGENT with that name would authenticate with a role byte-identical to
the host process's own — silently collapsing the per-agent audit trail into
the host role and inheriting every grant made to it. The same string is
also the ``spawned_by`` lineage sentinel recorded for bare CLI/operator
launches (``_lifecycle/_instances.py``), so the agent's lineage rows would
be indistinguishable from operator activity — the same slot, twice over.

Until 2026-08-28 the reservation existed only as a comment beside the
constant; nothing refused the name. This module is the enforcement, called
from both chokepoints an agent name enters the system through:

  * CREATION — ``sac agents create`` (``cli_pkg/_create.py``), right after
    the charset check, before anything is written;
  * SPEC VALIDATION — :func:`..config._validation.validate_raw`, which both
    ``sac agents check`` / ``validate_config`` and every ``load_config``
    run, so a hand-written or host-broker-materialised
    ``agents/<reserved>/spec.yaml`` is refused before it can start.

The import from ``runtimes._fleet_env`` is lazy on purpose: ``config`` must
not pull the ``runtimes`` package at import time (``runtimes.base`` imports
``config`` back — a module-level import here would re-enter a partially
initialised package).
"""

from __future__ import annotations

from pathlib import Path


def reserved_agent_name_error(name: str) -> str | None:
    """Return the refusal message when ``name`` is reserved, else ``None``.

    The message names the offending value, the collision (why the slot is
    reserved), and the next step — an error the operator can act on.
    """
    from ..runtimes._fleet_env import HOST_PROCESS_AGENT_NAME
    from ..runtimes._pg_identity_env import derive_pg_role

    if name != HOST_PROCESS_AGENT_NAME:
        return None
    return (
        f"Agent name {name!r} is reserved for sac's own host-side process: "
        f"the per-agent Postgres role scheme would give this agent "
        f"PGUSER={derive_pg_role(name)!r} — byte-identical to the role the "
        f"host process authenticates with (HOST_PROCESS_AGENT_NAME in "
        f"runtimes/_fleet_env.py) — collapsing the per-agent audit trail "
        f"and inheriting the host role's grants. {name!r} is also the "
        f"spawned_by lineage sentinel for bare CLI launches "
        f"(_lifecycle/_instances.py) — the same slot again. "
        f"Pick a different name."
    )


def reserved_spec_path_errors(path: str) -> list[str]:
    """dir-as-SSoT adapter for ``validate_raw``.

    The agent name IS the spec's parent-directory name
    (:func:`.._loaders._name_from_path`), so a spec at
    ``.../<reserved>/spec.yaml`` declares the reserved name by position
    alone — the YAML content never carries it.
    """
    msg = reserved_agent_name_error(Path(path).parent.name)
    return [] if msg is None else [msg]
