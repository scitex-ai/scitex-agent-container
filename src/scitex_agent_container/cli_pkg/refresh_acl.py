"""``sac agents refresh-acl`` — re-publish every fleet agent's ACL /
group policy FROM ITS CURRENT SPEC, with NO agent relaunch.

Why this exists
---------------
Each agent's named group + comms policy is persisted into the
``node_comms_policy`` DB table at *its own* agent-start, by
:func:`scitex_agent_container._lifecycle._spawn_gate.persist_acl_policy`.
The ACL reads those persisted values. So when the group model changes
(a new group resolver, or edited ``groups:`` labels in specs), the
change does NOT take effect for already-running agents until each one
restarts — even after the listen server restarts. Relaunching the whole
fleet is heavy (loses sessions, burns API credit).

This command is the lightweight activation step: it re-resolves and
re-writes every fleet agent's group policy from the authoritative
on-disk spec, with no agent relaunch. The operator's post-restart
sequence is::

    systemctl --user restart sac-listen && sac agents refresh-acl

Behaviour
---------
* Enumerates every fleet agent spec by globbing the USER-SCOPE fleet
  registry directly: ``~/.scitex/agent-container/agents/*/spec.yaml``
  (skipping dirs starting with ``_`` — ``_shared``, ``_template_*``).
  It does NOT route through the cwd-sensitive resolver — the glob is
  deterministic regardless of cwd.
* For each spec: reads the CURRENTLY persisted group SET, then
  ``load_config`` + ``persist_acl_policy`` re-resolves + re-writes the
  policy, then reads the NEW set and prints a per-agent diff. The set
  (not just the primary group) is what the diff shows, because the
  AUTHORITY gates read the set — an agent listing several groups was
  reduced to its first one before 2026-08-10, and this command is how a
  row written by the old code becomes correct without a relaunch.
* Idempotent + safe — it only refreshes the DB from on-disk specs;
  running it twice is a no-op on the second run.
* ``--dry-run`` shows the diff WITHOUT writing (the new group is
  previewed with the pure :func:`group_from_labels` resolver).
* Fails loud (non-zero exit) if the registry dir is missing or any spec
  fails to load — but keeps going for the rest, collecting + reporting
  every error at the end.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from ._helpers import console

# Env override for the user-scope fleet registry dir. Mirrors the
# ``SCITEX_AGENT_CONTAINER_STATE_DB`` override used by the state DB: it
# lets the command be pointed at an isolated on-disk registry (tests /
# a non-default install root) without touching the production default.
_REGISTRY_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


def _fleet_registry_dir() -> Path:
    """Return the user-scope fleet registry dir (``…/agents``).

    Honours the ``SCITEX_AGENT_CONTAINER_AGENTS_DIR`` env override; falls
    back to the canonical ``~/.scitex/agent-container/agents``. Globbed
    directly (NOT via the cwd-sensitive config resolver) so the command
    is deterministic regardless of the caller's cwd.
    """
    override = os.environ.get(_REGISTRY_ENV)
    if override:
        return Path(override).expanduser()
    return Path("~/.scitex/agent-container/agents").expanduser()


def _fleet_spec_paths(registry: Path) -> list[Path]:
    """Return every ``<name>/spec.yaml`` under ``registry``.

    Skips registry subdirs whose name starts with ``_`` (``_shared``,
    ``_template_*``) — those are scaffolding, not real fleet agents.
    """
    out: list[Path] = []
    for spec in sorted(registry.glob("*/spec.yaml")):
        if spec.parent.name.startswith("_"):
            continue
        out.append(spec)
    return out


@click.command(name="refresh-acl")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show the group diff WITHOUT writing to the DB.",
)
def refresh_acl(dry_run: bool) -> None:
    """Re-publish every fleet agent's ACL / group policy from its spec.

    Re-resolves each agent's NAMED group from its authoritative on-disk
    ``spec.yaml`` and re-writes ``node_comms_policy`` — WITHOUT relaunching
    any agent. Run it after a group-model change (e.g. ``systemctl --user
    restart sac-listen``) to bring the new group mesh live with no fleet
    relaunch.

    \b
    Example:
      $ sac agents refresh-acl
      $ sac agents refresh-acl --dry-run
    """
    from .._lifecycle._spawn_gate import persist_acl_policy
    from .._state.state_db_nodes import read_comms_policy
    from ..config import load_config
    from ..config._group_resolver import all_named_groups

    def _groups_of(name: str) -> tuple[str, ...]:
        """The persisted AUTHORITY set for ``name`` (sorted, for display)."""
        policy = read_comms_policy(name=name)
        out = {g for g in policy["group_names"] if g}
        if policy["group_name"]:
            out.add(policy["group_name"])
        return tuple(sorted(out))

    registry = _fleet_registry_dir()
    if not registry.is_dir():
        raise click.ClickException(
            f"Fleet registry dir not found: {registry}. Expected the "
            "user-scope agents registry at "
            "~/.scitex/agent-container/agents/. Nothing to refresh."
        )

    specs = _fleet_spec_paths(registry)
    if not specs:
        console.print(
            f"[yellow]No fleet agent specs under {registry} "
            "(only _shared/_template dirs, or empty). Nothing to "
            "refresh.[/yellow]"
        )
        return

    refreshed = 0
    changed = 0
    errors: list[tuple[Path, str]] = []

    for spec in specs:
        # stx-allow: fallback (reason: a single malformed/foreign spec.yaml
        # must NOT abort the rest of the fleet refresh — collect the error,
        # report it at the end, and exit non-zero. See errors[] below.)
        try:
            config = load_config(spec)
        except Exception as exc:  # stx-allow: fallback (reason: see above)
            errors.append((spec, str(exc)))
            console.print(f"[red]FAILED[/red] {spec}: {exc}")
            continue

        name = config.name
        old_groups = _groups_of(name)

        if dry_run:
            new_groups = tuple(
                sorted(all_named_groups(getattr(config, "labels", None)))
            )
        else:
            persist_acl_policy(config)
            new_groups = _groups_of(name)

        refreshed += 1
        old_disp = ", ".join(old_groups) or "(none)"
        new_disp = ", ".join(new_groups) or "(none)"
        if old_groups == new_groups:
            console.print(f"{name}: {old_disp} (unchanged)")
        else:
            changed += 1
            console.print(f"{name}: {old_disp} -> {new_disp}")

    mode = "would refresh" if dry_run else "refreshed"
    console.print(
        f"\n[bold]{refreshed} {mode}, {changed} changed"
        f"{', ' + str(len(errors)) + ' failed' if errors else ''}.[/bold]"
    )
    if errors:
        console.print(
            f"[red]{len(errors)} spec(s) failed to load — "
            "fix the named file(s) above and re-run.[/red]"
        )
        raise SystemExit(1)


__all__ = ["refresh_acl"]
