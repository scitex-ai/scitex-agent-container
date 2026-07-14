"""``sac ports`` — read-only inventory of the ports sac / scitex uses.

Port hygiene surface (operator request): a single command that shows
every port sac cares about, with live status, so the operator can SEE
the assignment scheme rather than reverse-engineer it from configs.

What it reports:

  1. The **sac listen** control-plane port (default ``7878`` —
     :data:`_listen._config.DEFAULT_LISTEN_PORT`), its liveness (a
     bounded TCP-connect probe) and the on-disk pidfile
     (``~/.scitex/agent-container/runtime/listen-<port>.pid``).
  2. Every **a2a sidecar** port CLAIM. Read in ONE query via
     :func:`_state.port_allocator.list_claims` (the same one-shot API
     ``sac agents list`` uses to enrich its rows), each probed for
     liveness with a short, bounded socket timeout so the command can
     never hang.
  3. A **reference map** of the scitex / sac port-assignment scheme
     for context (see :func:`_reference_map`).
  4. **CONFLICT** (two owners on one port) and **ORPHAN** (a claim
     with nothing listening) flags — both computed cheaply from the
     data already gathered.

Everything here is READ-ONLY: no claim is created, released, or
mutated. The probes are outbound TCP connects that touch nothing.
"""

from __future__ import annotations

import json as _json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import click


def _reference_map() -> list[dict]:
    """The scitex / sac port-assignment scheme, as a list of ``{range,
    purpose, owner}`` rows.

    This is a **sac-side** reference for operator context — there is no
    cross-ecosystem port-registry SSOT in the codebase yet, so the
    ``3129X`` GUI/dashboard block (owned by scitex-dev / tools) is
    listed here by convention. The ecosystem-wide view is being routed
    to scitex-dev separately; when that SSOT lands, this literal should
    be replaced with a read of it rather than forked into a second copy.

    The two sac-owned entries (listen port, a2a range) are read from
    their real sources — :data:`DEFAULT_LISTEN_PORT` and the *effective*
    allocator range (config override honoured) — so the reference never
    drifts from what sac actually uses.
    """
    from .._listen._config import DEFAULT_LISTEN_PORT
    from .._state import port_allocator

    # Effective a2a range: honours ``a2a.port_range`` in config.yaml,
    # falling back to the built-in default. Tolerant — a broken private
    # resolver must not sink a read-only inventory.
    try:  # stx-allow: fallback (reason: reference row must render even if config read hiccups)
        lo, hi = port_allocator._resolve_range(None)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        lo, hi = port_allocator.DEFAULT_RANGE

    return [
        {
            "range": str(DEFAULT_LISTEN_PORT),
            "purpose": "sac listen — control-plane HTTP/JSON (single host port)",
            "owner": "sac",
        },
        {
            "range": f"{lo}-{hi}",
            "purpose": "sac a2a sidecar — per-agent IPC auto-allocation range",
            "owner": "sac (port_allocator)",
        },
        {
            "range": "31290-31299",
            "purpose": "scitex GUI / dashboard block (scitex-dev / tools)",
            "owner": "scitex-dev (ecosystem)",
        },
    ]


def _safe_probe(probe: Callable[[str, int], bool], host: str, port: int) -> bool:
    """Run ``probe(host, port)`` and swallow any error as ``False``.

    A live-probe hiccup (socket exhaustion, odd address) must degrade to
    "not live" rather than crash the whole inventory.
    """
    # stx-allow: fallback (reason: a single probe failure maps to
    # not-live; the inventory must never crash on a socket hiccup.)
    try:
        return bool(probe(host, port))
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return False


def collect_ports_data(
    *,
    db_path: Path | None = None,
    listen_host: str | None = None,
    listen_port: int | None = None,
    probe: Callable[[str, int], bool] | None = None,
    probe_timeout: float = 0.3,
    lock_dir: Path | None = None,
) -> dict:
    """Assemble the port inventory as a plain dict (JSON-ready).

    Args:
        db_path: Override the state.db location (tests). ``None`` uses
            the default ``~/.scitex/agent-container/runtime/state.db``.
        listen_host / listen_port: Override the resolved listen bind
            (tests). ``None`` reads ``_listen._config`` (config.yaml >
            built-in ``127.0.0.1:7878``).
        probe: Liveness probe ``(host, port) -> bool``. ``None`` uses a
            bounded :func:`_listen._port_holder.port_is_bound` with
            ``probe_timeout``. Injected as a seam; the default is a real
            outbound TCP connect.
        probe_timeout: Per-port socket timeout (seconds) for the default
            probe. Short so the command stays fast and never hangs.
        lock_dir: Override the pidfile lock dir (tests).

    Returns:
        ``{"listen": {...}, "a2a_claims": [...], "conflicts": [...],
        "orphans": [...], "reference": [...]}``.
    """
    from .._listen import _config
    from .._listen._port_holder import port_is_bound
    from .._listen._restart import pid_alive, pidfile_path, read_pid_from_file
    from .._listen._single_instance import default_lock_dir
    from .._state import port_allocator

    host = listen_host if listen_host is not None else _config.listen_host()
    lport = listen_port if listen_port is not None else _config.listen_port()
    ldir = lock_dir if lock_dir is not None else default_lock_dir()

    if probe is None:

        def probe(h: str, p: int) -> bool:  # noqa: A001 — local shadow is intentional
            return port_is_bound(h, p, timeout=probe_timeout)

    # a2a claims — ONE db read (same API ``sac agents list`` uses).
    claims = port_allocator.list_claims(db_path=db_path)

    # Bounded liveness probes for listen + every claim, run concurrently
    # so N dead claims cost ~one timeout, not N. Each probe is already
    # timeout-bounded, so the pool only parallelises the waiting.
    targets: list[tuple[str, int]] = [(host, lport)]
    targets += [("127.0.0.1", c["port"]) for c in claims]
    workers = min(8, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        live_flags = list(pool.map(lambda t: _safe_probe(probe, t[0], t[1]), targets))

    listen_live = live_flags[0]
    a2a_live = live_flags[1:]

    pid_file = pidfile_path(lport, ldir)
    pid = read_pid_from_file(pid_file)
    listen_row = {
        "kind": "listen",
        "port": lport,
        "host": host,
        "purpose": "sac listen (control-plane)",
        "owner": "sac-listen",
        "live": bool(listen_live),
        "pidfile": str(pid_file),
        "pid": pid,
        "pid_alive": bool(pid is not None and pid_alive(pid)),
    }

    a2a_rows: list[dict] = []
    for claim, live in zip(claims, a2a_live):
        a2a_rows.append(
            {
                "kind": "a2a",
                "port": claim["port"],
                "host": "127.0.0.1",
                "purpose": "a2a sidecar",
                "owner": claim["name"],
                "live": bool(live),
                "claimed_at": claim.get("claimed_at"),
                "orphan": not bool(live),
            }
        )

    # CONFLICT: >1 owner on a single port. list_claims enforces UNIQUE
    # (port) among a2a claims, so the only way this fires is an a2a
    # claim colliding with the listen port — but compute it generally.
    owners_by_port: dict[int, list[str]] = defaultdict(list)
    owners_by_port[lport].append("sac-listen")
    for claim in claims:
        owners_by_port[claim["port"]].append(claim["name"])
    conflicts = [
        {"port": port, "owners": sorted(owners)}
        for port, owners in sorted(owners_by_port.items())
        if len(owners) > 1
    ]

    # ORPHAN: an a2a claim with nothing listening on its port.
    orphans = [
        {"agent": row["owner"], "port": row["port"]} for row in a2a_rows if row["orphan"]
    ]

    return {
        "listen": listen_row,
        "a2a_claims": a2a_rows,
        "conflicts": conflicts,
        "orphans": orphans,
        "reference": _reference_map(),
    }


def _live_cell(live: bool) -> str:
    """Rich-markup Live cell — green ``● yes`` / red ``○ no``."""
    return "[green]● yes[/green]" if live else "[red]○ no[/red]"


def _render_table(data: dict) -> None:
    """Print the inventory as two rich tables + a summary footer."""
    from rich.table import Table

    from ._helpers._console import console

    conflict_ports = {c["port"] for c in data["conflicts"]}

    inv = Table(title="Ports — sac live inventory")
    inv.add_column("Port", justify="right", style="bold", no_wrap=True)
    inv.add_column("Purpose")
    inv.add_column("Owner / Agent", no_wrap=True)
    inv.add_column("Live", justify="center")
    inv.add_column("Flag")

    for row in [data["listen"], *data["a2a_claims"]]:
        flags: list[str] = []
        if row["port"] in conflict_ports:
            flags.append("[bold red]CONFLICT[/bold red]")
        if row.get("orphan"):
            flags.append("[yellow]ORPHAN[/yellow]")
        inv.add_row(
            str(row["port"]),
            row["purpose"],
            row["owner"],
            _live_cell(bool(row["live"])),
            " ".join(flags) or "—",
        )
    console.print(inv)

    listen = data["listen"]
    if listen.get("pid") is not None:
        alive = "alive" if listen.get("pid_alive") else "[red]stale[/red]"
        console.print(
            f"[dim]listen pidfile {listen['pidfile']} → pid {listen['pid']} "
            f"({alive})[/dim]"
        )

    ref = Table(title="Reference — scitex / sac port-assignment scheme")
    ref.add_column("Range", justify="right", style="bold", no_wrap=True)
    ref.add_column("Purpose")
    ref.add_column("Owner", no_wrap=True)
    for row in data["reference"]:
        ref.add_row(row["range"], row["purpose"], row["owner"])
    console.print(ref)
    console.print(
        "[dim]Reference is a sac-side map; the ecosystem-wide port SSOT "
        "(incl. the 3129X GUI block) is being routed to scitex-dev.[/dim]"
    )

    live_a2a = sum(1 for r in data["a2a_claims"] if r["live"])
    console.print(
        f"[dim]{len(data['a2a_claims'])} a2a claim(s), {live_a2a} live; "
        f"listen {'up' if listen['live'] else 'down'}.[/dim]"
    )
    if data["conflicts"]:
        console.print(
            f"[bold red]⚠ {len(data['conflicts'])} port conflict(s)[/bold red] "
            "— two owners on one port."
        )
    if data["orphans"]:
        console.print(
            f"[yellow]⚠ {len(data['orphans'])} orphan claim(s)[/yellow] "
            "— a2a port claimed, nothing listening."
        )


@click.command(name="ports")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON envelope.",
)
@click.option(
    "--timeout",
    "probe_timeout",
    type=float,
    default=0.3,
    show_default=True,
    help="Per-port live-probe socket timeout (seconds).",
)
@click.pass_context
def ports(ctx: click.Context, as_json: bool, probe_timeout: float) -> None:
    """List the ports sac / scitex uses, with live status.

    Read-only port-hygiene inventory:

    \b
      * the sac listen control-plane port (default 7878) + liveness
      * every a2a sidecar port CLAIM (per-agent IPC) + liveness
      * a reference map of the scitex / sac port-assignment scheme
      * CONFLICT (two owners on one port) and ORPHAN (claim with
        nothing listening) flags

    \b
    Example:
        sac ports                 # rich table
        sac ports --json          # JSON envelope for scripts
        sac ports --timeout 1.0   # slower / looser live probes
    """
    from ._helpers._json_flag import _json_flag

    data = collect_ports_data(probe_timeout=probe_timeout)
    if _json_flag(ctx, as_json):
        click.echo(_json.dumps(data, indent=2, default=str))
        return
    _render_table(data)


__all__ = ["ports", "collect_ports_data"]
