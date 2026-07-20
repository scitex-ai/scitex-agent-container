"""``sac dev {cron,systemd}`` — sac's federated scheduled jobs.

Surfaces sac's OWN jobs (``sac.*``) by delegating to scitex-dev's
ecosystem aggregator (``scitex_dev.jobs``).

The group name is the ``scitex-dev ecosystem <name>`` subcommand we
delegate to; the KIND is the ``JobSpec.kind`` taxonomy we filter on. They
are NOT the same axis, and :data:`GROUP_KINDS` is the mapping between
them. Conflating the two made every job verb here inert for weeks —
``_load_sac_jobs`` was called with the GROUP NAME, so ``sac dev systemd
list`` asked for ``kind="systemd"``, a value ``JobSpec.validate()``
rejects at construction (``ALLOWED_KINDS`` is ``{service,timer,cron}``
since scitex-dev #153). No job could ever match, so all four of sac's
timers — the OAuth refresh, the drift check, the worktree GC and the
fleet reconciler — were invisible to their own CLI, which reported "No
sac systemd-kind jobs." and exit 0. The tests passed the whole time
because the fixture hand-rolled a fake ``scitex_dev.jobs`` whose ``_Job``
defaulted to ``kind="systemd"`` — a spec shape no real spec can have —
so the suite never ran the real validator. See ``_jobs_audit`` for the
detector that now makes this shape fail loudly in CI.

:data:`GROUP_KINDS` mirrors scitex-dev's canonical selection exactly
(verified against ``_cli/ecosystem/_cmds/``): ``_jobs_cron.py`` selects
``jobs_of_kind("cron")``; ``_jobs_systemd.py`` selects
``jobs_of_kind("timer") + jobs_of_kind("service")``.

The verbs per group mirror ``scitex-dev ecosystem <group> <verb>``:

* ``cron``    → ``list`` / ``install`` / ``uninstall``
* ``systemd`` → ``list`` / ``install`` / ``uninstall``

There is deliberately NO ``sac dev daemon`` group. It used to exist and
was dead in BOTH halves: it filtered ``kind="daemon"`` (not a legal kind,
so always zero jobs) and delegated to ``scitex-dev ecosystem daemon``,
which is not a subcommand of ``ecosystem`` at all. A long-running job is
``kind="service"``, installed through the ``systemd`` group above.

``install`` / ``uninstall`` delegate to
``scitex-dev ecosystem <group> {install,uninstall} --name <name>`` so the
unit/cron generation stays single-sourced in scitex-dev.

Graceful degradation: a scitex-dev that predates the ``scitex_dev.jobs``
contract (PyPI lag) raises ``ImportError`` on the lazy import; every
command catches it and prints an upgrade hint instead of a stack trace.

These commands are attached onto ``dev_group`` at import time via
:func:`register_dev_jobs_commands` (same pattern as the account group's
``register_sync_live_commands`` / ``register_refresh_command``).
"""

from __future__ import annotations

import shutil
import subprocess

import click

# The scitex-dev release that first ships ``scitex_dev.jobs`` (PR #91).
# scitex-dev 0.15.0 is the last release WITHOUT it; the jobs contract is
# in scitex-dev's Unreleased section → first available in 0.16.0.
_JOBS_MIN_VERSION = "0.16.0"

# sac owns every job whose name is prefixed ``sac.`` (JobSpec.name
# convention — see scitex_dev.jobs docstring).
_SAC_PREFIX = "sac."

#: ``sac dev <group>`` -> the ``JobSpec.kind`` values that group lists.
#:
#: THE SSOT for this mapping, and the reason it is module-level rather
#: than inlined: ``_jobs_audit.audit_jobs`` imports THIS dict to check
#: that every kind sac declares has a consumer able to see it. If the
#: audit re-declared the mapping instead of importing the one production
#: uses, the audit would be checking its own opinion — a declaration with
#: no live counterpart, i.e. the exact disease it exists to detect.
GROUP_KINDS: dict[str, frozenset[str]] = {
    "cron": frozenset({"cron"}),
    "systemd": frozenset({"timer", "service"}),
}


def _degrade_msg() -> str:
    return (
        "this command requires scitex-dev>=" + _JOBS_MIN_VERSION + " "
        "(the release that adds `scitex_dev.jobs`); upgrade with: "
        "uv pip install -U scitex-dev"
    )


def _load_sac_jobs(kinds: frozenset[str]) -> list:
    """Return sac-owned ``JobSpec`` whose kind is in ``kinds``.

    Takes the KIND SET, never a group name: passing the group name was the
    bug that made every verb here inert (see the module docstring).

    Lazy import of ``scitex_dev.jobs`` so an older installed scitex-dev
    surfaces as a clean ImportError the callers translate into an
    upgrade hint.
    """
    from scitex_dev.jobs import jobs_of_kind  # may ImportError on old scitex-dev

    jobs: list = []
    for kind in sorted(kinds):
        jobs.extend(j for j in jobs_of_kind(kind) if j.name.startswith(_SAC_PREFIX))
    return jobs


def _ecosystem_delegate(group: str, verb: str, name: str, yes: bool = False) -> int:
    """Delegate to ``scitex-dev ecosystem <group> <verb> --name <name>``.

    Returns the subprocess exit code. ``scitex-dev`` is a hard dependency
    of this package, so the console script is expected on PATH; a missing
    binary is reported as a clean ClickException.
    """
    exe = shutil.which("scitex-dev")
    if exe is None:
        raise click.ClickException(
            "`scitex-dev` console script not found on PATH; " + _degrade_msg()
        )
    cmd = [exe, "ecosystem", group, verb, "--name", name]
    if yes:
        cmd.append("--yes")
    return subprocess.call(cmd)


def _add_list_command(grp, group: str) -> None:
    """Attach the shared ``list`` read-verb onto a group."""

    @grp.command("list")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def _list(as_json):
        """List sac's own jobs of this group."""
        import json as _json

        try:
            jobs = _load_sac_jobs(GROUP_KINDS[group])
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if as_json:
            click.echo(
                _json.dumps(
                    [
                        {
                            "name": j.name,
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
            click.echo(f"  {j.name:24s} every {cadence}")
            click.echo(f"  {'':24s} {j.command}")
            click.echo(f"  {'':24s} {j.description}")


def _make_installable_group(group: str):
    """Build a ``sac dev <group>`` group with list/install/uninstall.

    Mirrors ``scitex-dev ecosystem <group>``, which is what the verbs
    delegate to; the jobs shown are those whose kind is in
    ``GROUP_KINDS[group]``.
    """

    @click.group(group, invoke_without_command=True)
    @click.pass_context
    def _grp(ctx):
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    kinds = ", ".join(sorted(GROUP_KINDS[group]))
    _grp.help = (
        f"sac's federated {group} jobs (delegates to scitex-dev ecosystem).\n\n"
        f"\b\nShows JobSpecs of kind: {kinds}\n\n"
        "\b\nVerbs:\n"
        "  list       — show sac's own jobs of this group\n"
        "  install    — generate + install them via scitex-dev\n"
        "  uninstall  — remove them via scitex-dev"
    )

    _add_list_command(_grp, group)

    @_grp.command("install")
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _install(yes):
        """Install sac's jobs of this group via scitex-dev."""
        try:
            jobs = _load_sac_jobs(GROUP_KINDS[group])
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if not jobs:
            click.echo(f"No sac {group} jobs to install.")
            return
        rc = 0
        for j in jobs:
            code = _ecosystem_delegate(group, "install", j.name, yes)
            rc = rc or code
        raise SystemExit(rc)

    @_grp.command("uninstall")
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _uninstall(yes):
        """Uninstall sac's jobs of this group via scitex-dev."""
        try:
            jobs = _load_sac_jobs(GROUP_KINDS[group])
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if not jobs:
            click.echo(f"No sac {group} jobs to uninstall.")
            return
        rc = 0
        for j in jobs:
            code = _ecosystem_delegate(group, "uninstall", j.name, yes)
            rc = rc or code
        raise SystemExit(rc)

    return _grp


def _add_audit_execstart_command(dev_group: click.Group) -> None:
    """Attach ``sac dev audit-execstart`` — the deployed-vs-declared check.

    This verb is the whole reason ``_execstart_audit`` is not a CI test:
    the comparison can only be made where the units live, and CI runs in a
    SIF with no access to the fleet host's ``systemctl --user``. A checker
    nobody can run is the inert-feature shape it exists to detect.
    """

    @dev_group.command(name="audit-execstart")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def _audit_execstart(as_json):
        """Check each sac unit RUNS what its JobSpec declares.

        Reports; never repairs. A divergence means an unmanaged local
        override or a generator bug — both worth knowing, neither safe to
        rewrite automatically.
        """
        import json as _json

        from scitex_agent_container._execstart_audit import audit_execstart

        try:
            report = audit_execstart()
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)

        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "ok": report.ok,
                        "findings": [
                            {
                                "job": f.job,
                                "unit": f.unit,
                                "verdict": f.verdict.value,
                                "detail": f.detail,
                                "intended": f.intended,
                                "resolved": f.resolved,
                            }
                            for f in report.findings
                        ],
                    }
                )
            )
        else:
            click.echo(report.render())

        # 0 = nothing diverged, 1 = at least one unit does not run what the
        # source declares. UNKNOWN deliberately does NOT set a failing code:
        # "could not ask" is not "found a problem", and making it red would
        # mute the check everywhere systemd is absent. The verdicts are all
        # in the output — nothing here should be inferred from the exit code.
        raise SystemExit(0 if report.ok else 1)


def register_dev_jobs_commands(dev_group: click.Group) -> None:
    """Attach a job group onto ``sac dev`` for every entry in GROUP_KINDS."""
    for group in sorted(GROUP_KINDS):
        dev_group.add_command(_make_installable_group(group))
    _add_audit_execstart_command(dev_group)


__all__ = ["GROUP_KINDS", "register_dev_jobs_commands"]
