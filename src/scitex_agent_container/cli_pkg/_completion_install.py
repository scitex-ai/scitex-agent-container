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
