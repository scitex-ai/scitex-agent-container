#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared bulk-selection surface for the destructive fleet verbs.

``sac agents restart`` and ``sac agents stop`` are the two verbs an
operator reaches for during an incident, and they MUST offer the same
selection surface — the operator's live terminal (2026-07-13)::

    $ sac agents stop --all
    Error: No such option: --all
    $ sac agents stop
    Error: Missing argument 'TARGETS...'.

``restart`` had bulk selection; ``stop`` had none, so there was no way to
bring the fleet down. That asymmetry is the bug this module removes: both
verbs now import the SAME flags, the SAME enumeration, and the SAME
mutual-exclusion rules from here, so the two surfaces cannot drift apart
again.

The three flags (identical for both verbs):

* ``--all-running``  — only agents with a LIVE session. The
  least-surprising "act on the live fleet" choice: an agent the operator
  deliberately stopped stays stopped.
* ``--all-registry`` — every registered agent, INCLUDING stopped ones.
* ``--all``          — backward-compat alias for ``--all-registry``.

All three are mutually exclusive with explicit name arguments and with
each other, and all three still require ``-y/--yes`` (the confirmation
guard on a fleet-wide destructive op is not weakened by bulk selection).
``--dry-run`` composes with them and needs no ``-y`` — it is the safety
valve for previewing what a fleet-wide op WOULD touch.
"""

from __future__ import annotations

from typing import Callable

import click


def _enumerate_fleet() -> list[str]:
    """Return every agent name ``sac agents list`` shows (the ``--all-registry`` set).

    Reuses the SAME data function the ``list`` command uses
    (:func:`cli_pkg._helpers.get_agent_list_data`) so ``--all-registry`` is
    exactly "everything ``sac agents list`` shows" — registered/running
    agents plus on-disk-defined ones — with no separate enumeration path to
    drift. Order-preserving de-dup by name.
    """
    from ..._state.registry import Registry
    from .._helpers import get_agent_list_data

    seen: set[str] = set()
    names: list[str] = []
    for row in get_agent_list_data(Registry()):
        name = row.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _enumerate_running() -> list[str]:
    """Return the agents that are currently LIVE (the ``--all-running`` set).

    Reuses the SAME data function — and therefore the SAME liveness — the
    ``list`` / ``status`` commands use
    (:func:`cli_pkg._helpers.get_agent_list_data`). Rows that are ``stopped`` /
    ``unknown`` / ``defined`` / ``invalid`` are excluded, so a plain
    ``--all-running`` never touches an agent the operator had deliberately
    stopped. No separate liveness rule is invented here. Order-preserving
    de-dup by name.

    LIVE is :func:`cli_pkg._helpers.is_live_status`, NOT ``== "running"``: an
    ``auth-failed`` agent IS up (its session exists and its pane process is
    alive) — it simply cannot call the API. Matching ``"running"`` alone would
    silently skip exactly the agents that most need acting on, and a RESTART
    cures the common cause (a token revoked by a sibling's OAuth refresh).
    """
    from ..._state.registry import Registry
    from .._helpers import get_agent_list_data, is_live_status

    seen: set[str] = set()
    names: list[str] = []
    for row in get_agent_list_data(Registry()):
        if not is_live_status(row.get("status")):
            continue
        name = row.get("name")
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def bulk_selection_options(verb: str, noun: str = "NAME") -> Callable:
    """Attach the three shared selection flags to a click command.

    ``verb`` is the command's own verb (``"restart"`` / ``"stop"``) and
    ``noun`` the metavar of its positional argument — both only ever reach
    the HELP TEXT, so the flag names, defaults and destinations stay
    byte-identical across the two verbs by construction.

    The options are applied bottom-up (``--all`` first) so the resulting
    ``--help`` ordering is ``--all-running``, ``--all-registry``, ``--all``
    — the same order a reader of the source sees.
    """
    verb_title = verb.capitalize()

    def _decorate(fn: Callable) -> Callable:
        fn = click.option(
            "--all",
            "all_alias",
            is_flag=True,
            default=False,
            help=(
                f"Backward-compat alias for --all-registry ({verb}s stopped "
                f"agents too). Prefer the explicit flags: --all-running "
                f"{verb}s only the live fleet; --all-registry {verb}s every "
                f"registered agent."
            ),
        )(fn)
        fn = click.option(
            "--all-registry",
            "all_registry",
            is_flag=True,
            default=False,
            help=(
                f"{verb_title} EVERY agent 'sac agents list' shows — INCLUDING "
                f"stopped ones. Mutually exclusive with explicit {noun} "
                f"arguments and with --all-running. Still requires -y/--yes."
            ),
        )(fn)
        fn = click.option(
            "--all-running",
            "all_running",
            is_flag=True,
            default=False,
            help=(
                f"{verb_title} ONLY the agents that are currently RUNNING (live "
                f"session). The least-surprising choice for '{verb} the live "
                f"fleet' — a deliberately-stopped agent stays stopped. Mutually "
                f"exclusive with explicit {noun} arguments and with "
                f"--all-registry. Still requires -y/--yes."
            ),
        )(fn)
        return fn

    return _decorate


def resolve_selection(
    names: tuple[str, ...],
    *,
    all_running: bool,
    all_registry: bool,
    all_alias: bool,
    enumerate_running: Callable[[], list[str]],
    enumerate_fleet: Callable[[], list[str]],
    noun: str = "NAME",
    metavar: str = "NAME...",
) -> tuple[list[str], bool]:
    """Resolve positional names + selection flags into ``(targets, batch_mode)``.

    The single source of truth for what the three flags MEAN, so ``restart``
    and ``stop`` cannot disagree. Raises :class:`click.UsageError` (exit 2)
    on every ambiguous invocation:

      * more than one selection mode (``--all-running`` with
        ``--all-registry`` / ``--all``);
      * a selection flag combined with explicit names — the operator must
        not be able to half-mean "these two AND everything";
      * neither names nor a selection flag — a bare ``sac agents stop`` keeps
        failing LOUD rather than silently stopping the whole fleet.

    ``enumerate_running`` / ``enumerate_fleet`` are passed in (rather than
    called directly) so each command keeps its own module-level enumeration
    seam — the attribute the tests swap — while the SELECTION LOGIC itself
    stays shared here.

    Returns the target list plus a ``batch_mode`` flag. An EMPTY target list
    with ``batch_mode`` True means "a selection flag matched nothing", which
    is not an error — the caller reports it and exits 0.
    """
    # ``--all`` is a backward-compat alias for ``--all-registry`` (do not
    # break cron/callers that still pass the old flag). The remaining
    # selection modes are mutually exclusive with each other.
    registry_mode = all_registry or all_alias
    running_mode = all_running
    if registry_mode and running_mode:
        raise click.UsageError(
            "--all-running and --all-registry (--all) are mutually exclusive; "
            "pass exactly one selection flag."
        )
    batch_mode = registry_mode or running_mode

    if batch_mode and names:
        raise click.UsageError(
            "A selection flag (--all-running / --all-registry / --all) cannot "
            f"be combined with explicit agent {noun} arguments."
        )

    if not batch_mode:
        if not names:
            raise click.UsageError(
                f"Missing argument '{metavar}'. Pass one or more agent names, "
                "or a selection flag (--all-running / --all-registry / --all)."
            )
        return list(names), False

    targets = enumerate_running() if running_mode else enumerate_fleet()
    return targets, True


__all__ = [
    "_enumerate_fleet",
    "_enumerate_running",
    "bulk_selection_options",
    "resolve_selection",
]
