"""``sac dev {cron,daemon,systemd}`` — sac's federated scheduled jobs.

Surfaces sac's OWN jobs (``sac.*``) by delegating to scitex-dev's
ecosystem aggregator (``scitex_dev.jobs``).

The verbs per job-kind mirror scitex-dev's canonical ecosystem
aggregator exactly (``scitex-dev ecosystem <kind> <verb>``):

* ``cron``    → ``list`` / ``install`` / ``uninstall``
* ``systemd`` → ``list`` / ``install`` / ``uninstall``
* ``daemon``  → ``list`` / ``exec``   (a daemon is *run*, not "installed")

``install`` / ``uninstall`` delegate to
``scitex-dev ecosystem <kind> {install,uninstall} --name <name>`` so the
unit/cron generation stays single-sourced in scitex-dev; ``exec``
delegates to ``scitex-dev ecosystem daemon exec <name>`` (positional
job-name argument, like scitex-dev's own ``daemon exec``).

Graceful degradation: a scitex-dev that predates the ``scitex_dev.jobs``
contract (PyPI lag — the contract is unreleased at the time of writing)
raises ``ImportError`` on the lazy import; every command catches it and
prints an upgrade hint instead of a stack trace.

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


def _degrade_msg() -> str:
    return (
        "this command requires scitex-dev>=" + _JOBS_MIN_VERSION + " "
        "(the release that adds `scitex_dev.jobs`); upgrade with: "
        "uv pip install -U scitex-dev"
    )


def _load_sac_jobs(kind: str) -> list:
    """Return sac-owned ``JobSpec`` of ``kind``, or raise ImportError.

    Lazy import of ``scitex_dev.jobs`` so an older installed scitex-dev
    surfaces as a clean ImportError the callers translate into an
    upgrade hint.
    """
    from scitex_dev.jobs import jobs_of_kind  # may ImportError on old scitex-dev

    return [j for j in jobs_of_kind(kind) if j.name.startswith(_SAC_PREFIX)]


def _ecosystem_delegate(kind: str, verb: str, name: str, yes: bool = False) -> int:
    """Delegate to ``scitex-dev ecosystem <kind> <verb> ... <name>``.

    The job-name is passed the way each canonical verb expects it:

    * ``daemon exec`` takes a *positional* ``NAME`` argument (and no
      ``--yes``), matching ``scitex-dev ecosystem daemon exec <name>``.
    * ``cron``/``systemd`` ``install``/``uninstall`` take ``--name
      <name>`` (and optionally ``--yes``).

    Returns the subprocess exit code. ``scitex-dev`` is a hard dependency
    of this package, so the console script is expected on PATH; a missing
    binary is reported as a clean ClickException.
    """
    exe = shutil.which("scitex-dev")
    if exe is None:
        raise click.ClickException(
            "`scitex-dev` console script not found on PATH; " + _degrade_msg()
        )
    if kind == "daemon" and verb == "exec":
        # `daemon exec` runs in the foreground via a positional NAME.
        return subprocess.call([exe, "ecosystem", kind, verb, name])
    cmd = [exe, "ecosystem", kind, verb, "--name", name]
    if yes:
        cmd.append("--yes")
    return subprocess.call(cmd)


def _add_list_command(grp, kind: str) -> None:
    """Attach the shared ``list`` read-verb onto a kind group."""

    @grp.command("list")
    @click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
    def _list(as_json):
        """List sac's own jobs of this kind."""
        import json as _json

        try:
            jobs = _load_sac_jobs(kind)
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if as_json:
            click.echo(
                _json.dumps(
                    [
                        {
                            "name": j.name,
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
            click.echo(f"No sac {kind}-kind jobs.")
            return
        for j in jobs:
            cadence = j.on_unit_active_sec or j.schedule
            click.echo(f"  {j.name:24s} every {cadence}")
            click.echo(f"  {'':24s} {j.command}")
            click.echo(f"  {'':24s} {j.description}")


def _make_installable_group(kind: str):
    """Build a ``sac dev <kind>`` group with list/install/uninstall.

    Used for the ``cron`` and ``systemd`` job-kinds — both of which are
    *installed* (materialised into a crontab block / systemd unit files)
    by scitex-dev. Mirrors ``scitex-dev ecosystem <kind>``.
    """

    @click.group(kind, invoke_without_command=True)
    @click.pass_context
    def _grp(ctx):
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    _grp.help = (
        f"sac's federated {kind} jobs (delegates to scitex-dev ecosystem).\n\n"
        "\b\nVerbs:\n"
        "  list       — show sac's own jobs of this kind\n"
        "  install    — generate + install them via scitex-dev\n"
        "  uninstall  — remove them via scitex-dev"
    )

    _add_list_command(_grp, kind)

    @_grp.command("install")
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Confirm. Forwarded to scitex-dev.",
    )
    def _install(yes):
        """Install sac's jobs of this kind via scitex-dev."""
        try:
            jobs = _load_sac_jobs(kind)
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if not jobs:
            click.echo(f"No sac {kind}-kind jobs to install.")
            return
        rc = 0
        for j in jobs:
            code = _ecosystem_delegate(kind, "install", j.name, yes)
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
        """Uninstall sac's jobs of this kind via scitex-dev."""
        try:
            jobs = _load_sac_jobs(kind)
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if not jobs:
            click.echo(f"No sac {kind}-kind jobs to uninstall.")
            return
        rc = 0
        for j in jobs:
            code = _ecosystem_delegate(kind, "uninstall", j.name, yes)
            rc = rc or code
        raise SystemExit(rc)

    return _grp


def _make_daemon_group():
    """Build the ``sac dev daemon`` group with list/exec.

    A daemon is a long-running process that is *run* (not "installed"),
    so this group exposes ``exec`` (run one job in the foreground) rather
    than install/uninstall — matching ``scitex-dev ecosystem daemon``.
    """

    @click.group("daemon", invoke_without_command=True)
    @click.pass_context
    def _grp(ctx):
        if ctx.invoked_subcommand is None:
            click.echo(ctx.get_help())

    _grp.help = (
        "sac's federated daemon jobs (delegates to scitex-dev ecosystem).\n\n"
        "\b\nVerbs:\n"
        "  list  — show sac's own daemon jobs\n"
        "  exec  — run one sac daemon job in the foreground via scitex-dev"
    )

    _add_list_command(_grp, "daemon")

    @_grp.command("exec")
    @click.argument("name")
    def _exec(name):
        """Run the sac daemon job NAME in the foreground via scitex-dev.

        \b
        Blocks until the process exits; the caller is responsible for
        backgrounding / supervision. Delegates to
        `scitex-dev ecosystem daemon exec <name>`.
        """
        try:
            jobs = {j.name: j for j in _load_sac_jobs("daemon")}
        except ImportError:  # stx-allow: fallback (reason: old scitex-dev lacks scitex_dev.jobs — print upgrade hint, not a stack trace)
            click.echo(_degrade_msg(), err=True)
            raise SystemExit(3)
        if name not in jobs:
            known = ", ".join(sorted(jobs)) or "(none)"
            raise click.ClickException(
                f"unknown sac daemon job: {name!r}. Known jobs: {known}"
            )
        raise SystemExit(_ecosystem_delegate("daemon", "exec", name))

    return _grp


def register_dev_jobs_commands(dev_group: click.Group) -> None:
    """Attach the ``cron`` / ``daemon`` / ``systemd`` groups onto ``sac dev``."""
    for kind in ("cron", "systemd"):
        dev_group.add_command(_make_installable_group(kind))
    dev_group.add_command(_make_daemon_group())


__all__ = ["register_dev_jobs_commands"]
