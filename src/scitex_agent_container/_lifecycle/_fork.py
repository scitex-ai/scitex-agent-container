"""Task-scoped FORKs: pure spec derivation for ``sac agents fork``.

A fork is a task-scoped child of a PARENT agent. Unlike a twin (which
forks the parent's *live session* for parallel/divergent work), a fork is
born for ONE assignment: it inherits the parent's repo / workdir / image /
binds / model verbatim, gets its own name + overlay + a2a port + session,
and carries the identity contract in its boot-kick (author = fork, card
ownership = parent).

This module is the shared front-half of BOTH ``sac agents fork`` and the
``agent_fork`` MCP tool: :func:`prepare_fork_spawn` resolves the parent
spec + fork name and returns ``(resolved_name, doc)`` ready to POST via
:func:`_spawn_client.request_spawn`. All functions here are PURE (no
spawns, no side effects) so they are unit-testable without a fleet.

Fail-loud :class:`ForkSeedError` on an unresolvable parent, a malformed
spec, a missing task, or an explicitly-taken name.
"""

from __future__ import annotations

import copy
from typing import Any, Iterable

from ..config import resolve_config
from ..config._resolve import enumerate_agent_names
from ._twin import (
    _TELEGRAMMER_CHANNEL,
    CARDS_AGENT_ENV,
    RETIRED_AGENT_ENV,
    SELF_NAME_ENV,
    TWIN_PARENT_ENV,
    _resolve_parent_to_home,
)

__all__ = [
    "ForkSeedError",
    "build_fork_boot_kick",
    "derive_fork_spec",
    "prepare_fork_spawn",
    "resolve_fork_name",
]


class ForkSeedError(RuntimeError):
    """The fork cannot be seeded: bad parent, bad name, or bad spec."""


def resolve_fork_name(
    parent_name: str,
    requested: str | None,
    existing: Iterable[str] | None,
) -> str:
    """Return the fork's agent name.

    An explicit ``requested`` name is returned verbatim (the caller
    decides whether a clash is an error). Otherwise the default
    ``<parent>-fork`` is used, bumped to ``<parent>-fork-2`` / ``-3`` /
    ... on the first free suffix so a parent can carry several live
    forks at once.
    """
    if requested:
        return requested
    taken = {str(n) for n in (existing or ())}
    base = f"{parent_name}-fork"
    if base not in taken:
        return base
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return f"{base}-{n}"


def build_fork_boot_kick(fork_name: str, parent_name: str, task: str) -> str:
    """First user message fed to the fork at boot.

    Carries the two things a freshly-spawned fork must know: its bounded
    assignment, and the IDENTITY-SPLIT rule (author = fork, owner =
    parent). The ownership rule is stated HERE — not only in the skill —
    because scitex-cards cannot enforce owner=parent from env, so the
    boot-kick is the deterministic delivery of the hard rule.
    """
    return "\n".join(
        [
            f"You are {fork_name}, a task-scoped FORK of {parent_name}.",
            f"Your assignment: {task.strip()}",
            "",
            "IDENTITY CONTRACT (hard rule — do not deviate):",
            f"  - Your scitex-cards writes are attributed to YOU ({fork_name}); "
            "that is intended.",
            f"  - But card OWNERSHIP must stay with {parent_name}. On EVERY "
            f"add_task / reassign, pass assignee={parent_name} (also available "
            f"as $SAC_FORK_PARENT). NEVER leave a card owned by {fork_name}: "
            "if you exit, a card you own lands in an inbox nobody drains.",
            f"  - Report evidence, results and blockers back to {parent_name} "
            "via a2a or a shared card owned by the parent.",
            "  - Do not use Telegram and do not create further agents.",
        ]
    )


def derive_fork_spec(
    parent_doc: dict[str, Any],
    *,
    fork_name: str,
    parent_name: str,
    persist: bool,
    role: str | None = None,
    task: str = "",
    to_home: str | None = None,
) -> dict[str, Any]:
    """Return the fork's inline spec document derived from the parent's.

    Pure — deep-copies ``parent_doc`` and overrides only what a fork must
    change, inheriting repo / workdir / image / binds / model verbatim:

      * ``spec.env`` — ``SCITEX_CARDS_AGENT_ID = <fork>`` (author = fork),
        ``SAC_FORK_PARENT = <parent>`` (owner-convention value); any
        inherited ``SAC_NAME`` is dropped (``listen_env_flags`` injects it
        from the fork's own name), as is any inherited
        ``SCITEX_TODO_AGENT_ID`` (retired, and carrying the PARENT's name).
      * ``spec.restart.policy`` — ``always`` when ``persist`` else
        ``never`` (ephemeral default: a stopped fork does not come back —
        the triplet rule removes all three when the task is done).
      * ``spec.a2a.port = "auto"`` — a fresh sidecar port, never the
        parent's (a pinned inherited port would collide).
      * telegrammer channel dropped — two agents must not fight one bot's
        getUpdates slot; the fork stays reachable via ``server:sac``.
      * ``spec.startup_prompts`` — replaced with the fork boot-kick (the
        identity contract + the bounded task).
      * ``metadata.labels.role`` — ``role`` when given, else
        ``domain-subagent``; ``metadata.labels.parent`` records lineage.
      * ``spec.extensions.fork`` — ``{parent, task}`` marker for
        operators auditing fork lineage.

    The fork's NAME is NOT written into the document: the host
    materialises the inline spec at ``agents/<fork_name>/spec.yaml`` and
    the loader derives the name from that directory (dir-as-SSoT).
    """
    if not task or not task.strip():
        raise ForkSeedError("a fork needs a bounded task; refusing a task-less fork.")
    doc = copy.deepcopy(parent_doc)
    if not isinstance(doc, dict):
        raise ForkSeedError(
            f"parent spec of {parent_name!r} did not parse to a mapping "
            f"(got {type(parent_doc).__name__!r}); cannot derive a fork."
        )
    spec = doc.setdefault("spec", {})
    if not isinstance(spec, dict):
        raise ForkSeedError(
            f"parent spec of {parent_name!r} has a non-mapping 'spec' block; "
            "cannot derive a fork."
        )

    claude = spec.setdefault("claude", {})
    if isinstance(claude, dict):
        claude["session"] = "continue"
        claude["resume_id"] = ""
        channels = claude.get("channels")
        if isinstance(channels, list):
            claude["channels"] = [
                c for c in channels if str(c).strip() != _TELEGRAMMER_CHANNEL
            ]
    comms = spec.get("comms")
    if isinstance(comms, dict):
        channels = comms.get("channels")
        if isinstance(channels, list):
            comms["channels"] = [
                c for c in channels if str(c).strip() != _TELEGRAMMER_CHANNEL
            ]

    env = spec.setdefault("env", {})
    if isinstance(env, dict):
        env[CARDS_AGENT_ENV] = fork_name
        env[TWIN_PARENT_ENV] = parent_name
        env.pop(SELF_NAME_ENV, None)
        env.pop(RETIRED_AGENT_ENV, None)

    restart = spec.setdefault("restart", {})
    if isinstance(restart, dict):
        restart["policy"] = "always" if persist else "never"

    a2a = spec.setdefault("a2a", {})
    if isinstance(a2a, dict):
        a2a["port"] = "auto"

    # Reuse the PARENT's to_home tree (skills / hooks / .mcp.json) verbatim
    # via its absolute host path, so the fork has the SAME capabilities
    # and MCP wiring as the parent. Per-agent identity in those files is
    # runtime-only and expands from the fork's OWN container env at boot.
    if to_home:
        spec["to_home"] = to_home

    metadata = doc.setdefault("metadata", {})
    if isinstance(metadata, dict):
        labels = metadata.setdefault("labels", {})
        if isinstance(labels, dict):
            labels["role"] = role or "domain-subagent"
            labels["parent"] = parent_name

    extensions = spec.setdefault("extensions", {})
    if isinstance(extensions, dict):
        extensions["fork"] = {"parent": parent_name, "task": task.strip()}

    spec["startup_prompts"] = [build_fork_boot_kick(fork_name, parent_name, task)]
    return doc


def prepare_fork_spawn(
    parent_name: str,
    *,
    fork_name: str | None = None,
    task: str | None = None,
    persist: bool = False,
    role: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Resolve the parent spec + fork name and derive the fork's inline doc.

    The shared front-half of BOTH ``sac agents fork`` and the
    ``agent_fork`` MCP tool: reads the parent's on-disk spec (raw YAML),
    resolves the fork name against the existing fleet (default-bumped),
    and returns ``(resolved_fork_name, fork_spec_doc)`` ready to POST via
    :func:`_spawn_client.request_spawn`.

    Fail-loud (:class:`ForkSeedError`) when the parent spec is
    unresolvable / malformed, when ``task`` is missing, or when an
    EXPLICIT ``fork_name`` is already taken (an operator-chosen name is
    never silently bumped).
    """
    import yaml

    if not task or not task.strip():
        raise ForkSeedError("a fork needs a bounded task (--task is required).")
    try:
        parent_path = resolve_config(parent_name)
    except Exception as exc:  # noqa: BLE001 - re-raised as fail-loud ForkSeedError
        raise ForkSeedError(
            f"cannot resolve parent agent {parent_name!r}: {exc}"
        ) from exc
    with open(parent_path, encoding="utf-8") as fh:
        parent_doc = yaml.safe_load(fh)
    if not isinstance(parent_doc, dict):
        raise ForkSeedError(
            f"parent spec {parent_path!r} did not parse to a YAML mapping."
        )

    try:
        existing = enumerate_agent_names()
    except Exception:  # stx-allow: fallback (reason: name enumeration is best-effort; a 409 on spawn is the backstop)
        existing = []
    if fork_name and fork_name in set(existing):
        raise ForkSeedError(
            f"fork name {fork_name!r} is already taken; pick another name or "
            "omit it to auto-bump <parent>-fork-N."
        )
    resolved_name = resolve_fork_name(parent_name, fork_name, existing)

    to_home = _resolve_parent_to_home(parent_path, parent_doc)
    doc = derive_fork_spec(
        parent_doc,
        fork_name=resolved_name,
        parent_name=parent_name,
        persist=persist,
        role=role,
        task=task,
        to_home=to_home,
    )
    return resolved_name, doc
