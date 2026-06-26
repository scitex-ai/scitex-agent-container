"""``sac registry sync`` — ADR-0014 anti-entropy for ``comms_nodes``.

Pulls / pushes the ``comms_nodes`` slice of state.db between hosts via
ssh + the existing ``sac db export`` / ``sac db import`` primitives.
No new transport, no new auth: ssh trust + ``INSERT OR IGNORE`` on the
``name`` PK gives idempotent convergence (Consul/etcd anti-entropy
shape, applied to a single small table).

Behaviour:

* ``--from PEER``: ssh to PEER, run ``sac db export --tables
  comms_nodes`` remotely, parse the JSON, mark every row's
  ``source_host`` to PEER's canonical hostname (extracted from the
  payload's ``host`` field), import locally via :func:`import_state`.
* ``--to PEER``: reverse — export locally, pipe to remote
  ``sac db import -``.
* ``--all``: walk every static (non-glob) entry in ``peers:``, pull
  from each, then push to each. Per-peer errors are logged and do not
  abort the run; the overall exit code reflects whether any peer failed.
* ``--dry-run``: log the plan; no ssh, no DB writes.

Trust boundary: the receiving host accepts whatever ssh delivers
(operator-managed ``~/.ssh/known_hosts``). The same envelope already
underlies ``sac fleet sync`` (PR #207) — re-using the path keeps the
cross-host attack surface unchanged.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Iterable

import click

from .._state.host_config import Config, build_ssh_argv, load

# Hard per-peer ssh wall-clock ceiling. ``build_ssh_argv`` already pins
# ``ConnectTimeout`` so the TCP handshake to a dead host fails fast, but a
# peer that accepts the connection and then stalls (or a wedged ProxyJump
# middle-hop) could otherwise keep ``subprocess.run`` blocked forever — the
# exact hang that, on the pre-bind startup path, took the whole fleet's
# comms down with no error logged (INCIDENT 2026-06-26). We pass ``timeout=``
# to every ssh ``subprocess.run`` and a tighter ``ConnectTimeout`` override so
# an unreachable/stalled peer is converted into a loud, fast per-peer FAIL
# instead of an indefinite wedge.
_PEER_SSH_TIMEOUT_S = 15.0
_PEER_SSH_CONNECT_TIMEOUT_S = 5

# ssh option overriding the probe-friendly default ConnectTimeout=10 baked
# into ``build_ssh_argv`` with a tighter value for the sync path, so a
# powered-off peer fails its TCP connect in ~5s rather than ~10s.
_SSH_TIGHT_CONNECT_OPTS = ["-o", f"ConnectTimeout={_PEER_SSH_CONNECT_TIMEOUT_S}"]


@dataclass
class SyncResult:
    """Per-peer outcome of one sync direction (pull or push).

    ``direction`` is ``"pull"`` (--from) or ``"push"`` (--to). ``ok``
    is False on any ssh non-zero / malformed JSON; ``error`` carries
    a short human-readable explanation suitable for a log line.
    ``inserted`` is the per-table ``{table: rows}`` map from
    :func:`import_state` (pull only; push gets the import-counts back
    from the peer's stdout when it returns JSON).
    """

    peer: str
    direction: str  # "pull" | "push"
    ok: bool
    error: str | None = None
    inserted: dict[str, int] | None = None


def _stamp_source_host(payload: dict[str, Any], source_host: str) -> dict[str, Any]:
    """Re-stamp every ``comms_nodes`` row's ``source_host`` field.

    The peer's ``sac db export`` writes ``source_host`` as the row was
    stored on its side (often NULL for the peer's own locally-registered
    nodes). For OUR import, those rows MUST be tagged with the peer's
    canonical host so the conflict detector in :func:`register_comms_node`
    can distinguish "I'm hearing about this from peer X" from a local
    registration.

    The export payload already carries the peer's canonical hostname in
    ``payload["host"]`` (set by :func:`export_state` from
    ``_resolve_host``); we use that as the source.

    Returns a new payload dict (deep enough for the comms_nodes
    rewrite); other tables pass through untouched.
    """
    new_payload = dict(payload)
    tables = dict(payload.get("tables", {}))
    rows = list(tables.get("comms_nodes", []))
    rewritten = []
    for row in rows:
        new_row = dict(row)
        new_row["source_host"] = source_host
        rewritten.append(new_row)
    tables["comms_nodes"] = rewritten
    new_payload["tables"] = tables
    return new_payload


def _pull_from(peer: str, cfg: Config, *, dry_run: bool) -> SyncResult:
    """Pull comms_nodes from ``peer`` and import locally."""
    remote_argv = ["sac", "db", "export", "--tables", "comms_nodes"]
    ssh_argv = build_ssh_argv(
        peer, remote_argv, cfg.peers, extra_opts=_SSH_TIGHT_CONNECT_OPTS
    )
    if dry_run:
        click.echo(
            f"[dry-run] pull comms_nodes from {peer!r}: "
            f"{' '.join(ssh_argv)}",
            err=True,
        )
        return SyncResult(peer=peer, direction="pull", ok=True, inserted={})
    try:
        proc = subprocess.run(
            ssh_argv, capture_output=True, text=True, timeout=_PEER_SSH_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        # Skip-unreachable-and-WARN: an unreachable/stalled peer must
        # fail fast and loud, never wedge the caller. Naming the peer +
        # the budget makes the WARN actionable.
        return SyncResult(
            peer=peer,
            direction="pull",
            ok=False,
            error=(
                f"ssh timed out after {_PEER_SSH_TIMEOUT_S:.0f}s "
                f"(peer unreachable or stalled) — skipped"
            ),
        )
    if proc.returncode != 0:
        return SyncResult(
            peer=peer,
            direction="pull",
            ok=False,
            error=(
                f"ssh exit={proc.returncode}; "
                f"stderr={proc.stderr.strip()[:300]!r}"
            ),
        )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        return SyncResult(
            peer=peer,
            direction="pull",
            ok=False,
            error=f"malformed JSON from remote: {exc}",
        )
    # Determine the source-host stamp. Prefer the peer's canonical name
    # from its own export header; fall back to the peer key when the
    # remote skipped the header for any reason.
    source_host = str(payload.get("host") or peer)
    stamped = _stamp_source_host(payload, source_host)
    from .._state.state_db import import_state
    from .._state.state_db_nodes import CommsNodeConflictError

    try:
        inserted = import_state(stamped)
    except CommsNodeConflictError as exc:
        return SyncResult(
            peer=peer,
            direction="pull",
            ok=False,
            error=f"comms_nodes name conflict during import: {exc}",
        )
    except ValueError as exc:
        return SyncResult(
            peer=peer,
            direction="pull",
            ok=False,
            error=f"import_state rejected payload: {exc}",
        )
    return SyncResult(peer=peer, direction="pull", ok=True, inserted=dict(inserted))


def _push_to(peer: str, cfg: Config, *, dry_run: bool) -> SyncResult:
    """Export locally and pipe to ``ssh peer -- sac db import -``."""
    remote_argv = ["sac", "db", "import", "-"]
    ssh_argv = build_ssh_argv(
        peer, remote_argv, cfg.peers, extra_opts=_SSH_TIGHT_CONNECT_OPTS
    )
    if dry_run:
        click.echo(
            f"[dry-run] push comms_nodes to {peer!r}: "
            f"{' '.join(ssh_argv)}",
            err=True,
        )
        return SyncResult(peer=peer, direction="push", ok=True, inserted={})
    from .._state.state_db import export_state

    payload = export_state(tables=["comms_nodes"])
    try:
        proc = subprocess.run(
            ssh_argv,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=_PEER_SSH_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Skip-unreachable-and-WARN (see _pull_from): a stalled push must
        # never block the caller; fail fast and name the peer + budget.
        return SyncResult(
            peer=peer,
            direction="push",
            ok=False,
            error=(
                f"ssh timed out after {_PEER_SSH_TIMEOUT_S:.0f}s "
                f"(peer unreachable or stalled) — skipped"
            ),
        )
    if proc.returncode != 0:
        return SyncResult(
            peer=peer,
            direction="push",
            ok=False,
            error=(
                f"ssh exit={proc.returncode}; "
                f"stderr={proc.stderr.strip()[:300]!r}"
            ),
        )
    return SyncResult(peer=peer, direction="push", ok=True, inserted=None)


def _enumerate_static_peers(cfg: Config) -> list[str]:
    """Every ``peers:`` key that isn't a glob pattern.

    Glob keys (``spartan-*``) are only ever realised via lookup against
    a known live name; for ``--all`` we cannot synthesize a peer from a
    pattern without that name. The skip mirrors :mod:`_fleet_sync`'s
    same-named helper.
    """
    return [name for name in cfg.peers.keys() if not any(c in name for c in "*?[")]


def _emit_text_report(results: Iterable[SyncResult]) -> int:
    """Pretty-print results to stderr and return the process exit code."""
    failed = 0
    for r in results:
        if r.ok:
            tag = "ok"
            extra = ""
            if r.inserted is not None:
                total = sum(r.inserted.values())
                extra = f" inserted={total}"
            click.echo(f"  [{tag}] {r.direction} {r.peer}{extra}", err=True)
        else:
            failed += 1
            click.echo(
                f"  [FAIL] {r.direction} {r.peer}: {r.error}",
                err=True,
            )
    return 1 if failed else 0


def registry_sync_impl(
    *,
    from_peer: str | None,
    to_peer: str | None,
    all_peers: bool,
    dry_run: bool,
    as_json: bool,
    overall_budget_s: float | None = None,
) -> int:
    """Click-decoupled core. Returns the intended process exit code.

    Mode resolution:

    * ``--from PEER`` and/or ``--to PEER`` named explicitly → run only
      that direction(s) against that peer.
    * ``--all`` → pull from every static peer, then push to every
      static peer. Per-peer errors do not abort; the exit code is
      non-zero iff any peer failed.
    * No mode flags → :class:`click.UsageError`.

    ``overall_budget_s`` (``--all`` only) caps the wall-clock the whole
    sweep may spend. Each peer is already bounded by ``_PEER_SSH_TIMEOUT_S``;
    this is the second guard so a config with many unreachable peers cannot
    accumulate into a long block. Once the budget is exhausted, every
    remaining peer is skipped with a loud per-peer WARN (named + reason)
    rather than attempted. ``None`` (the CLI default) means unbounded.
    """
    if not (from_peer or to_peer or all_peers):
        raise click.UsageError(
            "sac registry sync: pass at least one of --from PEER, --to PEER, "
            "or --all"
        )
    cfg = load()
    results: list[SyncResult] = []

    if all_peers:
        static = _enumerate_static_peers(cfg)
        if not static:
            click.echo(
                "sac registry sync --all: no static peers under peers: "
                "in config.yaml; nothing to do.",
                err=True,
            )
            return 0
        import time as _time

        deadline = (
            None if overall_budget_s is None else _time.monotonic() + overall_budget_s
        )

        def _budget_skip(pname: str, direction: str) -> SyncResult:
            return SyncResult(
                peer=pname,
                direction=direction,
                ok=False,
                error=(
                    f"overall sync budget ({overall_budget_s:.0f}s) exhausted "
                    f"before reaching this peer — skipped"
                ),
            )

        for pname in static:
            if deadline is not None and _time.monotonic() >= deadline:
                results.append(_budget_skip(pname, "pull"))
                continue
            results.append(_pull_from(pname, cfg, dry_run=dry_run))
        for pname in static:
            if deadline is not None and _time.monotonic() >= deadline:
                results.append(_budget_skip(pname, "push"))
                continue
            results.append(_push_to(pname, cfg, dry_run=dry_run))
    else:
        if from_peer:
            if from_peer not in cfg.peers:
                raise click.UsageError(
                    f"--from peer {from_peer!r} not in peers: in config.yaml"
                )
            results.append(_pull_from(from_peer, cfg, dry_run=dry_run))
        if to_peer:
            if to_peer not in cfg.peers:
                raise click.UsageError(
                    f"--to peer {to_peer!r} not in peers: in config.yaml"
                )
            results.append(_push_to(to_peer, cfg, dry_run=dry_run))

    if as_json:
        click.echo(
            json.dumps(
                {
                    "dry_run": dry_run,
                    "results": [
                        {
                            "peer": r.peer,
                            "direction": r.direction,
                            "ok": r.ok,
                            "error": r.error,
                            "inserted": r.inserted,
                        }
                        for r in results
                    ],
                },
                indent=2,
            )
        )
        return 1 if any(not r.ok for r in results) else 0
    return _emit_text_report(results)


@click.command("sync")
@click.option(
    "--from",
    "from_peer",
    type=str,
    default=None,
    help="Pull comms_nodes from PEER via ssh + sac db export.",
)
@click.option(
    "--to",
    "to_peer",
    type=str,
    default=None,
    help="Push local comms_nodes to PEER via ssh + sac db import -.",
)
@click.option(
    "--all",
    "all_peers",
    is_flag=True,
    default=False,
    help=(
        "Iterate every static peer under peers: in config.yaml — pull "
        "from each, then push to each. Per-peer errors do not abort."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Log the planned ssh commands; do not run them or write to DB.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a JSON envelope.")
def registry_sync(
    from_peer: str | None,
    to_peer: str | None,
    all_peers: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Anti-entropy sync of the comms_nodes registry across hosts.

    ADR-0014 Stage 1. Re-uses ``sac db export --tables comms_nodes``
    over ssh. Idempotent: INSERT OR IGNORE on the ``name`` PK plus
    the conflict-detect on differing (host, port) from a different
    source.

    \b
    Examples:
      $ sac registry sync --from spartan          # pull spartan's view
      $ sac registry sync --to lead-laptop        # push to a peer
      $ sac registry sync --all                   # bidirectional, all peers
      $ sac registry sync --all --dry-run         # plan only
    """
    rc = registry_sync_impl(
        from_peer=from_peer,
        to_peer=to_peer,
        all_peers=all_peers,
        dry_run=dry_run,
        as_json=as_json,
    )
    if rc != 0:
        raise SystemExit(rc)


__all__ = [
    "SyncResult",
    "registry_sync",
    "registry_sync_impl",
]
