"""Build/validation commands: check, validate, build."""

from __future__ import annotations

import shutil
import subprocess
import sys

import click

from ..config import load_config, resolve_config, validate_config
from ._helpers import agent_name_complete, console


@click.command()
@click.argument("name_or_path", type=str, shell_complete=agent_name_complete)
def check(name_or_path: str) -> None:
    """Run preflight checks for an agent deployment.

    Validates the YAML spec, then probes runtime dependencies
    (container backend, python). Accepts either a bare agent name
    (resolved against the search chain) or an explicit path to
    ``spec.yaml``.

    \b
    Example:
      $ sac agent check orchestrator
      $ sac agent check ~/.scitex/agent-container/agents/foo/spec.yaml
    """
    # stx-allow: fallback (reason: config file may not exist or contain invalid YAML; CLI exits with code 1 to signal preflight failure)
    try:
        config_path = resolve_config(name_or_path)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)

    errors = validate_config(config_path)
    if errors:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)

    # stx-allow: fallback (reason: load_config may fail post-validation in rare schema-evolution scenarios; CLI exits cleanly)
    try:
        config = load_config(config_path)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"[blue]Checking {config.name} ({config.runtime or 'apptainer'})...[/blue]"
    )

    all_ok = True

    # ``runtime`` selects the SAC execution path (for example ``tui``); it is
    # not an executable name. Apptainer is the sole container backend since
    # the 2026-05-13 backend ripout, including for TUI sessions.
    backend = "apptainer"
    backend_bin = shutil.which(backend)
    if backend_bin:
        console.print(f"  {backend + ':':30s} [green]OK ({backend_bin})[/green]")
    else:
        all_ok = False
        console.print(f"  {backend + ':':30s} [red]FAIL ({backend} not found)[/red]")

    # Python (used by hooks / pre-start scripts)
    try:
        proc = subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            console.print(
                f"  {'python:':30s} [green]OK ({proc.stdout.strip()})[/green]"
            )
        else:
            all_ok = False
            console.print(f"  {'python:':30s} [red]FAIL[/red]")
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        all_ok = False
        console.print(f"  {'python:':30s} [red]FAIL (python3 not found)[/red]")

    # D4 — warn (don't fail) on bind targets that mirror host paths.
    # Container-canonical roots are /srv/, /work/, /opt/, /data/. See
    # docs/adr/0001-isolation-hardening.md §D4.
    _warn_host_mirroring_bind_targets(config)

    if all_ok:
        console.print("[green]Ready to deploy.[/green]")
    else:
        console.print(
            "[red]Preflight checks failed. Fix the issues above before deploying.[/red]"
        )
        sys.exit(1)


# Bind targets that start with these prefixes mirror host home / user
# directories. ADR D4: container-canonical targets must live under
# /srv/, /work/, /opt/, /data/.
_HOST_MIRRORING_TARGET_PREFIXES = ("/home/", "/Users/", "/root/")


def _warn_host_mirroring_bind_targets(config) -> None:
    """Emit a non-fatal warning for each bind whose target mirrors a host path.

    See ``docs/adr/0001-isolation-hardening.md`` §D4. The
    operator may have HPC reasons to keep mirroring (e.g. cross-host
    path stability for shared filesystems) so this never fails the
    check — just makes the deviation visible.
    """
    ap = getattr(config, "apptainer", None)
    if ap is None:
        return
    binds = list(getattr(ap, "binds", None) or [])
    for bind in binds:
        target = _bind_target(str(bind))
        if not target:
            continue
        if any(target.startswith(p) for p in _HOST_MIRRORING_TARGET_PREFIXES):
            console.print(
                f"[yellow]WARN  {config.name}: bind target {target} mirrors a "
                f"host path; container-canonical convention is /srv/, /work/, "
                f"/opt/, /data/.\n       See "
                f"docs/adr/0001-isolation-hardening.md (D4).[/yellow]"
            )


def _bind_target(bind: str) -> str:
    """Return the container-side target of a ``host:target[:mode]`` bind string.

    Apptainer accepts both ``host:target`` and ``host:target:mode``; we
    parse with the same heuristic the runtime applies (the trailing
    token is a mode only if it's exactly ``ro`` or ``rw``).
    """
    parts = bind.split(":")
    if len(parts) < 2:
        return ""
    if len(parts) >= 3 and parts[-1] in {"ro", "rw"}:
        return parts[-2]
    return parts[1]


@click.command()
@click.argument("name_or_path", type=str)
def validate(name_or_path: str) -> None:
    """Validate a YAML config file.

    Accepts either a bare agent name (resolved against the search chain)
    or an explicit path to ``spec.yaml``.

    \b
    Example:
      $ sac agent validate orchestrator
      $ sac agent validate ~/.scitex/agent-container/agents/foo/spec.yaml
    """
    try:
        config_path = resolve_config(name_or_path)
    except Exception as exc:  # stx-allow: fallback (reason: not-found / unresolvable name surfaced to user)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)
    errors = validate_config(config_path)
    if not errors:
        console.print(f"[green]Config is valid: {config_path}[/green]")
    else:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)


# NOTE: the legacy `sac build-image` command lived here and supported
# Docker + Apptainer side-by-side. Both build paths have been removed
# in the 2026-05-13 docker/podman ripout — the canonical builder is
# now `sac image build` (in `image_group.py`), which delegates to
# `scitex-container` and emits Apptainer SIFs only.
