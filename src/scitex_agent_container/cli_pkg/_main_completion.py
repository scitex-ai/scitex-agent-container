"""Cache-based shell-completion install for sac's two binaries.

Split out of :mod:`._main` along a real seam. ``_main`` owns the CLI
SURFACE — the lazy command table, the short-help mirror, the renamed
commands, the entry point. This module owns one unrelated job: turning
click's completion support into a cache-file install.

It is the only part of the CLI module that reaches for ``subprocess``,
``os`` and the user's ``~/.bashrc``, which is what makes it a separable
responsibility rather than an arbitrary cut.

Why a cache file at all
-----------------------
The upstream helper writes an ``eval "$(_NAME_COMPLETE=...)"`` line in
``~/.bashrc`` for ONE binary. Two problems for sac:

1. We ship TWO binaries (``scitex-agent-container`` and ``sac``), and
   click's completion is keyed on ``argv[0]``, so each name needs its own
   registration.
2. The eval line invokes the binary on every shell start (~0.4 s per
   binary; 9 scitex eval lines = ~3.6 s of ``source ~/.bashrc`` latency).

So the static script is written once to
``~/.scitex/agent-container/runtime/completion/<binary>`` and ``~/.bashrc``
merely ``source``s it — microseconds instead of 0.4 s.
"""

from __future__ import annotations

import click

#: The binaries sac ships, with the env var click uses to emit each
#: one's completion script.
BINARIES = (
    ("scitex-agent-container", "_SCITEX_AGENT_CONTAINER_COMPLETE"),
    ("sac", "_SAC_COMPLETE"),
)

SOURCE_MAP = {"bash": "bash_source", "zsh": "zsh_source"}


def attach_completion(group) -> None:
    """Attach scitex-dev's completion commands, then swap in the cache install.

    ``scitex-dev[cli-audit]`` is optional and its import chain pulls the
    linter graph (~490 ms), so this is called only when one of the
    completion command names is actually resolved.
    """
    try:
        from scitex_dev._cli._completion import attach_shell_completion
    except Exception:  # stx-allow: fallback (reason: scitex-dev[cli-audit] is optional; broaden beyond ImportError so a misbuilt transitive dep can't break CLI startup)
        # scitex-dev[cli-audit] not installed (or its import chain raised
        # something other than ImportError); completion commands stay
        # missing — non-fatal for the CLI itself.
        return
    attach_shell_completion(group, prog_name="scitex-agent-container")
    install_shell_completion_cache_based(group)


def install_shell_completion_cache_based(group) -> None:
    """Replace ``install-shell-completion`` with a cache-file install.

    Writes generated completion scripts (one per binary) to
    ``~/.scitex/agent-container/runtime/completion/<binary>``, symlinks
    them into the XDG bash-completion dir for third-party discovery, and
    appends ``source`` lines to the shell rc. The source op is
    O(microseconds); the eval-the-binary op was O(0.4 s).
    """
    import os
    import subprocess
    from pathlib import Path

    cmd = group.commands.get("install-shell-completion")
    if cmd is None:
        return

    def install_cached(*args, **kwargs):
        # Re-resolve Path.home() each call (not at attach time) so $HOME
        # changes between invocations are honoured (matters for tests
        # under tmp_path, and for users who run with a custom HOME via
        # env-prefix).
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
        if shell not in SOURCE_MAP:
            click.echo(
                f"error: cache install supports bash/zsh; got {shell!r}", err=True
            )
            return
        rc_path = Path.home() / (".bashrc" if shell == "bash" else ".zshrc")

        for binary, env_var in BINARIES:
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
            env[env_var] = SOURCE_MAP[shell]
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

    cmd.callback = install_cached
