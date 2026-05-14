#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents delete`` — stop + deregister + remove agent dirs."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ..._state.registry import Registry
from .._helpers import agent_name_complete, console


@click.command()
@click.argument(
    "names",
    type=str,
    nargs=-1,
    required=True,
    shell_complete=agent_name_complete,
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be deleted without removing anything.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the bulk-delete confirmation gate (required when len(NAMES) > 1).",
)
@click.option(
    "--keep-runtime",
    "keep_runtime",
    is_flag=True,
    default=False,
    help="Keep the per-agent runtime/ dir (logs, session.jsonl, quota). "
    "Default: remove it along with the spec dir.",
)
def delete(
    names: tuple[str, ...],
    dry_run: bool,
    yes: bool,
    keep_runtime: bool,
) -> None:
    """Delete one or more agents — stop, deregister, and remove their dirs.

    For each NAME this:
      1. Stops the agent if running (best-effort; missing/stopped is fine).
      2. Removes the spec dir at ``~/.scitex/agent-container/agents/<name>/``.
      3. Removes the runtime state dir at ``~/.scitex/agent-container/runtime/<name>/``
         unless ``--keep-runtime`` is given.
      4. Drops the registry entry.

    \b
    Example:
      $ sac agent delete hello-agent
      $ sac agent delete hello-agent-1 hello-agent-2 hello-agent-3 -y
      $ sac agent delete hello-agent --dry-run
      $ sac agent delete hello-agent --keep-runtime
    """
    import shutil as _shutil

    if len(names) > 1 and not yes and not dry_run:
        click.echo(
            f"Refusing to delete {len(names)} agents without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)

    root = Path.home() / ".scitex" / "agent-container"
    agents_root = root / "agents"
    runtime_root = root / "runtime"
    registry = Registry()
    any_err = False

    for name in names:
        spec_dir = agents_root / name
        rt_dir = runtime_root / name
        existed_anywhere = spec_dir.exists() or rt_dir.exists() or registry.exists(name)
        if not existed_anywhere:
            click.echo(f"[skip] '{name}': not found (no spec, runtime, or registry)")
            any_err = True
            continue

        if dry_run:
            click.echo(
                f"[dry-run] would delete '{name}': "
                f"spec={spec_dir.exists()} runtime={rt_dir.exists() and not keep_runtime} "
                f"registry={registry.exists(name)}"
            )
            continue

        # 1. Best-effort stop. We don't care if it wasn't running.
        # stx-allow: fallback (stop-on-delete is best-effort; a missing
        # config or already-stopped agent must not block the delete)
        try:
            from ..._lifecycle.lifecycle import agent_stop

            cfg_yaml = spec_dir / "spec.yaml"
            if cfg_yaml.is_file():
                agent_stop(str(cfg_yaml), force=True)
        except Exception:
            pass

        # 2. Spec dir.
        if spec_dir.exists():
            # stx-allow: fallback (rmtree may race with a concurrent
            # writer; we report and continue rather than abort the batch)
            try:
                _shutil.rmtree(spec_dir)
            except OSError as exc:
                click.echo(f"[warn] '{name}': could not remove {spec_dir}: {exc}")
                any_err = True

        # 3. Runtime dir.
        if not keep_runtime and rt_dir.exists():
            # stx-allow: fallback (see spec-dir rmtree above)
            try:
                _shutil.rmtree(rt_dir)
            except OSError as exc:
                click.echo(f"[warn] '{name}': could not remove {rt_dir}: {exc}")
                any_err = True

        # 4. Registry.
        # stx-allow: fallback (registry.remove may raise on already-gone
        # entry depending on backend; the agent is already off disk)
        try:
            registry.remove(name)
        except Exception:
            pass

        console.print(f"[green]deleted[/green] {name}")

    if any_err:
        sys.exit(1)


__all__ = ["delete"]
