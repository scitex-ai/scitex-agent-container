#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents delete`` — stop + deregister + remove agent dirs.

Cross-host: when the active ``state.db.instances`` row records
``host != current_host``, delete ssh's into the peer to stop the
remote agent and ``rm -rf`` the remote spec dir, then removes the
lead-side instances row + local spec/runtime/registry as usual.
"""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import click

from ..._state.host_config import build_ssh_argv
from ..._state.host_config import load as _load_host_config
from ..._state.registry import Registry
from ..._state.state_db import record_instance_stop
from .._helpers import agent_name_complete, console
from ._dispatch import lookup_remote_peer


def _delete_via_host_listen(names: tuple[str, ...]) -> None:
    """In-SIF DELETE proxy — one HTTP DELETE per name → outcome JSON.

    PR-3 Checkpoint 3 — the path the CLI takes when running inside
    an apptainer SIF (= a SAC-from-SAC child agent's workspace).
    Each name's outcome is emitted as one JSON line to stdout in
    the wire-stable :func:`_in_sif_outcome.outcome_to_stdout_json`
    shape. Process exit code is the maximum of the per-name
    outcome exit codes (highest = worst case per the table), so
    the calling script sees the most actionable failure code for
    the batch.

    Never raises — every failure mode (transport, ACL deny, marker
    stillborn, unknown kind) is mapped into an outcome with the
    structured ``kind`` tag the consumer branches on.
    """
    from ..._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )
    from ..._lifecycle._in_sif_outcome import (
        build_outcome,
        outcome_to_stdout_json,
        transport_outcome,
    )

    worst_exit = 0
    for name in names:
        try:
            status, body = host_listen_call("DELETE", f"/agents/{name}")
            outcome = build_outcome(http_status=status, body=body)
        except HostListenTransportError as exc:
            outcome = transport_outcome(str(exc), url=exc.url)
        sys.stdout.write(outcome_to_stdout_json(outcome))
        worst_exit = max(worst_exit, outcome.exit_code)
    sys.exit(worst_exit)


def _dispatch_remote_delete(name: str) -> bool:
    """SSH into the peer that owns ``name`` to stop + rm + close row.

    Returns ``True`` when dispatched; the caller may still want to scrub
    any local spec/runtime/registry remnants on the lead. Returns
    ``False`` when no remote row exists.

    Raises:
        RuntimeError: When the resolved peer is not in ``peers.yaml``,
            or any of the remote ssh calls fail. No silent fallback —
            a delete that fails mid-way must surface so the operator
            sees the partial state.
    """
    found = lookup_remote_peer(name)
    if found is None:
        return False
    peer, row = found
    peers = _load_host_config().peers
    if peer not in peers:
        raise RuntimeError(
            f"Agent {name!r} active on peer {peer!r} per state.db, but "
            f"{peer!r} is NOT in ~/.scitex/agent-container/config.yaml's "
            f"peers: section. Cannot delete cross-host without an ssh "
            f"target. Add the peer entry and retry."
        )
    # 1. Remote stop (force, ignore-on-missing). We use --force so a
    # remote registry that's already drifted past the running state
    # doesn't abort the delete.
    stop_argv = build_ssh_argv(peer, ["sac", "agents", "stop", name, "--force"], peers)
    stop_proc = subprocess.run(stop_argv, capture_output=True, text=True, check=False)
    # Don't raise on stop failure — the agent may already be stopped.
    # But surface stderr so the operator can correlate.
    if stop_proc.returncode != 0:
        click.echo(
            f"[delete] remote stop on {peer!r} returned rc={stop_proc.returncode}; "
            f"continuing with rm. stderr: {(stop_proc.stderr or '').strip()[:200]}",
            err=True,
        )

    # 2. Remote rm -rf the spec dir. Be explicit about the path —
    # `~` expands on the remote, no shell metacharacter injection
    # because `name` is the same string the operator passed.
    rm_path = f"~/.scitex/agent-container/agents/{name}/"
    rm_argv = build_ssh_argv(peer, ["rm", "-rf", rm_path], peers)
    rm_proc = subprocess.run(rm_argv, capture_output=True, text=True, check=False)
    if rm_proc.returncode != 0:
        raise RuntimeError(
            f"Remote `rm -rf {rm_path}` failed on {peer!r} "
            f"(rc={rm_proc.returncode}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in rm_argv)}\n"
            f"stderr:\n{rm_proc.stderr}"
        )

    # 3. Close the lead-side instances row.
    instance_id = row.get("id")
    if instance_id:
        record_instance_stop(instance_id, exit_reason="deleted")
    click.echo(f"[delete] removed {name!r} on peer {peer!r}")
    return True


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

    # PR-3 — in-SIF auto-fallback. When the CLI is running inside an
    # apptainer SIF (= the SAC-from-SAC architecture: a child agent's
    # workspace), the local filesystem doesn't carry the host registry
    # (each SIF has its own ~/.scitex/agent-container/), and there is
    # no useful local pid file to SIGTERM. Auto-proxy the operation to
    # the host's ``sac listen`` server via the env-injected
    # SAC_LISTEN_BASE_URL + SAC_LISTEN_BEARER. The lineage-scoped ACL
    # gate on the host side enforces that the caller can only DELETE
    # itself or its lineage descendants — same wire shape (5-kind +
    # transport) as the in-process gate. Result is one
    # InSifOutcome JSON line per name to stdout; exit code is the
    # highest seen (worst-case mapping per the table) so a batch DELETE
    # surfaces the most actionable failure code to the calling script.
    from ..._lifecycle._in_sif_broker import is_in_sif

    if is_in_sif() and not dry_run:
        _delete_via_host_listen(names)
        return  # noreturn — _delete_via_host_listen sys.exits

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
        # An agent that lives only on a peer (rsync skipped during
        # start, or the lead spec was already deleted) still has a row
        # in state.db; we must count that as "exists" so the delete
        # doesn't no-op when it should ssh.
        remote_row = lookup_remote_peer(name)
        existed_anywhere = (
            spec_dir.exists()
            or rt_dir.exists()
            or registry.exists(name)
            or remote_row is not None
        )
        if not existed_anywhere:
            click.echo(f"[skip] '{name}': not found (no spec, runtime, or registry)")
            any_err = True
            continue

        if dry_run:
            remote_marker = f" remote={remote_row[0]}" if remote_row is not None else ""
            click.echo(
                f"[dry-run] would delete '{name}': "
                f"spec={spec_dir.exists()} runtime={rt_dir.exists() and not keep_runtime} "
                f"registry={registry.exists(name)}{remote_marker}"
            )
            continue

        # 0. Cross-host: if the agent is on a peer, stop + rm there,
        # then close the lead-side row. Failures surface (no silent
        # fallback) — a half-deleted remote is worse than no delete.
        if remote_row is not None:
            try:
                _dispatch_remote_delete(name)
            except RuntimeError as exc:
                any_err = True
                click.echo(f"[error] '{name}': {exc}", err=True)
                continue

        # 1. Best-effort local stop. We don't care if it wasn't running.
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
