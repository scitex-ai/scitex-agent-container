"""``agent_twin`` — the context-inheriting fork tool (Python API + MCP).

Extracted from :mod:`._agent`, which holds the thin ``invoke_cli_*``
wrappers over ``sac agents <verb>``. ``agent_twin`` is the one tool in that
group that does real work of its own: it BUILDS a twin spec through
:func:`scitex_agent_container._lifecycle._twin.prepare_twin_spawn` and
brokers it to the host listen daemon through
:func:`scitex_agent_container._lifecycle._spawn_client.request_spawn`.

``_agent`` re-exports the name, so ``_mcp._tools._agent.agent_twin`` and the
``register_agent_tools`` registration loop keep working unchanged.
"""

from __future__ import annotations

from typing import Any


def agent_twin(
    parent: str,
    name: str | None = None,
    task: str | None = None,
    persist: bool = False,
    role: str | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """Spawn a context-inheriting TWIN of a running agent (e.g. your own).

    A TWIN forks PARENT's live session — inherits its transcript at birth
    then diverges; PARENT is never touched. Same host-broker path as
    ``agent_spawn``; repo/workdir/image/binds/model inherited verbatim; own
    name + fresh a2a port + ``session: continue``; host seeds the twin's
    session from the parent's transcript at first boot. (Use one to inherit context
    without sharing future context, split parallel work, or run heavy work
    off your main loop; a plain Task subagent is cheaper otherwise.)

    IDENTITY CONTRACT (safety-critical; the twin's boot-kick repeats it):
    AUTHOR = twin (``SCITEX_CARDS_AGENT_ID`` = twin — its scitex-cards writes
    attribute to it). OWNER = parent, but scitex-cards cannot default the card
    owner from env, so the twin MUST pass ``assignee=<parent>`` (==
    ``$SAC_TWIN_PARENT``) on every card write — a hard rule, not an env
    guarantee; an ephemeral twin that owns cards then exits orphans them.

    ``name`` defaults to ``<parent>-twin`` (bumped if taken); ``persist``
    makes it long-lived (default ephemeral); ``task``/``role``/``caller``
    optional. Returns ``{"status":"ok","twin":..,"result":{..}}`` else
    ``{"status":"error","reason":..}``.
    """
    from ..._lifecycle._spawn_client import SpawnRequestError, request_spawn
    from ..._lifecycle._twin import TwinSeedError, prepare_twin_spawn

    try:
        twin_name, doc = prepare_twin_spawn(
            parent, twin_name=name, task=task, persist=persist, role=role
        )
    except TwinSeedError as exc:
        return {"status": "error", "reason": str(exc)}

    try:
        result = request_spawn(twin_name, spec=doc, caller=caller, assume_yes=True)
    except SpawnRequestError as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "http_status": exc.status,
            "body": exc.body,
        }
    return {"status": "ok", "twin": twin_name, "parent": parent, "result": result}


__all__ = ["agent_twin"]
