"""``sac dev migrate-job-names`` — execute the canonical-name cutover.

A TRANSITION TOOL, NOT PART OF THE GRAMMAR
==========================================
The steady-state surface is ``sac dev {service,timer,cron} <verb>``, whose
verbs match scitex-dev's counterpart exactly so nothing sac exposes is a
permanent exit-4. This command is deliberately NOT one of those verbs: it
is a one-time migration, it has no counterpart in the shared layer, and it
will be removed once every host is cut over. Putting it inside a kind
group would make it look like a verb the ecosystem serves, which is the
same "declaration with no live counterpart" disease ``_jobs_audit`` exists
to detect. It sits at ``sac dev`` as its own dated, one-shot verb.

WHAT IT REFUSES, AND WHY EACH REFUSAL EARNS ITS PLACE
====================================================
* **No ``systemctl``** — exit 3. nas-01 (armv7l), nas-02 and mba (launchd)
  cannot supervise ``--user`` units at all. Running the plan there would
  fail every step and then report "NO supervisor", which reads as a broken
  migration rather than an inapplicable host.
* **No ``--yes``** — exit 2, after printing the whole plan. The default is
  a dry run because the thing being migrated is the supervision of the
  fleet's credential machinery.
* **Held jobs** — skipped unless ``--include-held``, which additionally
  requires naming them with ``--only``. ``sac.accounts-refresh`` is the
  fleet's SOLE OAuth refresher against a single-use refresh token; a
  bulk ``--include-held --yes`` that swept it up by accident is precisely
  the accident worth two flags.

Exit codes: ``0`` clean, ``2`` refusing to mutate without ``--yes``,
``3`` this host cannot supervise units, ``6`` a step failed, ``7`` the
verification found something other than exactly one supervisor.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import click

from .._jobs import _migrate

#: Where systemd looks for ``--user`` units. Overridable for tests.
UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

EXIT_NO_YES = 2
EXIT_UNSUPPORTED_HOST = 3
EXIT_STEP_FAILED = 6
EXIT_VERIFY_FAILED = 7


def _measure(unit_dir: Path) -> tuple[frozenset[str], frozenset[str]]:
    """Read the host: which unit files exist, and which have drop-in dirs.

    Measured, never assumed — the plan is a function of this.
    """
    if not unit_dir.is_dir():
        return frozenset(), frozenset()
    files: set[str] = set()
    dirs: set[str] = set()
    for child in unit_dir.iterdir():
        if child.is_dir() and child.name.endswith(".d"):
            dirs.add(child.name)
        elif child.is_file() or child.is_symlink():
            files.add(child.name)
    return frozenset(files), frozenset(dirs)


def _displace_dir(unit_dir: Path, stamp: str) -> Path:
    """``.old/<timestamp>/`` beside the units. NOTHING is ever deleted."""
    return unit_dir / ".old" / stamp


def _run_step(step: _migrate.Step, *, unit_dir: Path, stamp: str) -> tuple[bool, str]:
    """Perform one step. Returns ``(ok, detail)``; never raises for a
    step's own failure, so the report can show every outcome rather than
    stopping at the first."""
    if step.action == "verify":
        return True, "deferred to the verification pass"

    if step.argv is not None:
        exe = step.argv[0]
        if shutil.which(exe) is None:
            return False, f"{exe!r} not found on PATH"
        proc = subprocess.run(  # noqa: S603
            list(step.argv), capture_output=True, text=True
        )
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        # `systemctl stop/disable` on a unit systemd never loaded is a
        # no-op we asked for, not a failure: the file is on disk and the
        # daemon has not read it. Treating that as fatal would abort a
        # migration over the very state it is there to clean up.
        if proc.returncode != 0 and step.action in ("stop", "disable"):
            return True, f"already inert ({tail})"
        return proc.returncode == 0, tail

    if step.action == "carry-dropins":
        src = unit_dir / str(step.path)
        dest = unit_dir / str(step.dest)
        if not src.is_dir():
            return True, "no drop-ins to carry"
        dest.mkdir(parents=True, exist_ok=True)
        carried = []
        for conf in sorted(src.iterdir()):
            if conf.is_file():
                target = dest / conf.name
                if target.exists():
                    # ADOPT, never overwrite. A drop-in already at the new
                    # name is the operator's, and it wins.
                    continue
                shutil.copy2(conf, target)
                carried.append(conf.name)
        return True, f"carried {carried or 'nothing (all already present)'}"

    if step.action == "displace":
        src = unit_dir / str(step.path)
        if not src.exists():
            return True, "already gone"
        dest_dir = _displace_dir(unit_dir, stamp)
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest_dir / src.name))
        return True, f"-> {dest_dir / src.name}"

    if step.action == "logging":
        rel = Path(str(step.path))
        target = unit_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        unit = rel.parent.name[: -len(".d")]
        job = unit[: -len(".service")]
        target.write_text(
            _migrate.logging_dropin_text(job, "timer"), encoding="utf-8"
        )
        return True, f"wrote {target}"

    return False, f"no executor for action {step.action!r}"


def _install_argv_factory():
    """Resolve the install delegation through the capability probe.

    So a host with a newer scitex-dev uses the per-kind ``dev timer``
    group and an older one uses ``dev systemd``, with no sac release
    either way. Falls through to the module default when the probe cannot
    decide, which is the same third-state discipline ``resolve`` uses.
    """
    from . import _dev_jobs_backend as backend

    def _argv(rename: _migrate.Rename) -> tuple[str, ...]:
        delegation = backend.resolve(rename.kind, "install")
        if not delegation.supported:
            return _migrate.default_install_argv(rename)
        return tuple(
            backend.build_argv(delegation, name=rename.new, yes=True, dry_run=False)
        )

    return _argv


def _select(only: tuple[str, ...], include_held: bool) -> list[_migrate.Rename]:
    """Which table rows this invocation acts on."""
    if only:
        chosen = [_migrate.by_local(name) for name in only]
    else:
        chosen = list(_migrate.RENAMES)
    out = []
    for rename in chosen:
        if rename.held and not (include_held and only):
            continue
        out.append(rename)
    return out


def register_migrate_command(dev_group: click.Group) -> None:
    """Attach ``sac dev migrate-job-names`` onto ``sac dev``."""

    @dev_group.command(name="migrate-job-names")
    @click.option(
        "--only",
        multiple=True,
        help="Migrate only these jobs (short local names). Repeatable.",
    )
    @click.option(
        "--include-held",
        is_flag=True,
        default=False,
        help=(
            "Also migrate jobs held for a supervised cutover. Requires "
            "--only, so a held job can never be swept up by a bulk run."
        ),
    )
    @click.option(
        "-y",
        "--yes",
        is_flag=True,
        default=False,
        help="Actually perform the migration. Without it this is a dry run.",
    )
    @click.option(
        "--unit-dir",
        type=click.Path(path_type=Path),
        default=None,
        help="systemd --user unit directory. Defaults to the real one.",
    )
    def _migrate_job_names(only, include_held, yes, unit_dir):
        """Cut sac's jobs over to their canonical `scitex-<pkg>-<name>` names.

        \b
        Order is stop -> disable -> carry drop-ins -> displace -> reload ->
        install -> logging -> verify, and install can never precede
        displace: a rename derives a DIFFERENT unit filename, so the two
        would otherwise both supervise the same command.

        \b
        Nothing is deleted. Displaced units go to `.old/<timestamp>/`
        beside them, and a unit's drop-ins are carried across the rename
        rather than orphaned under the old name.
        """
        directory = unit_dir if unit_dir is not None else UNIT_DIR

        if not _migrate.systemd_user_available():
            click.echo(
                "this host has no `systemctl`, so it cannot supervise --user "
                "units at all (measured 2026-08-11: nas-01 is armv7l, nas-02 "
                "has none, mba uses launchd). Nothing to migrate here.",
                err=True,
            )
            raise SystemExit(EXIT_UNSUPPORTED_HOST)

        if include_held and not only:
            click.echo(
                "--include-held requires --only: a held job states a reason it "
                "must be cut over under supervision, and a bulk run that swept "
                "one up by accident is exactly what the hold prevents.",
                err=True,
            )
            raise SystemExit(EXIT_NO_YES)

        renames = _select(tuple(only), include_held)
        present, dropins = _measure(directory)
        steps = _migrate.plan(
            renames,
            present=present,
            dropin_dirs=dropins,
            install_argv=_install_argv_factory(),
            include_held=include_held,
        )

        chosen = _migrate.selection(env=dict(__import__("os").environ), home=Path.home())
        click.echo(_migrate.explain(chosen))
        click.echo(f"unit dir: {directory}")

        held = [r for r in _migrate.RENAMES if r.held and r not in renames]
        for rename in held:
            click.echo(f"HELD {rename.local}: {rename.hold}")

        if not steps:
            click.echo("Nothing to migrate.")
            return

        click.echo(f"\nPlan ({len(steps)} steps):")
        for step in steps:
            click.echo("  " + step.render())

        if not yes:
            click.echo(
                "\nDry run — nothing changed. Re-run with --yes to apply.", err=True
            )
            raise SystemExit(EXIT_NO_YES)

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        click.echo("\nApplying:")
        failed = 0
        for step in steps:
            ok, detail = _run_step(step, unit_dir=directory, stamp=stamp)
            click.echo(f"  {'ok  ' if ok else 'FAIL'} {step.render()}  # {detail}")
            if not ok:
                failed += 1

        subprocess.run(  # noqa: S603
            ["systemctl", "--user", "daemon-reload"], capture_output=True, text=True
        )

        after, _ = _measure(directory)
        click.echo("\nVerification (exactly one supervisor per job):")
        bad = 0
        for rename in renames:
            if rename.held:
                continue
            result = _migrate.verify_exactly_one(rename, present=after)
            click.echo("  " + result.verdict)
            if not result.ok:
                bad += 1

        if failed:
            raise SystemExit(EXIT_STEP_FAILED)
        if bad:
            raise SystemExit(EXIT_VERIFY_FAILED)


__all__ = ["UNIT_DIR", "register_migrate_command"]
