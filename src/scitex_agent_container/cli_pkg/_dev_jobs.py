"""``sac dev {service,timer,cron}`` — sac's federated scheduled jobs.

THE GROUP NAME IS THE ``JobSpec.kind``. That is the whole design, it is
the ecosystem-wide grammar (operator decision, 2026-08-11 — every SciTeX
package exposes ``scitex-<pkg> dev {service,timer,cron} <verb>``), and it
is the countermeasure to the outage below rather than a cosmetic rename.

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

import re
from dataclasses import dataclass

import click

from .._jobs import _names
from . import _dev_jobs_backend as _backend

# The scitex-dev release that first ships ``scitex_dev.jobs`` (PR #91).
# scitex-dev 0.15.0 is the last release WITHOUT it; the jobs contract is
# in scitex-dev's Unreleased section -> first available in 0.16.0.
_JOBS_MIN_VERSION = "0.16.0"

#: ``sac dev <group>`` -> the ``JobSpec.kind`` values that group lists.
#:
#: THE SSOT for this mapping, and the reason it is module-level rather
#: than inlined: ``_jobs_audit.audit_jobs`` imports THIS dict to check
#: that every kind sac declares has a consumer able to see it, and that
#: no group filters on a kind the validator would reject. If the audit
#: re-declared the mapping instead of importing the one production uses,
#: the audit would be checking its own opinion — a declaration with no
#: live counterpart, i.e. the exact disease it exists to detect.
#:
#: Every entry except the deprecated alias maps a group to EXACTLY the
#: kind of the same name. That identity is the invariant; it is checked,
#: not trusted.
GROUP_KINDS: dict[str, frozenset[str]] = {
    "service": frozenset({"service"}),
    "timer": frozenset({"timer"}),
    "cron": frozenset({"cron"}),
    # Deprecated alias — see DEPRECATED_GROUPS. Covers both unit kinds,
    # which is what `scitex-dev ecosystem systemd` has always selected.
    "systemd": frozenset({"service", "timer"}),
}

_YYYY_MM = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Deprecation:
    """A dated retirement plan for a CLI surface.

    Every field is load-bearing and validated at construction, because a
    deprecation with a missing or malformed date is indistinguishable
    from no deprecation at all — which is how "temporary" aliases become
    permanent API.
    """

    since: str
    remove_after: str
    replacement: str

    def __post_init__(self) -> None:
        for field, value in (
            ("since", self.since),
            ("remove_after", self.remove_after),
        ):
            if not _YYYY_MM.match(value):
                raise ValueError(
                    f"Deprecation.{field}={value!r} must be YYYY-MM — a vague "
                    "date is the same as no date"
                )
        if self.remove_after <= self.since:
            raise ValueError(
                f"Deprecation.remove_after={self.remove_after!r} must be after "
                f"since={self.since!r}"
            )
        if not self.replacement:
            raise ValueError(
                "Deprecation.replacement must name what to use instead — a "
                "deprecation with no replacement is just a complaint"
            )

    def is_expired(self, today: str) -> bool:
        """True once ``today`` (``YYYY-MM``) is past ``remove_after``."""
        if not _YYYY_MM.match(today):
            raise ValueError(f"today={today!r} must be YYYY-MM")
        return today > self.remove_after

    def notice(self, group: str) -> str:
        """The stderr line printed on every use of the deprecated group."""
        return (
            f"DEPRECATED: `sac dev {group}` is deprecated since {self.since} "
            f"and will be REMOVED after {self.remove_after}. "
            f"Use: {self.replacement}. "
            "(The group name is now the JobSpec kind — `systemd` is a "
            "delivery mechanism, not a kind.)"
        )


#: Groups kept only for compatibility. A group listed here MUST also be
#: in GROUP_KINDS; the audit checks that a deprecated alias never becomes
#: the only way to reach a kind.
DEPRECATED_GROUPS: dict[str, Deprecation] = {
    "systemd": Deprecation(
        since="2026-08",
        remove_after="2026-10",
        replacement="`sac dev service` / `sac dev timer`",
    ),
}

#: Verbs that take a job NAME and act on ONE job.
_NAMED_VERBS: frozenset[str] = frozenset(
    {"status", "start", "stop", "restart", "enable", "disable"}
)

#: How each verb reads at the start of its one-line help. Spelled out
#: rather than ``verb.capitalize()`` because that produces "Status one of
#: sac's timer jobs", which is not a sentence.
_VERB_SUMMARY: dict[str, str] = {
    "status": "Show the status of",
    "start": "Start",
    "stop": "Stop",
    "restart": "Restart",
    "enable": "Enable",
    "disable": "Disable",
    "install": "Install",
    "uninstall": "Uninstall",
}

#: Verbs that act on EVERY job of the kind unless given an optional name,
#: and mutate the host, so they require ``--yes``.
_BULK_VERBS: frozenset[str] = frozenset({"install", "uninstall"})

#: ``sac dev <group>`` -> its verbs. Per KIND, deliberately not uniform.
#:
#: * ``service`` — a long-running unit: the full lifecycle applies.
#: * ``timer``   — scheduled, not run by hand. ``enable``/``disable`` is
#:   the systemd idiom for a timer (``enable --now`` starts it), so
#:   ``start``/``stop``/``restart`` would be three ways to say the same
#:   thing with different edge cases. Omitted rather than aliased.
#: * ``cron``    — a crontab line is present or commented out. There is
#:   no runtime object to start, stop or ask for status, so those verbs
#:   do not exist here instead of existing and erroring.
#: * ``systemd`` — the deprecated alias keeps EXACTLY its historical
#:   surface. New verbs are reachable only through the kind groups, so
#:   nothing new can be built on the alias.
GROUP_VERBS: dict[str, tuple[str, ...]] = {
    "service": (
        "list",
        "status",
        "start",
        "stop",
        "restart",
        "enable",
        "disable",
        "install",
        "uninstall",
    ),
    "timer": ("list", "status", "enable", "disable", "install", "uninstall"),
    "cron": ("list", "enable", "disable", "install", "uninstall"),
    "systemd": ("list", "install", "uninstall"),
}


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


def _delegate(kind: str, verb: str, name: str | None, yes: bool) -> int:
    """Resolve + run one ecosystem delegation. THE single mutation seam.

    Tests replace this one callable to capture the ``(kind, verb, name)``
    tuples a verb delegates with, rather than shelling out to a real
    ``scitex-dev`` that would rewrite the host's units and crontab. The
    delegation ARGUMENTS are what those tests are about.
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
    return _backend.invoke(delegation, name=name, yes=yes)


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


def _add_bulk_command(grp, group: str, verb: str) -> None:
    """Attach ``install`` / ``uninstall``: every job of the kind, or one."""

    @grp.command(verb)
    @click.argument("name", required=False)
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _bulk(name, yes, _verb=verb):
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
            code = _delegate(_kind_of(group), _verb, j.name, yes)
            rc = rc or code
        raise SystemExit(rc)

    _bulk.help = (
        f"{_VERB_SUMMARY[verb]} sac's {group} jobs via scitex-dev.\n\n"
        "\b\nNAME is the short local name (e.g. `accounts-refresh`); the "
        "canonical form works too. Omit it to act on every job of this kind."
    )


def _add_named_command(grp, group: str, verb: str) -> None:
    """Attach a lifecycle verb that acts on ONE named job."""

    @grp.command(verb)
    @click.argument("name")
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _named(name, yes, _verb=verb):
        _announce_deprecation(group)
        wanted = _resolve_one(group, name)
        raise SystemExit(_delegate(_kind_of(group), _verb, wanted, yes))

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
