"""``sac fleet sync-code`` — cross-host checkout drift audit (NO auto-merge).

Behaviour
---------
* Read-only: never pulls / merges / installs anything on any host.
  The eco_sync.sh triple gate (clean + develop + 0 ahead) stays the
  operator's explicit action; this command only REPORTS drift.
* For ``~/.dotfiles`` + every ``~/proj/scitex-*`` checkout + the
  installed ``sac`` version, build a per-host manifest (git HEAD,
  branch, tracked-dirty flag, ahead/behind vs origin) and diff across
  the fleet.
* On ANY drift, FAIL LOUD with a structured per-host report and a
  non-zero exit. No fallback, no majority-wins. The operator picks the
  authoritative copy (or runs the pull).
* On any ssh failure, FAIL LOUD with exit 2 — never produce a partial
  fleet view that could be misread as agreement.

Exit codes
----------
* ``0`` — every checkout agrees across every queried host
* ``1`` — at least one drift (sha / dirty / ahead / branch / version)
* ``2`` — ssh / preflight failure

CLI worker mode (``--collect``)
-------------------------------
Invoked remotely as ``ssh peer -- sac fleet sync-code --collect``.
The remote process collects its own host's git state and emits JSON on
stdout; the lead host collates. Operators don't run ``--collect`` by
hand.
"""

from __future__ import annotations

import scitex_logging as slogging
from .._logging import render_rich
import json
import subprocess
from typing import Any

import click

from .._state.host_config import Config, build_ssh_argv, load
from .._state.checkout_manifest import diff_checkout_manifests
from ._fleet_sync import _fail_loud_unreachable, _is_unresolvable, _fail_loud_unresolvable
from ._fleet_sync_code_collect import collect_checkout_state, emit_collect

log = slogging.getLogger(__name__)


def _fetch_peer_manifest(
    *,
    peer_name: str,
    cfg: Config,
    as_json: bool,
    fleet: list[str],
) -> dict[str, Any]:
    """Run ``ssh peer -- sac fleet sync-code --collect`` and parse."""
    argv = build_ssh_argv(peer_name, ["sac", "fleet", "sync-code", "--collect"], cfg.peers)
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    if proc.returncode not in (0, 2):
        _fail_loud_unreachable(
            peer=peer_name,
            ssh_argv=argv,
            exit_code=proc.returncode,
            stderr=proc.stderr,
            as_json=as_json,
            fleet=fleet,
        )
    try:
        manifest = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        _fail_loud_unreachable(
            peer=peer_name,
            ssh_argv=argv,
            exit_code=proc.returncode,
            stderr=f"malformed JSON from --collect: {exc}\n{proc.stdout[:500]}",
            as_json=as_json,
            fleet=fleet,
        )
    return manifest


def _render_text_drift(diff: dict[str, Any]) -> None:
    render_rich("[bold red]FLEET CHECKOUT DRIFT[/bold red]", __name__)
    fleet = diff.get("fleet", [])
    render_rich(f"  fleet: {', '.join(fleet)}", __name__)
    dot = diff.get("dotfiles", {})
    if not dot.get("ok", True):
        render_rich(
            f"  [yellow]dotfiles drift[/yellow] diverged={dot.get('diverged_hosts', [])}",
            __name__,
        )
        for h, sha in dot.get("per_host", {}).items():
            render_rich(f"    {h}: {(sha or 'absent')[:9]}", __name__)
    sac_v = diff.get("sac_version", {})
    if not sac_v.get("ok", True):
        render_rich(
            f"  [yellow]sac version drift[/yellow] diverged={sac_v.get('diverged_hosts', [])}",
            __name__,
        )
        for h, v in sac_v.get("per_host", {}).items():
            render_rich(f"    {h}: {v or 'absent'}", __name__)
    for pkg, info in diff.get("packages", {}).items():
        if info.get("ok", True):
            continue
        render_rich(f"  [yellow]{pkg}[/yellow]", __name__)
        for c in info.get("conflicts", []):
            render_rich(
                f"    {c['kind']} diverged={c.get('diverged_hosts', [])}",
                __name__,
            )


def _sync_code_impl(
    *,
    as_json: bool,
    only: tuple[str, ...],
    peer_filter: tuple[str, ...],
    allow_unresolvable: bool,
    collect: bool,
) -> None:
    """Click-decoupled core so the implementation is unit-testable."""
    from .. import __version__ as sac_version

    if collect:
        raise SystemExit(emit_collect(sac_version))

    cfg = load()
    local_host = cfg.canonical_host()
    static_peers: list[str] = [
        name for name in cfg.peers.keys() if not any(c in name for c in "*?[")
    ]
    if peer_filter:
        unknown = [p for p in peer_filter if p not in cfg.peers]
        if unknown:
            click.echo(
                f"FLEET CHECKOUT SYNC FAILED — unknown --peer entries: {unknown}",
                err=True,
            )
            raise SystemExit(2)
        peer_names = [p for p in peer_filter if p in static_peers]
    else:
        peer_names = static_peers

    fleet = [local_host] + peer_names
    if len(fleet) < 2:
        click.echo(
            "FLEET CHECKOUT SYNC: no peers to compare with — add hosts under "
            "peers: in config.yaml or pass --peer.",
            err=True,
        )
        raise SystemExit(2)

    manifests: dict[str, dict[str, Any]] = {}
    unreachable_warnings: list[dict[str, Any]] = []

    # Local first.
    from .._state.checkout_manifest import build_checkout_manifest

    local_state = collect_checkout_state()
    manifests[local_host] = build_checkout_manifest(
        host=local_host,
        dotfiles_sha=local_state["dotfiles_sha"],
        sac_version=sac_version,
        checkouts=local_state["checkouts"],
        errors=local_state["errors"],
    )
    if only:
        for m in manifests.values():
            m["checkouts"] = {k: v for k, v in m["checkouts"].items() if k in only}

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
        assert pspec is not None  # narrowed by _fail_loud_unreachable above
        if _is_unresolvable(pspec):
            pspec_resolve = pspec.resolve  # type: ignore[union-attr]
            src = pspec_resolve.source if pspec_resolve is not None else "?"
            reason = (
                f"peer has resolve: source={src!r} but no "
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
            as_json=as_json,
            fleet=fleet,
        )

    diff = diff_checkout_manifests(manifests)
    diff["unreachable"] = unreachable_warnings
    if diff["ok"] and not unreachable_warnings:
        diff["exit_code"] = 0
    else:
        diff["exit_code"] = 1

    if as_json:
        click.echo(json.dumps(diff, indent=2))
    else:
        if diff["exit_code"] == 0:
            render_rich(
                f"[green]ok[/green]  every checkout agrees across "
                f"{len(fleet)} host(s): {', '.join(fleet)}",
                __name__,
            )
        else:
            _render_text_drift(diff)

    if diff["exit_code"] != 0:
        raise SystemExit(diff["exit_code"])


@click.command("sync-code")
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
@click.option(
    "--only",
    "only",
    multiple=True,
    help="Restrict the audit to named package(s); repeatable.",
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
    help="Downgrade Phase-1-unresolvable peers to warnings instead of exiting 2.",
)
@click.option(
    "--collect",
    is_flag=True,
    default=False,
    hidden=True,
    help="Worker mode: emit this host's manifest as JSON and exit. Used "
    "internally by ssh-dispatched calls.",
)
def fleet_sync_code(
    as_json: bool,
    only: tuple[str, ...],
    peer_filter: tuple[str, ...],
    allow_unresolvable: bool,
    collect: bool,
) -> None:
    """Audit checkout drift across the fleet (read-only, no auto-merge)."""
    _sync_code_impl(
        as_json=as_json,
        only=only,
        peer_filter=peer_filter,
        allow_unresolvable=allow_unresolvable,
        collect=collect,
    )


# EOF
