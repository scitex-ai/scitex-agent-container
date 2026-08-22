"""``sac dev {service,timer,cron}`` — sac's federated scheduled jobs.

THE GROUP NAME IS THE ``JobSpec.kind``. That is the whole design, it is
the ecosystem-wide grammar (operator decision, 2026-08-11 — every SciTeX
package exposes ``scitex-<pkg> dev {service,timer,cron} <verb>``), and it
is the countermeasure to the outage below rather than a cosmetic rename.
The tables that encode it now live in :mod:`._dev_jobs_grammar`; this
module holds the Click command builders that consume them, and
re-exports every public name so existing imports keep resolving.

WHAT THE OLD SHAPE COST
=======================
The groups used to be named after the DELIVERY MECHANISM (``cron`` and
``systemd``) while the filter was the KIND — two different axes wearing
one name. ``_load_sac_jobs`` was then called with the GROUP NAME, so
``sac dev systemd list`` asked for ``kind="systemd"``, a value
``JobSpec.validate()`` rejects at construction (``ALLOWED_KINDS`` is
``{service,timer,cron}`` since scitex-dev #153). No job could ever match,
so every timer sac owns — the OAuth refresh, the drift check, the
worktree GC, the fleet reconciler and the rest — was invisible to its own
CLI, which reported "No sac systemd-kind jobs." and exit 0. The tests
passed the whole time because the fixture hand-rolled a fake
``scitex_dev.jobs`` whose ``_Job`` defaulted to ``kind="systemd"`` — a
spec shape no real spec can have — so the suite never ran the real
validator.

Naming the groups after the kinds removes the second axis entirely: there
is no longer a group name that COULD be passed where a kind is expected
and mean something different. ``_jobs_audit`` machine-checks exactly that
(``Form.GROUP_IS_NOT_ITS_KIND``), so the invariant is enforced, not
documented.

``systemd`` SURVIVES AS A DEPRECATED ALIAS, WITH A DATE
=======================================================
Existing callers, docs and the operator's muscle memory all say
``systemd``, so removing it now would break working commands for no gain.
It stays — covering ``{service, timer}`` exactly as before, with the same
three verbs it always had and no new ones, so nothing new is built on
it — and it carries :class:`Deprecation` metadata with a real
``remove_after`` date. "当面" ("for the time being") becomes permanent
unless a date is written down, and a date nobody enforces is the same
thing, so ``test__dev_jobs.py`` fails the build once that date passes.

The notice goes to **stderr**, never stdout. ``sac`` commands are parsed
by machines: a sibling agent measured ``scitex_dev.hosts._retired``
printing a stale-registry ``WARN:`` to stdout, which corrupted
``sac host list --json`` and turned 7 tests red across three unrelated
PRs. Every ``--json`` verb here is covered by a test that reads STDOUT
ALONE (``Result.stdout``, not ``Result.output`` — click 8.4's ``output``
merges stderr in, which is precisely how a stdout-purity test can pass
while stdout is filthy, or fail while it is clean).

WHERE THE VERBS GO
==================
Delegation is resolved by :mod:`._dev_jobs_backend`, which also carries
the argument for why sac does not call ``systemctl`` itself. The verb set
differs per kind on purpose: a verb that makes no sense for a kind does
not exist for that kind, rather than existing and erroring. There is
deliberately no ``daemon`` group — it was dead in both halves (it
filtered ``kind="daemon"``, never legal, and delegated to an
``ecosystem daemon`` that does not exist). A long-running job is
``kind="service"``.

INSTALL IS NOT ENABLE, WHICH IS WHY ``enable`` IS BULK
======================================================
``install`` and ``enable`` are both bulk verbs here: given no NAME they
act on every job of the kind. That is not symmetry for its own sake.
scitex-dev's ``_jobs_units.do_install`` writes the unit files and then
merely PRINTS the ``systemctl --user enable --now`` line to stderr, so a
host that has been "installed" carries N correct, INERT units. Arming
them was N hand-typed commands until 2026-08-15, and seven of sac's ten
timers were duly found ``disabled`` on scitex-compute-04. ``disable``
stays strictly per-name — the reasoning for the asymmetry is on
:data:`._dev_jobs_grammar._BULK_VERBS`.

The collective apply that CALLS these verbs from host provisioning lives
in :mod:`._dev_jobs_apply`.

Graceful degradation: a scitex-dev that predates the ``scitex_dev.jobs``
contract raises ``ImportError`` on the lazy import; every command catches
it and prints an upgrade hint instead of a stack trace.

Exit codes: ``2`` refusing to mutate without ``--yes``, ``3`` scitex-dev
too old, ``4`` the verb exists in this grammar but the installed
scitex-dev cannot serve it yet, ``5`` no such job name.

These commands are attached onto ``dev_group`` at import time via
:func:`register_dev_jobs_commands`.
"""

from __future__ import annotations

import click

from .._jobs import _names
from . import _dev_jobs_backend as _backend
from ._dev_jobs_grammar import (
    _BULK_VERBS,
    _JOBS_MIN_VERSION,
    _NAMED_VERBS,
    _VERB_SUMMARY,
    DEPRECATED_GROUPS,
    Deprecation,
    GROUP_KINDS,
    GROUP_VERBS,
)


def _degrade_msg() -> str:
    return (
        "this command requires scitex-dev>=" + _JOBS_MIN_VERSION + " "
        "(the release that adds `scitex_dev.jobs`); upgrade with: "
        "uv pip install -U scitex-dev"
    )


def _load_sac_jobs(kinds: frozenset[str]) -> list:
    """Return sac-owned ``JobSpec`` whose kind is in ``kinds``.

    Takes the KIND SET, never a group name: passing the group name was
    the bug that made every verb here inert (see the module docstring).

    Lazy import of ``scitex_dev.jobs`` so an older installed scitex-dev
    surfaces as a clean ImportError the callers translate into an upgrade
    hint.
    """
    from scitex_dev.jobs import jobs_of_kind  # may ImportError on old scitex-dev

    jobs: list = []
    for kind in sorted(kinds):
        jobs.extend(j for j in jobs_of_kind(kind) if _names.is_ours(j.name))
    return jobs


def _jobs_or_degrade(group: str) -> list:
    """Load this group's jobs, or exit 3 with the upgrade hint on stderr."""
    try:
        return _load_sac_jobs(GROUP_KINDS[group])
    except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
        click.echo(_degrade_msg(), err=True)
        raise SystemExit(3)


def _announce_deprecation(group: str) -> None:
    """Print the dated deprecation notice to STDERR, never stdout.

    stdout carries machine-readable payloads (``--json``); a courtesy
    message written there is indistinguishable from data.
    """
    dep = DEPRECATED_GROUPS.get(group)
    if dep is not None:
        click.echo(dep.notice(group), err=True)


def _kind_of(group: str) -> str:
    """The kind a group's VERBS act on.

    Identical to the group name for every kind group. The deprecated
    alias covers two kinds, so its verbs resolve against ``timer`` — the
    kind every job sac declares today has, and the one whose delegation
    target (``ecosystem systemd``) is the same for both.
    """
    return "timer" if group == "systemd" else group


def _delegate(
    kind: str,
    verb: str,
    name: str | None,
    yes: bool,
    dry_run: bool = False,
    adopt: bool = False,
    force: bool = False,
) -> int:
    """Resolve + run one ecosystem delegation. THE single mutation seam.

    Tests replace this one callable to capture the ``(kind, verb, name,
    yes, dry_run)`` tuples a verb delegates with, rather than shelling out
    to a real ``scitex-dev`` that would rewrite the host's units and
    crontab. The delegation ARGUMENTS are what those tests are about.

    ``adopt`` and ``force`` default to False and callers pass them BY
    KEYWORD, so the positional call shape those tests capture is unchanged:
    a five-element tuple before this parameter existed, five after. A
    required parameter — or a positional call — would have rewritten every
    one of those assertions to prove nothing about the behaviour that moved.
    """
    delegation = _backend.resolve(kind, verb)
    if not delegation.supported:
        click.echo(
            f"`sac dev {kind} {verb}` is part of the SciTeX job grammar, but "
            f"the installed scitex-dev cannot serve it yet: "
            f"{delegation.evidence}.",
            err=True,
        )
        click.echo(
            "Run it by hand meanwhile: "
            + _backend.manual_hint(kind, verb, name or "<job>"),
            err=True,
        )
        raise SystemExit(4)
    return _backend.invoke(
        delegation,
        name=name,
        yes=yes,
        dry_run=dry_run,
        adopt=adopt,
        force=force,
    )


def _resolve_one(group: str, typed: str) -> str:
    """Resolve a typed local-or-canonical job name, or exit 5."""
    jobs = _jobs_or_degrade(group)
    try:
        return _names.resolve(typed, [j.name for j in jobs])
    except KeyError as exc:
        click.echo(str(exc.args[0]), err=True)
        raise SystemExit(5)


def _add_list_command(grp, group: str) -> None:
    """Attach the shared ``list`` read-verb onto a group."""

    @grp.command("list")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def _list(as_json):
        """List sac's own jobs of this kind."""
        import json as _json

        _announce_deprecation(group)
        jobs = _jobs_or_degrade(group)
        if as_json:
            click.echo(
                _json.dumps(
                    [
                        {
                            "name": j.name,
                            "local_name": _names.local(j.name),
                            "kind": j.kind,
                            "schedule": j.schedule,
                            "command": j.command,
                            "description": j.description,
                            "on_unit_active_sec": j.on_unit_active_sec,
                        }
                        for j in jobs
                    ]
                )
            )
            return
        if not jobs:
            click.echo(f"No sac {group} jobs.")
            return
        for j in jobs:
            cadence = j.on_unit_active_sec or j.schedule
            click.echo(f"  {_names.local(j.name):24s} every {cadence}")
            click.echo(f"  {'':24s} {j.command}")
            click.echo(f"  {'':24s} {j.description}")


def _adoption_options(verb: str):
    """``--adopt`` / ``--force``, attached ONLY to the verb that has them.

    Both are real options on ``scitex-dev ecosystem timer install`` and on
    nothing else in the group. Declaring them unconditionally would let
    ``sac dev timer uninstall --force`` parse here and then fail downstream
    on a command that has no such option — trading one misleading message
    for another.

    They exist at all because scitex-dev's refusal names them. MEASURED
    2026-08-20: ``install`` on an existing unit prints "Use --adopt to keep
    the existing supervisor (writes nothing), or --force to overwrite", and
    following that advice returned ``Error: No such option '--force'``. The
    wrapper forwarded the message and not the flags.
    """

    def decorate(fn):
        if verb != "install":
            return fn
        fn = click.option(
            "--force",
            is_flag=True,
            default=False,
            help=(
                "Overwrite even when another supervisor exists. Forwarded "
                "to scitex-dev, which reports loudly what it replaced."
            ),
        )(fn)
        fn = click.option(
            "--adopt",
            is_flag=True,
            default=False,
            help=(
                "Keep an existing supervisor of ANY mechanism and write "
                "nothing. Forwarded to scitex-dev."
            ),
        )(fn)
        return fn

    return decorate


def _add_bulk_command(grp, group: str, verb: str) -> None:
    """Attach a verb that acts on every job of the kind, or one named one."""

    @grp.command(verb)
    @click.argument("name", required=False)
    @click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        default=False,
        help="Preview only. Forwarded to scitex-dev.",
    )
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    @_adoption_options(verb)
    def _bulk(name, dry_run, yes, adopt=False, force=False, _verb=verb):
        _announce_deprecation(group)
        jobs = _jobs_or_degrade(group)
        if name is not None:
            wanted = _resolve_one(group, name)
            jobs = [j for j in jobs if j.name == wanted]
        if not jobs:
            click.echo(f"No sac {group} jobs to {_verb}.")
            return
        rc = 0
        for j in jobs:
            code = _delegate(
                _kind_of(group),
                _verb,
                j.name,
                yes,
                dry_run,
                # BY KEYWORD, and that is load-bearing. The tests replace
                # `_delegate` with a lambda that captures `*args`, so passing
                # these positionally would grow every captured tuple from five
                # elements to seven and break assertions about a call shape
                # this change does not alter.
                adopt=adopt,
                force=force,
            )
            rc = rc or code
        raise SystemExit(rc)

    _bulk.help = (
        f"{_VERB_SUMMARY[verb]} sac's {group} jobs via scitex-dev.\n\n"
        "\b\nNAME is the short local name (e.g. `accounts-refresh`); the "
        "canonical form works too. Omit it to act on every job of this kind."
    )


def _add_named_command(grp, group: str, verb: str) -> None:
    """Attach a lifecycle verb that acts on ONE named job."""

    mutating = verb in _backend.MUTATING_VERBS

    @grp.command(verb)
    @click.argument("name")
    @click.option(
        "--dry-run",
        "dry_run",
        is_flag=True,
        default=False,
        hidden=not mutating,
        help="Preview only. Forwarded to scitex-dev.",
    )
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        hidden=not mutating,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _named(name, dry_run, yes, _verb=verb):
        _announce_deprecation(group)
        wanted = _resolve_one(group, name)
        raise SystemExit(_delegate(_kind_of(group), _verb, wanted, yes, dry_run))

    _named.help = (
        f"{_VERB_SUMMARY[verb]} one of sac's {group} jobs via scitex-dev.\n\n"
        "\b\nNAME is the short local name (e.g. `accounts-refresh`); the "
        "canonical form works too."
    )


def _make_group(group: str):
    """Build a ``sac dev <group>`` group with the verbs declared for it."""

    @click.group(group, invoke_without_command=True)
    @click.pass_context
    def _grp(ctx):
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    kinds = ", ".join(sorted(GROUP_KINDS[group]))
    verbs = "\n".join(f"  {v}" for v in GROUP_VERBS[group])
    dep = DEPRECATED_GROUPS.get(group)
    banner = (
        f"DEPRECATED since {dep.since}, REMOVED after {dep.remove_after} — "
        f"use {dep.replacement}.\n\n"
        if dep
        else ""
    )
    _grp.help = (
        f"{banner}sac's federated {group} jobs (delegates to scitex-dev "
        "ecosystem).\n\n"
        f"\b\nShows JobSpecs of kind: {kinds}\n\n"
        "\b\nVerbs:\n" + verbs
    )

    for verb in GROUP_VERBS[group]:
        if verb == "list":
            _add_list_command(_grp, group)
        elif verb in _BULK_VERBS:
            _add_bulk_command(_grp, group, verb)
        elif verb in _NAMED_VERBS:
            _add_named_command(_grp, group, verb)
        else:  # pragma: no cover - guarded by test_every_verb_has_a_shape
            raise ValueError(
                f"`sac dev {group} {verb}` has no declared verb shape; add it "
                "to _NAMED_VERBS or _BULK_VERBS"
            )

    return _grp


def register_dev_jobs_commands(dev_group: click.Group) -> None:
    """Attach a job group onto ``sac dev`` for every entry in GROUP_KINDS."""
    for group in sorted(GROUP_KINDS):
        dev_group.add_command(_make_group(group))


__all__ = [
    "DEPRECATED_GROUPS",
    "Deprecation",
    "GROUP_KINDS",
    "GROUP_VERBS",
    "register_dev_jobs_commands",
]
