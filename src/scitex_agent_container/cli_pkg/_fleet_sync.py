"""``sac fleet sync`` — cross-host agent-spec audit (NO auto-merge).

Behaviour
---------
* Read-only: never writes / rsyncs / modifies any agent's spec.yaml or
  to_home tree on any host (sac never picks an authoritative version).
* For every agent under ``~/.scitex/agent-container/agents/<name>/`` on
  the local host and each peer in ``config.yaml``'s ``peers:`` block,
  build a manifest of ``spec.yaml`` + ``to_home/**`` and diff across hosts.
* On ANY disagreement (sha256 / file presence / mode), FAIL LOUD with
  a structured per-host report and a non-zero exit. No fallback, no
  majority-wins rewrite. The operator picks the authoritative copy.
* On any ssh failure (peer unreachable, malformed remote output, peer
  resolve: not yet supported), FAIL LOUD with exit 2 — never produce a
  partial fleet view that could be misread as agreement.

Exit codes
----------
* ``0`` — every agent present on every queried host is bit-identical
* ``1`` — at least one conflict (spec.yaml or to_home/** disagrees)
* ``2`` — ssh / preflight failure (peer unreachable, malformed JSON,
          unresolvable peer without ``--allow-unresolvable``)

CLI worker mode (``--collect``)
-------------------------------
Invoked remotely as ``ssh peer -- sac fleet sync --collect --json``.
The remote process builds its own host's manifest and emits JSON on
stdout; the lead host collates. Operators don't run ``--collect`` by
hand.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import click

from .._state.host_config import Config, build_ssh_argv, load
from .._state.spec_manifest import build_manifest, diff_manifests
from ._helpers import console


_DEFAULT_AGENTS_DIR = "~/.scitex/agent-container/agents"


def _resolve_agents_dir(override: Path | None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    return Path(_DEFAULT_AGENTS_DIR).expanduser()


def _fail_loud_unreachable(
    *,
    peer: str,
    ssh_argv: list[str],
    exit_code: int,
    stderr: str,
    as_json: bool,
    fleet: list[str],
) -> None:
    """Emit a loud "peer unreachable" report and SystemExit(2).

    No silent fallback — we refuse to compute a diff across a partial
    fleet because that could be misread as "everything agrees".
    """
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "exit_code": 2,
                    "fleet": fleet,
                    "unreachable": [
                        {
                            "peer": peer,
                            "ssh_argv": ssh_argv,
                            "exit": exit_code,
                            "stderr": stderr.strip()[:1000],
                        }
                    ],
                    "agents": {},
                },
                indent=2,
            )
        )
    else:
        click.echo("FLEET SPEC SYNC FAILED — peer unreachable", err=True)
        click.echo(f"  peer:    {peer}", err=True)
        click.echo(f"  ssh exit: {exit_code}", err=True)
        click.echo(f"  ssh argv: {' '.join(ssh_argv)}", err=True)
        if stderr.strip():
            click.echo(f"  stderr:  {stderr.strip()[:500]}", err=True)
        click.echo(
            "  (no silent fallback: refusing to compute a partial fleet diff)",
            err=True,
        )
    raise SystemExit(2)


def _fail_loud_unresolvable(
    *,
    peer: str,
    reason: str,
    as_json: bool,
    fleet: list[str],
) -> None:
    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": False,
                    "exit_code": 2,
                    "fleet": fleet,
                    "unreachable": [{"peer": peer, "reason": reason}],
                    "agents": {},
                },
                indent=2,
            )
        )
    else:
        click.echo("FLEET SPEC SYNC FAILED — peer unresolvable", err=True)
        click.echo(f"  peer:   {peer}", err=True)
        click.echo(f"  reason: {reason}", err=True)
        click.echo(
            "  (pass --allow-unresolvable to downgrade this to a warning)",
            err=True,
        )
    raise SystemExit(2)


def _render_text_conflicts(diff: dict[str, Any]) -> None:
    """Loud, operator-readable conflict report. Written on stderr so
    JSON consumers can rely on stdout staying clean."""
    fleet = diff["fleet"]
    unreachable = diff.get("unreachable", [])
    console.print("FLEET SPEC CONFLICT — fail loud, no auto-merge", style="bold red")
    console.print("=" * 72)
    console.print(f"fleet hosts: {', '.join(fleet)}")
    if unreachable:
        console.print("[yellow]warnings (unresolvable peers):[/yellow]")
        for u in unreachable:
            console.print(f"  - {u['peer']}: {u.get('reason', '?')}")
    console.print("")

    conflict_count = 0
    for agent in sorted(diff["agents"].keys()):
        entry = diff["agents"][agent]
        if entry["ok"]:
            continue
        conflict_count += 1
        console.print(f"agent: {agent}", style="bold")
        for c in entry["conflicts"]:
            console.print(f"  file: {c['file']}")
            console.print(f"    kind:       {c['kind']}")
            for h in fleet:
                ph = c["per_host"].get(h)
                if ph is None:
                    continue
                if not ph.get("present"):
                    marker = "<-- DIFFERS" if h in c["diverged_hosts"] else ""
                    console.print(f"    {h:<11} <missing>                                     {marker}")
                    continue
                sha = ph.get("sha256", "")
                size = ph.get("size", "")
                mode = ph.get("mode", "")
                marker = "<-- DIFFERS" if h in c["diverged_hosts"] else ""
                console.print(
                    f"    {h:<11} sha256={sha[:14] + '...' if sha else '':<18} "
                    f"size={size}  mode={mode}   {marker}".rstrip()
                )
        console.print("")
    console.print("=" * 72)
    console.print(
        f"SUMMARY: {conflict_count} agent(s) conflict across {len(fleet)} host(s); refusing to merge.",
        style="bold",
    )
    console.print("Operator action (sac will NEVER do this for you):")
    console.print("  1. Pick the authoritative copy per agent — sac has no opinion.")
    console.print("  2. Rsync that tree to the diverged hosts manually.")
    console.print("  3. Re-run `sac fleet sync` until it exits 0.")
    console.print("=" * 72)


def _fetch_peer_manifest(
    *,
    peer_name: str,
    cfg: Config,
    only: tuple[str, ...],
    as_json: bool,
    fleet: list[str],
) -> dict[str, Any]:
    """Run ``ssh peer -- sac fleet sync --collect`` and parse the manifest.

    Fails loud on any ssh non-zero / malformed JSON. No silent retry.
    """
    remote_argv: list[str] = ["sac", "fleet", "sync", "--collect", "--json"]
    for o in only:
        remote_argv.extend(["--only", o])
    ssh_argv = build_ssh_argv(peer_name, remote_argv, cfg.peers)
    proc = subprocess.run(ssh_argv, capture_output=True, text=True)
    if proc.returncode != 0:
        _fail_loud_unreachable(
            peer=peer_name,
            ssh_argv=ssh_argv,
            exit_code=proc.returncode,
            stderr=proc.stderr,
            as_json=as_json,
            fleet=fleet,
        )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        # Fail loud — never accept a malformed remote response.
        if as_json:
            click.echo(
                json.dumps(
                    {
                        "ok": False,
                        "exit_code": 2,
                        "fleet": fleet,
                        "unreachable": [
                            {
                                "peer": peer_name,
                                "reason": f"malformed JSON from remote: {e}",
                                "raw_stdout_head": proc.stdout[:200],
                            }
                        ],
                        "agents": {},
                    },
                    indent=2,
                )
            )
        else:
            click.echo(
                "FLEET SPEC SYNC FAILED — peer returned malformed JSON",
                err=True,
            )
            click.echo(f"  peer:        {peer_name}", err=True)
            click.echo(f"  parse error: {e}", err=True)
            click.echo(
                f"  raw stdout (first 200 chars): {proc.stdout[:200]!r}",
                err=True,
            )
        raise SystemExit(2)


def _collect_local(
    *,
    cfg: Config,
    agents_dir: Path,
    only: tuple[str, ...],
) -> dict[str, Any]:
    return build_manifest(
        host=cfg.canonical_host(),
        agents_dir=agents_dir,
        only=list(only) if only else None,
    )


def _is_unresolvable(peer_spec) -> bool:
    """Phase-1 unresolvable: ``resolve:`` set + no static ssh target."""
    return (peer_spec.resolve is not None) and not peer_spec.ssh


def fleet_sync_impl(
    *,
    as_json: bool,
    only: tuple[str, ...],
    peer_filter: tuple[str, ...],
    allow_unresolvable: bool,
    collect: bool,
    agents_dir_override: Path | None,
) -> None:
    """Click-decoupled core so the implementation is unit-testable."""
    cfg = load()
    agents_dir = _resolve_agents_dir(agents_dir_override)

    # Worker mode — emit our own manifest as JSON and exit 0.
    if collect:
        manifest = _collect_local(cfg=cfg, agents_dir=agents_dir, only=only)
        click.echo(json.dumps(manifest, indent=2))
        return

    # Lead mode.
    local_host = cfg.canonical_host()
    # Enumerate peers (skip glob patterns — they're synth-only).
    static_peers: list[str] = [
        name for name in cfg.peers.keys() if not any(c in name for c in "*?[")
    ]
    if peer_filter:
        unknown = [p for p in peer_filter if p not in cfg.peers]
        if unknown:
            click.echo(
                f"FLEET SPEC SYNC FAILED — unknown --peer entries: {unknown}",
                err=True,
            )
            raise SystemExit(2)
        peer_names = [p for p in peer_filter if p in static_peers]
    else:
        peer_names = static_peers

    fleet = [local_host] + peer_names
    if len(fleet) < 2:
        # Single-host "fleet" — nothing to diff. Loud about it (not silent OK).
        click.echo(
            "FLEET SPEC SYNC: no peers to compare with — add hosts under "
            "peers: in config.yaml or pass --peer.",
            err=True,
        )
        raise SystemExit(2)

    manifests: dict[str, dict[str, Any]] = {}
    unreachable_warnings: list[dict[str, Any]] = []

    # Local first (so we never accidentally treat the lead host as a peer).
    manifests[local_host] = _collect_local(cfg=cfg, agents_dir=agents_dir, only=only)

    for pname in peer_names:
        pspec = cfg.peer(pname)
        if pspec is None:
            _fail_loud_unreachable(
                peer=pname,
                ssh_argv=[],
                exit_code=2,
                stderr=f"peer '{pname}' has no PeerSpec",
                as_json=as_json,
                fleet=fleet,
            )
        if _is_unresolvable(pspec):
            reason = (
                f"peer has resolve: source={pspec.resolve.source!r} but no "
                "static ssh: target; Phase-1 cannot resolve at dispatch time"
            )
            if not allow_unresolvable:
                _fail_loud_unresolvable(
                    peer=pname, reason=reason, as_json=as_json, fleet=fleet
                )
            unreachable_warnings.append({"peer": pname, "reason": reason})
            continue
        manifests[pname] = _fetch_peer_manifest(
            peer_name=pname,
            cfg=cfg,
            only=only,
            as_json=as_json,
            fleet=fleet,
        )

    diff = diff_manifests(manifests)
    diff["unreachable"] = unreachable_warnings
    if diff["ok"] and not unreachable_warnings:
        diff["exit_code"] = 0
    elif not diff["ok"]:
        diff["exit_code"] = 1
    else:
        # ok but with unreachable warnings — still loud, exit 1.
        diff["exit_code"] = 1

    if as_json:
        click.echo(json.dumps(diff, indent=2))
    else:
        if diff["exit_code"] == 0:
            console.print(
                f"[green]ok[/green]  every agent agrees across "
                f"{len(fleet)} host(s): {', '.join(fleet)}"
            )
        else:
            _render_text_conflicts(diff)

    if diff["exit_code"] != 0:
        raise SystemExit(diff["exit_code"])


@click.command("sync")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
@click.option(
    "--only",
    "only",
    multiple=True,
    help="Restrict the audit to named agent(s); repeatable.",
)
@click.option(
    "--peer",
    "peer_filter",
    multiple=True,
    help="Restrict the audit to named peer(s); repeatable. Default = every "
    "peer in config.yaml.",
)
@click.option(
    "--allow-unresolvable",
    is_flag=True,
    default=False,
    help="Downgrade Phase-1-unresolvable peers (those with resolve: but no "
    "static ssh:) to warnings instead of exiting 2.",
)
@click.option(
    "--collect",
    is_flag=True,
    default=False,
    hidden=True,
    help="Worker mode: emit this host's manifest as JSON and exit. Used "
    "internally by ssh-dispatched calls.",
)
@click.option(
    "--agents-dir",
    "agents_dir_override",
    type=click.Path(path_type=Path),
    default=None,
    help="Override the agents directory (defaults to "
    "~/.scitex/agent-container/agents).",
)
def fleet_sync(
    as_json: bool,
    only: tuple[str, ...],
    peer_filter: tuple[str, ...],
    allow_unresolvable: bool,
    collect: bool,
    agents_dir_override: Path | None,
) -> None:
    """Cross-host agent-spec audit; fails loud on any disagreement.

    \b
    Audits ``~/.scitex/agent-container/agents/<name>/{spec.yaml,
    to_home/**}`` on the local host AND every peer under ``peers:`` in
    config.yaml. ANY divergence (content, presence, or mode) exits
    non-zero with a per-host report — sac NEVER auto-merges.

    \b
    Examples:
      $ sac fleet sync                       # audit every agent on every host
      $ sac fleet sync --only myagent --json # narrow + machine-readable
      $ sac fleet sync --peer spartan        # only diff local vs. spartan
    """
    fleet_sync_impl(
        as_json=as_json,
        only=only,
        peer_filter=peer_filter,
        allow_unresolvable=allow_unresolvable,
        collect=collect,
        agents_dir_override=agents_dir_override,
    )


__all__ = ["fleet_sync"]
