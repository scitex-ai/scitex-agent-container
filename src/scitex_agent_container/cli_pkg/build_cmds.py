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

    # raw_args as an APPTAINER ARGV, not merely as YAML. This check said
    # "Ready to deploy" on a spec whose raw_args carried an env assignment
    # with no `--env` before it, minutes before that agent failed to start
    # (2026-08-18). Everything above validates shape and environment; this
    # is the one that reads raw_args the way apptainer will.
    if not _check_raw_args(config):
        all_ok = False

    # `host:` as a ROUTE, not merely a string. Every runtime path already
    # refuses an unroutable pin; until this call existed the PREFLIGHT was the
    # one place that said "Ready to deploy" about an agent nobody could start.
    if not _check_host_route(config):
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


def _check_host_route(config) -> bool:
    """Report whether ``spec.host`` names a machine sac can dispatch to.

    FAILS the preflight rather than warning, for the same reason
    :func:`_check_raw_args` does: every runtime path already treats an
    unroutable ``host:`` as fatal, so passing one is not a deviation the
    operator might have chosen. It is a wrong answer to the only question this
    command exists to ask.

    MEASURED 2026-09-05. Two live specs pinned ``host: scitex-02`` and
    ``host: scitex-01``. Both names were retired on 2026-08-12 when the peer
    table was re-keyed to ``scitex-compute-0N`` (config.yaml records why: the
    short forms resolve nowhere in DNS). The re-key updated the registry and
    left those two specs pointing at names that no longer exist::

        sac agents start <agent>
            -> spec.host is neither this machine nor a registered peer
        agent_spawn <agent>
            -> ssh: Could not resolve hostname scitex-02 (rc=255)
        sac agents check <agent>
            -> exit 0, "Ready to deploy."          <-- the bug this closes

    Both runtime paths failed correctly and loudly for two months. Only the
    preflight was green, so an agent that COULD NOT start read as one that
    simply had not. A peer's card waited on a review from one of them for two
    months and was nearly closed as "the agent did not respond" rather than
    "the agent could not be started" -- two very different records.

    NO REACHABILITY PROBE, deliberately. The resolver is called without an
    oracle, so a registered peer that is merely DOWN stays UNKNOWN, and UNKNOWN
    never rejects (see :mod:`.lifecycle._host_chain`). This command answers "is
    this spec well-formed?"; "is that machine up right now?" is
    ``sac host probe``'s question. Folding them would cost an ssh round-trip
    per check and fail preflights on a transient blip -- turning a spec
    validator into a fleet monitor.

    Degrades to WARN when the peer table or this machine's own hostname cannot
    be resolved. With no registry to judge against there is no EVIDENCE of a
    bad name, and rejecting on absent evidence is the exact UNKNOWN-collapse
    the routing modules forbid.
    """
    from .lifecycle._host_chain import UNROUTABLE, chain_hosts

    spec_host = getattr(getattr(config, "hosts_spec", None), "host", None)
    if not chain_hosts(spec_host):
        console.print(
            f"  {'host:':30s} [green]OK (unpinned - starts on this machine)"
            f"[/green]"
        )
        return True

    # stx-allow: fallback (reason: an unloadable peer table or unresolvable
    # local hostname is absence of evidence, not evidence of a bad pin; the
    # check degrades to a warning rather than rejecting a spec it cannot judge)
    try:
        from .._state.host_config import load as _load_host_config
        from ..config._host import resolve_hostname
        from .lifecycle._host_identity import _local_host_names

        peers = _load_host_config().peers
        current_host = resolve_hostname()
        local_names = _local_host_names(current_host)
    except Exception as exc:
        console.print(
            f"  {'host:':30s} [yellow]WARN (cannot verify pin: {exc})[/yellow]"
        )
        return True

    from .lifecycle._host_routing import (
        format_route_error,
        resolve_spec_host_route,
    )

    route = resolve_spec_host_route(
        spec_host, current_host, peers, local_names=local_names
    )
    if route.kind == UNROUTABLE and not peers:
        # An EMPTY peer table cannot convict a name. On a machine that has
        # never been given a `peers:` section every non-local pin would
        # otherwise fail here, turning \"this fleet is not configured yet\"
        # into \"your spec is wrong\" -- the misattributed-error shape this
        # whole check exists to remove.
        console.print(
            f"  {'host:':30s} [yellow]WARN (not this machine, and no peers "
            f"are registered to check it against)[/yellow]"
        )
        return True

    if route.kind == UNROUTABLE:
        console.print(f"  {'host:':30s} [red]FAIL[/red]")
        console.print(
            "[red]"
            + format_route_error(
                config.name,
                spec_host,
                route,
                peers,
                verb="start",
                current_host=current_host or "",
                local_names=local_names,
            )
            + "[/red]"
        )
        return False

    where = "this machine" if route.kind == "local" else f"peer {route.peer}"
    console.print(
        f"  {'host:':30s} [green]OK ({route.host} - {where})[/green]"
    )
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
