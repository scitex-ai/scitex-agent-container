"""Reading the INSTALLED scitex-dev's Click tree — capability, not delegation.

Split out of ``_dev_jobs_backend`` (which had grown past the line limit)
along the seam that was already there: this module only ever ASKS THE
INSTALLED PACKAGE WHAT IT CAN DO. It knows nothing about ``Delegation``,
argv, or subprocesses; ``_dev_jobs_backend`` owns those and re-exports
everything here, so existing imports of either name keep working.

THE DOCTRINE, WHICH IS THE WHOLE REASON THIS IS INTROSPECTION AND NOT A
TABLE: read the real thing. A hard-coded map of "which verbs exist" or
"which verbs take ``--name``" is a COPY of another package's CLI, and a
copy goes stale silently — it keeps answering confidently while the
original moves underneath it. Both times this module was wrong, that was
why:

* scitex-dev relocated its job groups under ``ecosystem dev`` and left
  deprecated forwarding shims at the old names, so a name-keyed probe
  said "present" about a ``Command`` that enumerates zero verbs; and
* scitex-dev 0.48.0 made the job-name argument shape MIXED, so emitting
  ``--name`` unconditionally started failing on half the verbs.

Every answer here therefore comes from the installed objects, and every
unreadable answer is a THIRD STATE (``None`` / ``"unknown"``), never a
refusal. A false "unsupported" refuses a command that would have worked,
which is the more expensive mistake in both directions.
"""

from __future__ import annotations

import click

#: The ecosystem subcommand that serves a kind on the surface that has
#: SHIPPED: ``cron`` for cron jobs, and ``systemd`` for both unit kinds,
#: which it lumps together.
#:
#: This is a FALLBACK, consulted only after the preferred per-kind
#: surface (``dev service`` / ``dev timer``) is probed for and found
#: absent. It disappears on its own the moment scitex-dev ships those
#: groups — no coordinated release, no flag.
LEGACY_GROUP_FOR_KIND: dict[str, str] = {
    "service": "systemd",
    "timer": "systemd",
    "cron": "cron",
}

#: The subgroup scitex-dev moved its job CLI under. Probed for, never
#: assumed: an older scitex-dev mounts the groups at the top level and a
#: newer one under here, and both must work without a sac release.
DEV_SUBGROUP = "dev"

#: Group names worth descending into. Everything else under ``ecosystem``
#: is unrelated to jobs.
_PROBED_GROUPS: frozenset[str] = frozenset({"service", "timer", "cron", "systemd"})

# Sentinel distinguishing "not probed yet" from "probed, unavailable".
_UNPROBED: object = object()
_VERBS_CACHE: object = _UNPROBED


def reset_capability_cache() -> None:
    """Forget the probed surface. For tests that install a different one."""
    global _VERBS_CACHE
    _VERBS_CACHE = _UNPROBED


def _child(command, name: str):
    """Return the named subcommand of ``command``, or None."""
    getter = getattr(command, "get_command", None)
    if getter is not None:
        try:
            with click.Context(command) as ctx:
                return getter(ctx, name)
        except Exception:  # stx-allow: fallback (reason: another package's Group subclass may need a context we cannot build — fall back to the eager dict)
            pass
    return (getattr(command, "commands", {}) or {}).get(name)


def _leaf_verbs(command) -> frozenset[str]:
    """The subcommand names of one ecosystem group.

    ``Group.list_commands(ctx)`` rather than ``Group.commands`` so a LAZY
    group is read correctly — the eager dict can be empty while the group
    is fully featured.

    Returns an EMPTY set for a node that is a plain ``Command`` (a
    forwarding shim) rather than a ``Group``. That empty is honest — a
    shim genuinely has no enumerable verbs — and callers must read it as
    "cannot tell", never as "serves nothing"; see ``resolve``.

    CORRECTION, so the next reader does not inherit a wrong mechanism:
    an earlier revision claimed laziness EXPLAINED the "four cron verbs
    locally, zero in CI" split. It did not. Measured afterwards, the real
    cause was scitex-dev relocating those verbs under ``ecosystem dev``
    and leaving a deprecated ``Command`` shim behind at the old name.
    Reading ``list_commands`` is still correct; it was simply not the fix
    for that symptom — walking into ``dev`` is.
    """
    lister = getattr(command, "list_commands", None)
    if lister is not None:
        try:
            with click.Context(command) as ctx:
                return frozenset(lister(ctx))
        except Exception:  # stx-allow: fallback (reason: another package's Group subclass may need a context we cannot build — fall back to the eager dict)
            pass
    return frozenset(getattr(command, "commands", {}) or {})


def _walk(ecosystem) -> dict[tuple[str, ...], frozenset[str]]:
    """Map ``path under ecosystem -> its verbs``, one level into ``dev``.

    Depth is bounded to the job groups on purpose: enumerating all ~51
    ecosystem subcommands' children would pay for building trees nobody
    here consults.
    """
    tree: dict[tuple[str, ...], frozenset[str]] = {}
    for name in sorted(_leaf_verbs(ecosystem)):
        if name not in _PROBED_GROUPS and name != DEV_SUBGROUP:
            continue
        group = _child(ecosystem, name)
        if group is None:
            continue
        tree[(name,)] = _leaf_verbs(group)
        if name != DEV_SUBGROUP:
            continue
        for sub in sorted(tree[(name,)]):
            if sub not in _PROBED_GROUPS:
                continue
            child = _child(group, sub)
            if child is not None:
                tree[(name, sub)] = _leaf_verbs(child)
    return tree


def ecosystem_root():
    """The INSTALLED scitex-dev's wired ``ecosystem`` group, or ``None``.

    Kept separate from :func:`ecosystem_verbs` because that one caches a
    SUMMARY (path -> verb names) while callers that need to inspect a
    command's parameters need the COMMAND OBJECTS themselves.
    """
    try:
        from scitex_dev._cli.ecosystem import register_ecosystem_commands

        @click.group()
        def _probe_root() -> None:  # pragma: no cover - never invoked
            """Throwaway root; we only want the tree it gets wired onto."""

        return register_ecosystem_commands(_probe_root)
    except Exception:  # stx-allow: fallback (reason: introspecting another package's private CLI tree must degrade to "cannot tell", never to a refusal)
        return None


def ecosystem_verbs() -> dict[tuple[str, ...], frozenset[str]] | None:
    """Return ``{path under `ecosystem` -> verbs}`` for the INSTALLED scitex-dev.

    Keys are PATHS — ``("cron",)``, ``("dev", "timer")`` — because the job
    groups moved under ``ecosystem dev`` and the old names survive as
    empty shells. A name-keyed map cannot tell those two apart.

    ``None`` when the Click tree cannot be built at all (an old or partial
    scitex-dev). ``None`` means "cannot tell", never "unsupported" — the
    three-state discipline from ``_jobs_audit``: a false "unsupported"
    here refuses a command that would have worked.
    """
    global _VERBS_CACHE
    if _VERBS_CACHE is not _UNPROBED:
        return _VERBS_CACHE  # type: ignore[return-value]

    root = ecosystem_root()
    probed = None if root is None else _walk(root)

    _VERBS_CACHE = probed
    return probed


def leaf_command(path: tuple[str, ...], verb: str):
    """The real Click command ``ecosystem <path...> <verb>`` resolves to.

    ``None`` when any step is unreadable — "cannot tell", never
    "absent", per the three-state discipline this module follows
    throughout.
    """
    node = ecosystem_root()
    if node is None:
        return None
    for part in path:
        node = _child(node, part)
        if node is None:
            return None
    return _child(node, verb)


def name_is_an_option(command) -> bool:
    """Does ``command`` take the job name as ``--name``, or positionally?

    Answered by reading the command's OWN parameters. Deliberately takes a
    Click command rather than a path so it can be exercised against real
    commands of both shapes without reaching for the installed package.
    """
    for param in getattr(command, "params", None) or []:
        if "--name" in (getattr(param, "opts", None) or []):
            return True
    return False


def name_style_for(path: tuple[str, ...], verb: str) -> str:
    """``"option"`` | ``"positional"`` | ``"unknown"`` for the job name.

    *** WHY THIS EXISTS: scitex-dev 0.48.0 MADE THE SHAPE MIXED. ***
    Through 0.47.0 every job verb took ``--name X``, so the delegation
    emitted it unconditionally. 0.48.0 splits them:

        install / uninstall                                       --name X
        status / enable / disable / start / stop / restart / exec  NAME

    Sending the old shape to a new positional verb is rejected by Click
    BEFORE the command runs — ``Error: No such option '--name'``, exit 2 —
    which reads exactly like sac refusing the verb on capability grounds
    while sac is in fact failing to invoke it. That misreading cost a
    night of fleet-wide diagnosis (every open PR went red and the cause
    was hunted in runners, Python versions and branch staleness first), so
    the shape is now READ from the installed CLI instead of assumed.

    ``"unknown"`` when the leaf command cannot be introspected. Callers
    keep the pre-0.48 ``--name`` shape there, because that is what every
    reachable older scitex-dev wants and because both wrong guesses fail
    the same safe way: a Click usage error, before any host state is
    touched.
    """
    command = leaf_command(path, verb)
    if command is None:
        return "unknown"
    return "option" if name_is_an_option(command) else "positional"


__all__ = [
    "DEV_SUBGROUP",
    "LEGACY_GROUP_FOR_KIND",
    "ecosystem_root",
    "ecosystem_verbs",
    "leaf_command",
    "name_is_an_option",
    "name_style_for",
    "reset_capability_cache",
]
