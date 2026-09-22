"""Install commands: ``sac install --boot`` and ``sac installation setup-cron``.

Deliverables:
  * ``sac install --boot`` — first-time host bootstrap (venv, dirs, PATH)
  * ``sac installation setup-cron`` — add/remove crontab entry for
    post-merge-pull.sh
"""

from __future__ import annotations

import scitex_logging as slogging
from .._logging import render_rich
import importlib.resources
import shutil
import subprocess
import sys
from pathlib import Path

import click

from . import _installation_check

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

log = slogging.getLogger(__name__)

_SHARED_DIRS = [
    "~/.scitex/agent-container/agents",
    "~/.scitex/agent-container/skills",
    "~/.scitex/agent-container/runtime/logs",
    "~/.scitex/agent-container/runtime/cron",
]

_CRON_SCRIPT_NAME = "post-merge-pull.sh"
_CRON_SCRIPT_DEST = (
    Path("~/.scitex/agent-container/runtime/cron").expanduser() / _CRON_SCRIPT_NAME
)
_CRON_LOG_PATTERN = (
    "~/.scitex/agent-container/runtime/logs/post-merge-pull.$(hostname -s).cron.log"
)

_CRON_MARKER = "post-merge-pull"


def _cron_line() -> str:
    return f"* * * * * {_CRON_SCRIPT_DEST} >> {_CRON_LOG_PATTERN} 2>&1"


# ---------------------------------------------------------------------------
# install group
# ---------------------------------------------------------------------------


@click.group("installation")
def install_group() -> None:
    """Bootstrap and install helpers for a new fleet host.

    \b
    Create an install:  boot, setup-cron
    Verify one:         check  (read-only — dead/shadowed editable
                        pointers, orphaned or duplicated dist-info)
    """


# ---------------------------------------------------------------------------
# sac install --boot
# ---------------------------------------------------------------------------


@install_group.command("boot")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print what would be done without making any changes.",
)
def boot(dry_run: bool) -> None:  # noqa: C901
    """First-time host bootstrap: venv, PATH, shared dirs, cron script.

    Safe to re-run — every step is idempotent.

    \b
    Steps:
      1. Create ~/.venv-3.11 (python3.11+) if missing.
      2. pip install -e <this package> into the venv.
      3. Add ~/.venv-3.11/bin to ~/.bashrc and ~/.zshrc (if they exist).
      4. Verify tmux is on PATH (prints instructions if missing).
      5. Create ~/.scitex/agent-container/{agents,skills,runtime/{logs,cron}}/.
      6. Copy bundled post-merge-pull.sh to the cron dir (chmod +x).
      7. Apply AND ARM every job this package declares (see below).
      8. Print "boot OK" and the installed sac version.

    \b
    Step 7 is the BASE CASE of the declared-job invariant, and it is the
    reason this command is the right place for it. A JobSpec in the repo
    is DECLARED; the `scitex_dev.jobs` entry point makes it REGISTERED;
    neither of those puts anything on a host. The step that does — write
    the unit, then ENABLE it — had no caller at all until 2026-08-15, so
    every host was armed by hand and a rebuilt one started bare. It
    cannot be fixed by declaring a convergence timer, because nothing
    would arm THAT timer either; the recursion needs a step that runs
    unconditionally on a host with nothing, which is this one. The
    periodic half that re-asserts the invariant afterwards belongs to
    scitex-dev, so that one job converges every registered package
    instead of each package shipping its own copy.

    \b
    Example:
      $ sac install boot
      $ sac install boot --dry-run
    """
    tag = "[dim][dry-run][/dim] " if dry_run else ""

    # ------------------------------------------------------------------
    # Step 1 — venv
    # ------------------------------------------------------------------
    venv_dir = Path("~/.venv-3.11").expanduser()
    if venv_dir.exists():
        render_rich(f"{tag}venv [green]already exists[/green]: {venv_dir}", __name__)
    else:
        python = _find_python311()
        if python is None:
            log.error("Error: python3.11+ not found on PATH. "
                "Install it via your package manager (e.g. apt install python3.11).")
            sys.exit(1)
        render_rich(f"{tag}Creating venv at {venv_dir} with {python}…", __name__)
        if not dry_run:
            subprocess.run([python, "-m", "venv", str(venv_dir)], check=True)

    # ------------------------------------------------------------------
    # Step 2 — pip install -e <this package>
    # ------------------------------------------------------------------
    sac_src = _find_sac_src()
    pip = venv_dir / "bin" / "pip"
    if not dry_run and venv_dir.exists():
        render_rich(f"{tag}Installing sac into venv…", __name__)
        subprocess.run([str(pip), "install", "--quiet", "-e", str(sac_src)], check=True)
    else:
        render_rich(f"{tag}Would run: {pip} install -e {sac_src}", __name__)

    # ------------------------------------------------------------------
    # Step 3 — PATH injection
    # ------------------------------------------------------------------
    bin_dir = str(venv_dir / "bin")
    path_line = f'\nexport PATH="{bin_dir}:$PATH"  # added by sac install --boot\n'
    for rc in ["~/.bashrc", "~/.zshrc"]:
        rc_path = Path(rc).expanduser()
        if not rc_path.exists():
            continue
        content = rc_path.read_text()
        if bin_dir in content:
            render_rich(f"{tag}PATH already set in {rc_path}", __name__)
        else:
            render_rich(f"{tag}Adding {bin_dir} to {rc_path}…", __name__)
            if not dry_run:
                rc_path.write_text(content + path_line)

    # ------------------------------------------------------------------
    # Step 4 — tmux check
    # ------------------------------------------------------------------
    if shutil.which("tmux") is None:
        render_rich("[yellow]WARNING:[/yellow] tmux not found on PATH. "
            "Install it with your package manager:\n"
            "  Ubuntu/Debian: sudo apt install tmux\n"
            "  macOS:         brew install tmux\n"
            "  RHEL/Rocky:    sudo dnf install tmux", __name__)
    else:
        tmux_ver = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True
        ).stdout.strip()
        render_rich(f"{tag}tmux [green]OK[/green]: {tmux_ver}", __name__)

    # ------------------------------------------------------------------
    # Step 5 — shared dirs
    # ------------------------------------------------------------------
    for d in _SHARED_DIRS:
        p = Path(d).expanduser()
        if p.exists():
            render_rich(f"{tag}dir [green]exists[/green]: {p}", __name__)
        else:
            render_rich(f"{tag}Creating {p}…", __name__)
            if not dry_run:
                p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 6 — copy cron script
    # ------------------------------------------------------------------
    _deploy_cron_script(dry_run=dry_run, tag=tag)

    # ------------------------------------------------------------------
    # Step 7 — apply + ARM every job this package declares
    # ------------------------------------------------------------------
    _apply_declared_jobs_step(dry_run=dry_run, tag=tag)

    # ------------------------------------------------------------------
    # Step 8 — summary
    # ------------------------------------------------------------------
    if dry_run:
        render_rich("[dim]dry-run complete — no changes were made.[/dim]", __name__)
    else:
        try:
            sac_bin = venv_dir / "bin" / "sac"
            ver = subprocess.run(
                [str(sac_bin), "--version"], capture_output=True, text=True
            ).stdout.strip()
        except Exception:
            ver = "(version unavailable)"
        render_rich(f"[green bold]boot OK[/green bold] — {ver}", __name__)
        render_rich("[dim]Re-source your shell or open a new terminal to pick up the PATH change.[/dim]", __name__)


def _apply_declared_jobs_step(dry_run: bool, tag: str) -> None:
    """Turn this package's DECLARED jobs into APPLIED, ARMED units.

    Four states, and the whole point of this step is that the last two do
    not follow from the first two: a JobSpec in the repo is DECLARED, the
    ``scitex_dev.jobs`` entry point makes it REGISTERED, a unit file on
    this host makes it APPLIED, and ``systemctl enable`` makes it ARMED.
    Only ARMED fires. Nothing in sac produced either of the last two
    until this step existed.

    Delegated wholesale to :func:`._dev_jobs_apply.apply_declared_jobs`,
    which routes every verb through scitex-dev. Nothing here knows what a
    unit file is.

    NEVER FATAL. A failure to arm is reported loudly and does not abort
    the bootstrap: the venv, PATH and shared dirs from steps 1-6 are what
    every other recovery path needs, and throwing them away because a
    timer would not enable would turn a degraded host into an unusable
    one. The report is the deliverable — an unarmed job that is COUNTED
    is recoverable; an unarmed job nobody counted is the defect this step
    exists to end.
    """
    from ._dev_jobs_apply import apply_declared_jobs

    render_rich(f"{tag}Applying + arming declared jobs…", __name__)
    try:
        report = apply_declared_jobs(
            yes=not dry_run,
            dry_run=dry_run,
            echo=lambda line: render_rich(f"{tag}{line}", __name__),
        )
    except Exception as exc:  # noqa: BLE001 — see NEVER FATAL above
        render_rich(f"[yellow]WARNING:[/yellow] could not apply declared jobs: {exc}\n"
            "  Steps 1-6 completed. Re-run `sac installation boot` once "
            "scitex-dev is healthy, or apply by hand with "
            "`sac dev timer install --yes && sac dev timer enable --yes`.", __name__)
        return

    render_rich(f"{tag}jobs: {report.summary()}", __name__)
    for line in report.skipped:
        render_rich(f"[yellow]WARNING:[/yellow] skipped {line}", __name__)
    for step in report.failed:
        render_rich(f"[red]FAILED:[/red] {step}", __name__)
    if report.failed:
        # Naming the remedy matters more than the failure: `install`
        # refusing because a supervisor already exists is a SAFE outcome,
        # while `enable` failing means the job genuinely will not fire.
        render_rich("[dim]  `install` may refuse when a supervisor already exists — "
            "that is safe. A failed `enable` is not: the job will not "
            "fire.[/dim]", __name__)


def _find_python311() -> str | None:
    for candidate in ("python3.11", "python3.12", "python3.13", "python3"):
        path = shutil.which(candidate)
        if path is None:
            continue
        result = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info >= (3,11))"],
            capture_output=True,
            text=True,
        )
        if result.stdout.strip() == "True":
            return path
    return None


def _find_sac_src() -> Path:
    """Return the root directory of the scitex-agent-container package."""
    try:
        import scitex_agent_container as _pkg

        pkg_path = Path(_pkg.__file__).parent
        # Walk up to pyproject.toml
        for parent in [pkg_path, pkg_path.parent, pkg_path.parent.parent]:
            if (parent / "pyproject.toml").exists():
                return parent
    except ImportError:
        pass
    return Path(__file__).parent.parent.parent.parent


def _deploy_cron_script(dry_run: bool, tag: str) -> None:
    """Copy bundled post-merge-pull.sh to the shared cron dir."""
    dest = _CRON_SCRIPT_DEST
    if not dry_run:
        dest.parent.mkdir(parents=True, exist_ok=True)

    # Locate the bundled script via package resources.
    try:
        src = importlib.resources.files("scitex_agent_container.cron").joinpath(
            _CRON_SCRIPT_NAME
        )
        src_path = Path(str(src))
    except Exception:
        # Fallback: relative to this file.
        src_path = Path(__file__).parent.parent / "cron" / _CRON_SCRIPT_NAME

    if not src_path.exists():
        log.error(f"Error: bundled {_CRON_SCRIPT_NAME} not found at {src_path}")
        return

    if dest.exists() and dest.read_bytes() == src_path.read_bytes():
        render_rich(f"{tag}cron script [green]up-to-date[/green]: {dest}", __name__)
    else:
        render_rich(f"{tag}Deploying {_CRON_SCRIPT_NAME} → {dest}…", __name__)
        if not dry_run:
            shutil.copy2(str(src_path), str(dest))
            dest.chmod(0o755)


# ---------------------------------------------------------------------------
# sac installation setup-cron
# ---------------------------------------------------------------------------


@click.command("install-post-merge-cron")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the crontab line without modifying crontab.",
)
@click.option(
    "--uninstall",
    is_flag=True,
    default=False,
    help="Remove the post-merge-pull cron entry if present.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def install_post_merge_cron(dry_run: bool, uninstall: bool, yes: bool) -> None:
    """Add (or remove) the post-merge-pull crontab entry.

    Idempotent: re-running when the line already exists is a no-op.
    Requires post-merge-pull.sh to be deployed first
    (run ``sac install boot`` to do that).

    \b
    Example:
      $ sac installation setup-cron
      $ sac installation setup-cron --dry-run
      $ sac installation setup-cron --uninstall
    """
    if not dry_run and not yes:
        action = "remove" if uninstall else "install"
        click.echo(
            f"Refusing to {action} post-merge-pull cron entry without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)
    if dry_run and uninstall:
        click.echo("Error: --dry-run and --uninstall are mutually exclusive.", err=True)
        sys.exit(2)

    cron_line = _cron_line()

    # Read current crontab.
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        render_rich(f"[red]Error reading crontab:[/red] {result.stderr.strip()}", __name__)
        sys.exit(1)
    current = result.stdout if result.returncode == 0 else ""
    lines = current.splitlines(keepends=True)

    already_present = any(_CRON_MARKER in line for line in lines)

    if uninstall:
        if not already_present:
            render_rich("[dim]No post-merge-pull entry in crontab — nothing to remove.[/dim]", __name__)
            return
        new_lines = [l for l in lines if _CRON_MARKER not in l]
        _write_crontab(new_lines)
        render_rich("[green]Removed[/green] post-merge-pull from crontab.", __name__)
        return

    if dry_run:
        render_rich("[bold]Would add to crontab:[/bold]", __name__)
        click.echo(cron_line)
        if already_present:
            render_rich("[dim](line already present — would be a no-op)[/dim]", __name__)
        return

    if already_present:
        render_rich("[dim]post-merge-pull already in crontab — no-op.[/dim]", __name__)
        return

    # Ensure cron script is executable.
    if not _CRON_SCRIPT_DEST.exists():
        render_rich(f"[yellow]WARNING:[/yellow] {_CRON_SCRIPT_DEST} not found. "
            "Run `sac install boot` first to deploy the script.", __name__)

    new_content = current.rstrip("\n") + ("\n" if current else "") + cron_line + "\n"
    _write_crontab_str(new_content)
    render_rich(f"[green]Added[/green] to crontab:\n  {cron_line}", __name__)


def _write_crontab(lines: list[str]) -> None:
    _write_crontab_str("".join(lines))


def _write_crontab_str(content: str) -> None:
    proc = subprocess.run(
        ["crontab", "-"],
        input=content,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        render_rich(f"[red]Error writing crontab:[/red] {proc.stderr.strip()}", __name__)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Sub-verb wiring
# ---------------------------------------------------------------------------
# Done at module level (rather than in cli_pkg/_main.py) so the lazy
# loader resolves install_group with `setup-cron` already attached.
install_group.add_command(
    click.Command(
        name="setup-cron",
        callback=install_post_merge_cron.callback,
        params=list(install_post_merge_cron.params),
        help=install_post_merge_cron.help,
        short_help=install_post_merge_cron.short_help,
        epilog=install_post_merge_cron.epilog,
    )
)

# The read-only half of the noun: `boot` and `setup-cron` CREATE an
# install, `check` asks whether an existing one still describes reality.
# Attached by its own module's register(), mirroring how `worktree_group`
# collects `_worktree_gc`.
_installation_check.register(install_group)
