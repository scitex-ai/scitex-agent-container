"""``agent_fork`` — the task-scoped fork tool (Python API + MCP).

Mirrors :mod:`._agent_twin`: BUILDS a fork spec through
:func:`scitex_agent_container._lifecycle._fork.prepare_fork_spawn` and
brokers it to the host listen daemon through
:func:`scitex_agent_container._lifecycle._spawn_client.request_spawn`.
"""

from __future__ import annotations

from typing import Any


def agent_fork(
    parent: str,
    name: str | None = None,
    task: str | None = None,
    persist: bool = False,
    role: str | None = None,
    caller: str | None = None,
    fresh: bool = False,
    handover: str | None = None,
) -> dict[str, Any]:
    """Spawn a task-scoped FORK of a running agent (e.g. your own).

    A FORK is born for ONE assignment: it inherits PARENT's session at
    birth (unless ``fresh``) then diverges; PARENT is never touched.
    Repo/workdir/image/binds/model inherited verbatim; own name + fresh
    a2a port + identity-split env (author = fork, card owner = parent).

    ``handover`` is a direct message (伝言) prepended to the fork's
    boot-kick — newest context first. ``fresh`` starts a clean session
    (0 inherited exchanges). ``name`` defaults to ``<parent>-fork``
    (bumped if taken); ``persist`` makes it long-lived (default
    ephemeral — remove all three when the triplet's task is done).

    Returns ``{"status":"ok","fork":..,"result\":{..}}`` else
    ``{"status":"error","reason":..}``.
    """
    from ..._lifecycle._fork import ForkSeedError, prepare_fork_spawn
    from ..._lifecycle._spawn_client import SpawnRequestError, request_spawn

    try:
        fork_name, doc = prepare_fork_spawn(
            parent,
            fork_name=name,
            task=task,
            persist=persist,
            role=role,
            fresh=fresh,
            handover=handover,
        )
    except ForkSeedError as exc:
        return {"status": "error", "reason": str(exc)}

    try:
        result = request_spawn(fork_name, spec=doc, caller=caller, assume_yes=True)
    except SpawnRequestError as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "http_status": exc.status,
            "body": exc.body,
        }
    return {"status": "ok", "fork": fork_name, "parent": parent, "result": result}


__all__ = ["agent_fork"]
