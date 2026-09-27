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

from ..._logging import render_rich
from ..._state.host_config import build_ssh_argv
from ..._state.host_config import load as _load_host_config
from ..._state.registry import Registry
from ..._state.state_store import record_instance_stop
from .._helpers import agent_name_complete
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


def _delete_probe(name: str) -> dict:
    """Scan every place ``name`` can exist, without mutating anything.

    PUBLIC-ISH ON PURPOSE (module-internal shared core): the existence
    scan behind BOTH ``sac agents delete`` and the Agents GUI delete
    flow, so the two cannot disagree about "not found" vs "remote" vs
    "dangling link". Pure reads — safe to call from the web process.
    """
    root = Path.home() / ".scitex" / "agent-container"
    agents_root = root / "agents"
    runtime_root = root / "runtime"
    registry = Registry()
    spec_dir = agents_root / name
    rt_dir = runtime_root / name
    # An agent that lives only on a peer (rsync skipped during
    # start, or the lead spec was already deleted) still has a row
    # in state.db; we must count that as "exists" so the delete
    # doesn't no-op when it should ssh.
    remote_row = lookup_remote_peer(name)
    # An instances-only ORPHAN (active row in the shared store, but the
    # spec dir, runtime dir and registry pin are all gone — e.g. a start
    # that died in container_creation, or a local delete that predates
    # row-closing) still shows up in the fleet, so it must count as
    # "exists" here; _delete_one closes the row. Without this the fleet
    # shows a row the delete verb calls "not found" (seen 2026-09-28 on
    # compute-03: agent 'fb' listed, delete 404'd).
    from ..._state.state_store import list_active_instances

    local_rows = [r for r in list_active_instances() if r.get("name") == name]
    local_row = local_rows[0] if local_rows else None
    # A DANGLING spec link is the case this verb most needs to handle: the
    # authority half of a deletion landed, so the link's target is gone and
    # ``spec_dir.exists()`` is False. Measured 2026-09-19 on compute-03 —
    # two links left by exactly that sequence reported "not found" and the
    # only cleaner for them was a plain rm. ``is_symlink()`` is True even
    # when the target is missing, so it is the correct existence test.
    spec_is_link = spec_dir.is_symlink()
    spec_is_dangling = spec_is_link and not spec_dir.exists()
    existed_anywhere = (
        spec_dir.exists()
        or spec_is_link
        or rt_dir.exists()
        or registry.exists(name)
        or remote_row is not None
        or local_row is not None
    )
    return {
        "spec_dir": spec_dir,
        "rt_dir": rt_dir,
        "spec_is_link": spec_is_link,
        "spec_is_dangling": spec_is_dangling,
        "spec_exists": spec_dir.exists(),
        "runtime_exists": rt_dir.exists(),
        "registry_exists": registry.exists(name),
        "remote_row": remote_row,
        "local_row": local_row,
        "existed_anywhere": existed_anywhere,
    }


def _delete_one(name: str, *, keep_runtime: bool = False) -> dict:
    """Delete one agent — stop, deregister, remove dirs. THE shared core.

    PUBLIC ON PURPOSE: the same backend behind ``sac agents delete``
    and the Agents GUI delete flow. Cross-host remote dispatch failures
    raise :class:`RuntimeError` (fail loud — a half-deleted remote is
    worse than no delete); a name that exists nowhere raises
    :class:`LookupError`. Per-file ``OSError`` races are collected into
    the ``warnings`` list, never swallowed. Returns the outcome
    envelope (``deleted=True`` when the removal ran).
    """
    import shutil as _shutil

    probe = _delete_probe(name)
    if not probe["existed_anywhere"]:
        raise LookupError(
            f"'{name}': not found (no spec, runtime, registry, or instances)"
        )
    spec_dir = probe["spec_dir"]
    rt_dir = probe["rt_dir"]
    warnings: list[str] = []

    # 0. Cross-host: if the agent is on a peer, stop + rm there,
    # then close the lead-side row. Failures surface (no silent
    # fallback) — a half-deleted remote is worse than no delete.
    remote = False
    if probe["remote_row"] is not None:
        _dispatch_remote_delete(name)
        remote = True

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

    # 2. Spec dir. A DANGLING link is removed as a LINK — rmtree cannot
    # resolve it, and the link itself is the debris that half-done
    # deletions leave behind.
    spec_removed = False
    if probe["spec_is_dangling"]:
        # stx-allow: fallback (unlink may race with a concurrent relink;
        # we report and continue rather than abort the batch)
        try:
            spec_dir.unlink()
            spec_removed = True
        except OSError as exc:
            warnings.append(
                f"could not remove dangling link {spec_dir}: {exc}"
            )
    elif spec_dir.exists():
        # stx-allow: fallback (rmtree may race with a concurrent
        # writer; we report and continue rather than abort the batch)
        try:
            _shutil.rmtree(spec_dir)
            spec_removed = True
        except OSError as exc:
            warnings.append(f"could not remove {spec_dir}: {exc}")

    # 3. Runtime dir.
    runtime_removed = False
    if not keep_runtime and rt_dir.exists():
        # stx-allow: fallback (see spec-dir rmtree above)
        try:
            _shutil.rmtree(rt_dir)
            runtime_removed = True
        except OSError as exc:
            warnings.append(f"could not remove {rt_dir}: {exc}")

    # 4. Registry.
    # stx-allow: fallback (registry.remove may raise on already-gone
    # entry depending on backend; the agent is already off disk)
    try:
        Registry().remove(name)
    except Exception:
        pass

    # 5. Close any active local instances rows. The remote path above
    # already closes the lead-side row; the local path never did, so a
    # spec/runtime/registry removal left the shared-store row active and
    # the fleet kept listing a ghost the next delete called "not found".
    # Closing here makes local delete converge the same way and lets an
    # instances-only orphan delete cleanly (row closed, dirs already gone).
    # stx-allow: fallback (a row that vanishes between probe and close is
    # already gone — the desired end state, not an error)
    try:
        from ..._state.state_store import list_active_instances

        for row in list_active_instances():
            if row.get("name") != name:
                continue
            instance_id = row.get("id")
            if not instance_id:
                continue
            try:
                record_instance_stop(instance_id, exit_reason="deleted")
            except Exception:
                pass
    except Exception:
        pass

    return {
        "name": name,
        "deleted": True,
        "remote": remote,
        "spec_removed": spec_removed,
        "runtime_removed": runtime_removed,
        "warnings": warnings,
    }


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
            f"Agent {name!r} active on peer {peer!r} per the shared store, but "
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
        probe = _delete_probe(name)
        remote_row = probe["remote_row"]
        spec_is_dangling = probe["spec_is_dangling"]
        if not probe["existed_anywhere"]:
            click.echo(f"[skip] '{name}': not found (no spec, runtime, registry, or instances)")
            any_err = True
            continue

        if dry_run:
            remote_marker = f" remote={remote_row[0]}" if remote_row is not None else ""
            dangling_marker = " DANGLING (target missing)" if spec_is_dangling else ""
            click.echo(
                f"[dry-run] would delete '{name}': "
                f"spec={spec_dir.exists()}{dangling_marker} runtime={rt_dir.exists() and not keep_runtime} "
                f"registry={registry.exists(name)}{remote_marker}"
            )
            continue

        try:
            envelope = _delete_one(name, keep_runtime=keep_runtime)
        except RuntimeError as exc:
            any_err = True
            click.echo(f"[error] '{name}': {exc}", err=True)
            continue
        for warning in envelope["warnings"]:
            click.echo(f"[warn] '{name}': {warning}")
            any_err = True

        render_rich(f"[green]deleted[/green] {name}", __name__)

    if any_err:
        sys.exit(1)


__all__ = ["_delete_one", "_delete_probe", "delete"]
