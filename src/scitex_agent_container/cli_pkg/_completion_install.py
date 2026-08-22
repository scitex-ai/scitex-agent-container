#!/usr/bin/env python3
# File: src/scitex_agent_container/cli_pkg/_completion_install.py

"""Cache-file install for shell completion — extracted from ``_main.py``.

scitex-dev's ``attach_shell_completion`` writes an
``eval "$(_NAME_COMPLETE=... )"`` line into ~/.bashrc for ONE binary. Two
problems for sac:

1. We ship TWO binaries (``scitex-agent-container`` and ``sac``); click's
   completion is keyed on argv[0], so each name needs its own script.
2. The eval line invokes the binary on every shell start (~0.4 s per
   binary; nine scitex eval lines cost ~3.6 s of shell latency).

:func:`install_completion_cache` replaces that callback with a cache-file
install: generate each binary's completion script once, write it under
``~/.scitex/agent-container/runtime/completion/<binary>``, symlink it into
the XDG bash-completion dir, and have the rc file merely ``source`` it
(microseconds).

This lives beside ``_main.py`` rather than inside it because it is a
different job — it touches ``$HOME``, ``subprocess`` and the user's rc
file, while ``_main.py`` only resolves subcommands.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import click

__all__ = ["install_completion_cache"]

_BINARIES = (
    ("scitex-agent-container", "_SCITEX_AGENT_CONTAINER_COMPLETE"),
    ("sac", "_SAC_COMPLETE"),
)
_SOURCE_MAP = {"bash": "bash_source", "zsh": "zsh_source"}


def _tracked_in_a_git_repo(path: Path) -> Path | None:
    """The repo root when ``path`` is a TRACKED file in a git repo, else None.

    MEASURED 2026-08-20, found by the dotfiles agent while building the fleet
    git sync. On every fleet host `~/.bashrc` is a symlink:

        /home/ywatanabe/.bashrc -> /home/ywatanabe/.dotfiles/src/.bashrc

    so appending to it writes a TRACKED file in the dotfiles repo. Four hosts
    ended up carrying the same "local edit" — not four humans, one installer.
    Those checkouts are permanently dirty, an ff-only pull will not apply, and
    that is a direct contributor to the fleet running five different dotfiles
    heads.

    The path is RESOLVED first, because the symlink is the whole mechanism: the
    file we would open is not the file the name refers to.

    `git ls-files --error-unmatch` rather than `git status`, because the
    question is "is this file under version control", not "is it currently
    modified" — an unmodified tracked file is exactly as wrong to append to,
    and would silently become the modified one.
    """
    import subprocess

    real = path.resolve()
    try:
        root = subprocess.run(
            ["git", "-C", str(real.parent), "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=10,
        )
        if root.returncode != 0:
            return None
        tracked = subprocess.run(
            ["git", "-C", str(real.parent), "ls-files", "--error-unmatch", str(real)],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        # An unusable git is not evidence that the file is untracked, but it is
        # also not a reason to block a local install. Fall through to appending
        # and say nothing false.
        return None
    if tracked.returncode != 0:
        return None
    return Path(root.stdout.strip())


def _install_cached(*args, **kwargs) -> None:
    """Replacement callback for ``install-shell-completion``."""
    # Re-resolve Path.home() each call (not at attach time) so $HOME
    # changes between invocations are honoured (matters for tests under
    # tmp_path, and for users who run with a custom HOME via env-prefix).
    # Primary: sac-owned, under runtime/ per local-state-directories §4b.
    sac_cache_dir = (
        Path.home() / ".scitex" / "agent-container" / "runtime" / "completion"
    )
    # Secondary: XDG bash-completion dir (where third-party tooling
    # auto-discovers); kept as a symlink to the sac-owned file so both
    # paths point at the same content.
    xdg_cache_dir = Path.home() / ".local" / "share" / "bash-completion" / "scitex"
    shell = kwargs.get("shell", "bash")
    dry_run = kwargs.get("dry_run", False)
    if shell not in _SOURCE_MAP:
        click.echo(
            f"error: cache install supports bash/zsh; got {shell!r}", err=True
        )
        return
    rc_path = Path.home() / (".bashrc" if shell == "bash" else ".zshrc")

    for binary, env_var in _BINARIES:
        cache_path = sac_cache_dir / binary
        xdg_link = xdg_cache_dir / binary
        source_line = f"[ -f {cache_path} ] && source {cache_path}"
        marker = f"# sac-completion: {binary}"

        if dry_run:
            click.echo(f"Would write {cache_path} ({binary} completions)")
            click.echo(f"Would symlink {xdg_link} -> {cache_path}")
            click.echo(f"Would append to {rc_path}: {source_line}  {marker}")
            continue

        # Generate static script via the binary itself.
        env = os.environ.copy()
        env[env_var] = _SOURCE_MAP[shell]
        result = subprocess.run([binary], capture_output=True, text=True, env=env)
        if result.returncode != 0 or not result.stdout.strip():
            click.echo(
                f"warn: failed to generate completion for {binary}", err=True
            )
            continue
        sac_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(result.stdout)

        # XDG symlink for auto-discovery (idempotent).
        xdg_cache_dir.mkdir(parents=True, exist_ok=True)
        if xdg_link.is_symlink() or xdg_link.exists():
            xdg_link.unlink()
        xdg_link.symlink_to(cache_path)

        # Add the source line in rc if not already present.
        if rc_path.is_file() and marker in rc_path.read_text():
            continue
        repo = _tracked_in_a_git_repo(rc_path)
        if repo is not None:
            # REFUSE, and say exactly what to add and where. The line belongs
            # IN the dotfiles repo — committed once and carried to every host
            # by the sync that already exists — not appended per host forever
            # by an installer that makes the checkout dirty each time.
            click.echo(
                f"refusing to append to {rc_path}: it resolves to "
                f"{rc_path.resolve()}, a file tracked in {repo}. Appending "
                f"would leave that checkout permanently dirty and block an "
                f"ff-only pull, on this host and on every host that installs "
                f"sac.\n"
                f"  Add this line to that file yourself and commit it, so it "
                f"reaches every host once:\n"
                f"    {source_line}  {marker}",
                err=True,
            )
            continue
        with rc_path.open("a") as fh:
            fh.write(f"\n{source_line}  {marker}\n")
        click.echo(f"Tab completion installed: {cache_path}")
        click.echo(f"  XDG symlink: {xdg_link}")

    if not dry_run:
        click.echo(f"Run: source {rc_path}")


def install_completion_cache(group) -> None:
    """Point ``group``'s ``install-shell-completion`` at the cache install.

    No-op when the command is absent — ``attach_shell_completion`` is
    optional (it needs scitex-dev[cli-audit]), and a missing completion
    command must not break CLI startup.
    """
    cmd = group.commands.get("install-shell-completion")
    if cmd is None:
        return
    cmd.callback = _install_cached


# EOF
