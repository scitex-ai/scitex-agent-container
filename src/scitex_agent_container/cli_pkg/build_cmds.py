"""Build/validation commands: check, validate, build."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

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

    # advise=True: this is THE command that answers "is this spec well-formed?",
    # so authoring lints (long startup_prompts, ...) belong here and nowhere
    # else. They used to fire from load_config itself, which meant `agents list`
    # printed one WARN per offending agent above the table on every run.
    # stx-allow: fallback (reason: load_config may fail post-validation in rare schema-evolution scenarios; CLI exits cleanly)
    try:
        config = load_config(config_path, advise=True)
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

    # Bind SOURCES that are absent on THIS host. Until now `check` said
    # nothing about them and the operator learned of one only at start — an
    # ERROR line from the bind guard, then apptainer's own `FATAL: mount
    # source ... doesn't exist`, rc 255. This is the detection half of a
    # retired host script (~/.local/bin/sac-prune-binds.py); the script's
    # other half DELETED the offending line, which the 2026-08-09 ruling
    # forbids and which this deliberately does not do. See
    # docs/hand-written-script-retirement-20260902.md.
    _warn_absent_bind_sources(config_path, config)

    # raw_args as an APPTAINER ARGV, not merely as YAML. This check said
    # "Ready to deploy" on a spec whose raw_args carried an env assignment
    # with no `--env` before it, minutes before that agent failed to start
    # (2026-08-18). Everything above validates shape and environment; this
    # is the one that reads raw_args the way apptainer will.
    if not _check_raw_args(config):
        all_ok = False

    if all_ok:
        console.print("[green]Ready to deploy.[/green]")
    else:
        console.print(
            "[red]Preflight checks failed. Fix the issues above before deploying.[/red]"
        )
        sys.exit(1)


def _check_raw_args(config) -> bool:
    """Report whether ``spec.apptainer.raw_args`` is a well-formed argv.

    FAILS the preflight rather than warning, because the failure it catches
    is not a deviation the operator might have chosen — a positional in
    raw_args cannot start the agent at all. Returns True when there is
    nothing to say, so a spec with no raw_args is unaffected.
    """
    from ..runtimes._apptainer_argv_guard import ApptainerArgvError, validate_raw_args

    ap = getattr(config, "apptainer", None)
    raw = list(getattr(ap, "raw_args", None) or []) if ap is not None else []
    if not raw:
        console.print(f"  {'raw_args:':30s} [green]OK (none declared)[/green]")
        return True
    try:
        validate_raw_args(raw, agent=getattr(config, "name", None))
    except ApptainerArgvError as exc:
        console.print(f"  {'raw_args:':30s} [red]FAIL[/red]")
        console.print(f"[red]{exc}[/red]")
        return False
    console.print(f"  {'raw_args:':30s} [green]OK ({len(raw)} token(s))[/green]")
    return True


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


def _warn_absent_bind_sources(config_path, config) -> None:
    """Name every ``spec.apptainer.binds`` source that does not exist here.

    REPORTS, NEVER PRUNES, AND NEVER FAILS THE CHECK
        A missing bind source stops a start dead, so the temptation is to
        treat it as a preflight failure and to offer to delete the line. Both
        are wrong here. Deleting is forbidden outright — the operator's
        2026-08-09 ruling is that an absent source is three different problems
        (provision it / carry it with the agent / decide it is obsolete) and
        removing the declaration answers none of them while making the spec
        lie about what the agent needs. Failing is wrong because ``check`` is
        routinely run on one host for a spec that RUNS on another, where a
        host-local source is absent by design and by nobody's mistake; a check
        that goes red for that teaches operators to ignore it.

    Each missing source is classified by :func:`.._lifecycle
    ._relocate_bind_kind.classify_bind` — the same classifier the relocate
    preflight uses — so the line carries WHAT the path is and WHAT to do,
    rather than "path not found" repeated three times for three different
    problems.

    Reads the raw document rather than ``config.apptainer.binds`` because
    :func:`.._listen._inline_spec_preflight.preflight_bind_sources` is the
    package's one answer to "is this bind resolvable?", it takes the raw v3
    shape, and it applies the same ``~``/``$VAR`` expansion the spec parser
    does. Any problem reading the file is swallowed: this is an ADVISORY
    beside a check that has already validated the spec.
    """
    import yaml

    from .._lifecycle._relocate_bind_kind import classify_bind
    from .._listen._inline_spec_preflight import preflight_bind_sources

    try:
        raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except (
        OSError,
        yaml.YAMLError,
    ) as exc:  # stx-allow: fallback (reason: an advisory must not break a check whose spec already validated)
        console.print(f"[dim]  binds: not re-read for source check ({exc})[/dim]")
        return

    result = preflight_bind_sources(raw if isinstance(raw, dict) else {})
    if not result.checks:
        console.print(f"  {'bind sources:':30s} [green]OK (none declared)[/green]")
        return
    if result.ok:
        console.print(
            f"  {'bind sources:':30s} "
            f"[green]OK ({len(result.checks)} present)[/green]"
        )
        return

    from ..config._host import resolve_hostname

    console.print(
        f"  {'bind sources:':30s} "
        f"[yellow]{len(result.unresolvable)} of {len(result.checks)} "
        f"absent on this host[/yellow]"
    )
    workdir = str(getattr(config, "workdir", "") or "")
    here = resolve_hostname()
    for entry in result.unresolvable:
        path = entry.host_resolved or entry.bind
        kind = classify_bind(path, workdir=workdir, from_host=here)
        # soft_wrap: a bind path wrapped across two lines is one the operator
        # cannot grep out of a log — the same reason `agents reconcile` sets it
        # on the agent name.
        console.print(
            f"[yellow]  WARN  {entry.bind}\n"
            f"        {kind.kind} — {kind.action}: {kind.fix}[/yellow]",
            soft_wrap=True,
        )
    console.print(
        "[yellow]        A start will refuse (credential binds) or apptainer "
        "will FATAL. Provision, carry or re-point the source — do not delete "
        "the declaration.[/yellow]"
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
