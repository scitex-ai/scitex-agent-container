"""Install commands: ``sac install --boot`` and ``sac install-post-merge-cron``.

Deliverables:
  * ``sac install --boot`` — first-time host bootstrap (venv, dirs, PATH)
  * ``sac install-post-merge-cron`` — add/remove crontab entry for
    post-merge-pull.sh
"""

from __future__ import annotations

import importlib.resources
import shutil
import subprocess
import sys
from pathlib import Path

import click

from ._helpers import console

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_SHARED_DIRS = [
    "~/.scitex/orochi/shared/agents",
    "~/.scitex/orochi/shared/skills",
    "~/.scitex/orochi/shared/logs",
    "~/.scitex/orochi/shared/cron",
]

_CRON_SCRIPT_NAME = "post-merge-pull.sh"
_CRON_SCRIPT_DEST = (
    Path("~/.scitex/orochi/shared/cron").expanduser() / _CRON_SCRIPT_NAME
)
_CRON_LOG_PATTERN = (
    "~/.scitex/orochi/shared/logs/post-merge-pull.$(hostname -s).cron.log"
)

_CRON_MARKER = "post-merge-pull"


def _cron_line() -> str:
    return f"* * * * * {_CRON_SCRIPT_DEST} >> {_CRON_LOG_PATTERN} 2>&1"


# ---------------------------------------------------------------------------
# install group
# ---------------------------------------------------------------------------


@click.group("install")
def install_group() -> None:
    """Bootstrap and install helpers for a new fleet host."""


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
      5. Create ~/.scitex/orochi/shared/{agents,skills,logs,cron}/.
      6. Copy bundled post-merge-pull.sh to the cron dir (chmod +x).
      7. Print "boot OK" and the installed sac version.

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
        console.print(f"{tag}venv [green]already exists[/green]: {venv_dir}")
    else:
        python = _find_python311()
        if python is None:
            console.print(
                "[red]Error:[/red] python3.11+ not found on PATH. "
                "Install it via your package manager (e.g. apt install python3.11).",
                err=True,
            )
            sys.exit(1)
        console.print(f"{tag}Creating venv at {venv_dir} with {python}…")
        if not dry_run:
            subprocess.run([python, "-m", "venv", str(venv_dir)], check=True)

    # ------------------------------------------------------------------
    # Step 2 — pip install -e <this package>
    # ------------------------------------------------------------------
    sac_src = _find_sac_src()
    pip = venv_dir / "bin" / "pip"
    if not dry_run and venv_dir.exists():
        console.print(f"{tag}Installing sac into venv…")
        subprocess.run([str(pip), "install", "--quiet", "-e", str(sac_src)], check=True)
    else:
        console.print(f"{tag}Would run: {pip} install -e {sac_src}")

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
            console.print(f"{tag}PATH already set in {rc_path}")
        else:
            console.print(f"{tag}Adding {bin_dir} to {rc_path}…")
            if not dry_run:
                rc_path.write_text(content + path_line)

    # ------------------------------------------------------------------
    # Step 4 — tmux check
    # ------------------------------------------------------------------
    if shutil.which("tmux") is None:
        console.print(
            "[yellow]WARNING:[/yellow] tmux not found on PATH. "
            "Install it with your package manager:\n"
            "  Ubuntu/Debian: sudo apt install tmux\n"
            "  macOS:         brew install tmux\n"
            "  RHEL/Rocky:    sudo dnf install tmux"
        )
    else:
        tmux_ver = subprocess.run(
            ["tmux", "-V"], capture_output=True, text=True
        ).stdout.strip()
        console.print(f"{tag}tmux [green]OK[/green]: {tmux_ver}")

    # ------------------------------------------------------------------
    # Step 5 — shared dirs
    # ------------------------------------------------------------------
    for d in _SHARED_DIRS:
        p = Path(d).expanduser()
        if p.exists():
            console.print(f"{tag}dir [green]exists[/green]: {p}")
        else:
            console.print(f"{tag}Creating {p}…")
            if not dry_run:
                p.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Step 6 — copy cron script
    # ------------------------------------------------------------------
    _deploy_cron_script(dry_run=dry_run, tag=tag)

    # ------------------------------------------------------------------
    # Step 7 — summary
    # ------------------------------------------------------------------
    if dry_run:
        console.print("[dim]dry-run complete — no changes were made.[/dim]")
    else:
        try:
            sac_bin = venv_dir / "bin" / "sac"
            ver = subprocess.run(
                [str(sac_bin), "--version"], capture_output=True, text=True
            ).stdout.strip()
        except Exception:
            ver = "(version unavailable)"
        console.print(f"[green bold]boot OK[/green bold] — {ver}")
        console.print(
            "[dim]Re-source your shell or open a new terminal to pick up the PATH change.[/dim]"
        )


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
        console.print(
            f"[red]Error:[/red] bundled {_CRON_SCRIPT_NAME} not found at {src_path}",
            err=True,
        )
        return

    if dest.exists() and dest.read_bytes() == src_path.read_bytes():
        console.print(f"{tag}cron script [green]up-to-date[/green]: {dest}")
    else:
        console.print(f"{tag}Deploying {_CRON_SCRIPT_NAME} → {dest}…")
        if not dry_run:
            shutil.copy2(str(src_path), str(dest))
            dest.chmod(0o755)


# ---------------------------------------------------------------------------
# sac install-post-merge-cron
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
      $ sac install-post-merge-cron
      $ sac install-post-merge-cron --dry-run
      $ sac install-post-merge-cron --uninstall
    """
    if not dry_run and not yes:
        action = "Remove" if uninstall else "Install"
        if not click.confirm(f"{action} post-merge-pull cron entry?", default=True):
            click.echo("Aborted.")
            return
    if dry_run and uninstall:
        click.echo("Error: --dry-run and --uninstall are mutually exclusive.", err=True)
        sys.exit(2)

    cron_line = _cron_line()

    # Read current crontab.
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode not in (0, 1):
        console.print(f"[red]Error reading crontab:[/red] {result.stderr.strip()}")
        sys.exit(1)
    current = result.stdout if result.returncode == 0 else ""
    lines = current.splitlines(keepends=True)

    already_present = any(_CRON_MARKER in line for line in lines)

    if uninstall:
        if not already_present:
            console.print(
                "[dim]No post-merge-pull entry in crontab — nothing to remove.[/dim]"
            )
            return
        new_lines = [l for l in lines if _CRON_MARKER not in l]
        _write_crontab(new_lines)
        console.print("[green]Removed[/green] post-merge-pull from crontab.")
        return

    if dry_run:
        console.print("[bold]Would add to crontab:[/bold]")
        click.echo(cron_line)
        if already_present:
            console.print("[dim](line already present — would be a no-op)[/dim]")
        return

    if already_present:
        console.print("[dim]post-merge-pull already in crontab — no-op.[/dim]")
        return

    # Ensure cron script is executable.
    if not _CRON_SCRIPT_DEST.exists():
        console.print(
            f"[yellow]WARNING:[/yellow] {_CRON_SCRIPT_DEST} not found. "
            "Run `sac install boot` first to deploy the script."
        )

    new_content = current.rstrip("\n") + ("\n" if current else "") + cron_line + "\n"
    _write_crontab_str(new_content)
    console.print(f"[green]Added[/green] to crontab:\n  {cron_line}")


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
        console.print(f"[red]Error writing crontab:[/red] {proc.stderr.strip()}")
        sys.exit(1)
