"""``sac agents cct-audit`` — who declares a Telegram rail, and who actually has one.

WHY A SWEEP AND NOT JUST THE START-TIME ALARM
    :mod:`..runtimes._cct_rail_alarm` fires when an agent STARTS. That closes
    the class going forward and does nothing for the agents already running
    mute right now — which is the population that matters, because every one of
    them is a channel the operator has silently lost. An agent only re-reports
    when it is next restarted, and the whole point of the failure is that
    nobody knows which agents to restart.

    So this reads every spec on the host and answers the question once, for the
    whole fleet, without touching a single running agent. Read-only: it starts
    nothing, restarts nothing, and writes nothing.

READ THE OUTPUT LIKE THIS
    UP        a token resolves (via the pool, or already folded into .env).
    DOWN      the spec asks for the rail and NO slot resolves. This agent is
              mute AND deaf on Telegram right now, and cannot self-diagnose.
    UNKNOWN   sac could not tell. NOT a soft DOWN and NOT an all-clear — the
              pool read was inconclusive, so nothing was learned. Fix the
              vantage point first, then re-run; a fleet full of UNKNOWN
              usually means ONE thing is wrong (no SAC_SECRETS_ENVRC in the
              environment you ran this from), not ninety.
    n/a       the spec never asks for the rail. Nothing to be wrong.

    Exits 1 when any agent is DOWN or UNKNOWN, so a timer or a relocation
    preflight can gate on it.

VANTAGE POINT IS PART OF THE MEASUREMENT
    The pool resolves from the environment of whoever runs this. An operator's
    interactive shell, a systemd unit, a non-interactive ssh and a container
    each see a DIFFERENT pool, and that difference is the 2026-08-12 root
    cause, not a footnote. Every run therefore prints the pool source and
    whether the read was conclusive, and marks the whole table UNKNOWN rather
    than DOWN when it was not. Run it where the agents are STARTED from.

TOKEN VALUES ARE NEVER READ
    Presence only. The table carries slot NAMES and pool source PATHS — the
    same strings sac's logs already carry — and nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

from ..runtimes._cct_rail_verdict import (
    RAIL_DOWN,
    RAIL_NOT_REQUESTED,
    RAIL_UNKNOWN,
    RAIL_UP,
    assess_cct_rail,
)
from ..runtimes._secret_pool import _pool_source_label, read_pool
from ._helpers import _json_flag, console

_STYLE = {
    RAIL_UP: ("UP", "green"),
    RAIL_DOWN: ("DOWN (mute + deaf)", "red"),
    RAIL_UNKNOWN: ("UNKNOWN", "yellow"),
    RAIL_NOT_REQUESTED: ("n/a", "dim"),
}


def _spec_paths():
    """Every ``<agents_root>/<name>/spec.yaml`` on this host, sorted by name."""
    from .._state.state_paths import agents_root

    root = agents_root()
    if not root.is_dir():
        return []
    return [p for p in sorted(root.glob("*/spec.yaml")) if p.is_file()]


def _rows(include_unrequested: bool) -> list[dict]:
    """Assess every spec against ONE pool read.

    The pool is read once and injected into every assessment: forking a bash
    per agent to source ~28 secret files would make a 90-agent sweep cost
    minutes, and — worse — could produce rows that disagree with each other if
    the environment shifted mid-run.
    """
    from ..config import load_config

    pool = read_pool()
    rows: list[dict] = []
    for path in _spec_paths():
        name = path.parent.name
        # stx-allow: fallback (reason: one unloadable spec must not abort a fleet-wide audit; it is reported as its OWN unknown row rather than dropped, because a spec sac cannot read is exactly the kind of thing this sweep exists to surface)
        try:
            config = load_config(str(path))
        except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
            rows.append(
                {
                    "agent": name,
                    "state": RAIL_UNKNOWN,
                    "declared_slot": "",
                    "resolved_slot": "",
                    "slots_tried": [],
                    "near_miss_slots": [],
                    "pool_read_conclusive": pool.trusted,
                    "detail": f"spec could not be loaded: {exc}",
                    "spec": str(path),
                }
            )
            continue
        verdict = assess_cct_rail(config, pool=pool)
        if verdict.state == RAIL_NOT_REQUESTED and not include_unrequested:
            continue
        rows.append(
            {
                "agent": verdict.agent or name,
                "state": verdict.state,
                "declared_slot": verdict.declared_slot,
                "resolved_slot": verdict.resolved_slot,
                "slots_tried": list(verdict.candidates),
                "near_miss_slots": list(verdict.near_misses),
                "pool_read_conclusive": verdict.pool_trusted,
                "detail": verdict.detail,
                "spec": str(path),
            }
        )
    return rows


def _short_pool_label(label: str) -> str:
    """The pool source condensed for the console. JSON keeps it verbatim.

    A real fleet host lists ~28 absolute secret-file paths here, which wraps to
    a dozen lines and buries the counts underneath it. The reader needs to know
    WHICH pool was read, not to re-read every path — and the exact list is one
    ``--json`` away.
    """
    prefix, sep, raw = label.partition("=")
    parts = [p for p in raw.split(":") if p]
    if not sep or len(parts) <= 1:
        return label
    return f"{prefix}={len(parts)} secret file(s) under {Path(parts[0]).parent}"


def _render_table(rows: list[dict]) -> None:
    table = Table(title="CCT Telegram rail — declared vs resolved")
    table.add_column("AGENT", overflow="fold")
    table.add_column("RAIL")
    table.add_column("SLOT", overflow="fold")
    table.add_column("TRIED", overflow="fold")
    table.add_column("DID YOU MEAN", overflow="fold")
    for row in rows:
        label, style = _STYLE.get(row["state"], (row["state"], "white"))
        slot = row["resolved_slot"] or (
            f"declared:{row['declared_slot']}" if row["declared_slot"] else "-"
        )
        table.add_row(
            row["agent"],
            f"[{style}]{label}[/{style}]",
            slot,
            ", ".join(row["slots_tried"]) or "-",
            ", ".join(row["near_miss_slots"]) or "-",
        )
    console.print(table)


@click.command(name="cct-audit")
@click.option(
    "--all",
    "include_unrequested",
    is_flag=True,
    help="Also list agents whose spec never requests the telegrammer channel.",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def cct_audit(ctx: click.Context, include_unrequested: bool, as_json: bool) -> None:
    """Audit every spec's Telegram rail: declared vs actually resolved.

    \b
    THE FAILURE THIS SWEEPS UP:
      When a spec declares `server:claude-code-telegrammer` and no
      CCT_BOT_TOKEN_<SLOT> resolves, sac REMOVES the MCP server (correct, by
      operator ruling). The agent then starts perfectly and reports healthy
      while being MUTE and DEAF on Telegram. It cannot even tell you — the
      `health` tool lives on the server that was removed.

    \b
    SLOT NAMES ARE NOT CHECKED AGAINST THE POOL ANYWHERE ELSE:
      candidates are derived mechanically from the agent NAME, and the pool is
      named by whoever wrote it. Measured mismatches include a WORD-ORDER
      difference (NEUROVISTA_PAPER_WRITER vs PAPER_NEUROVISTA_WRITER), which
      no derivation rule can bridge. The DID YOU MEAN column names pool slots
      sharing a word with the agent — a hint for a human, never something sac
      acts on.

    \b
    THE FIX IS ONE LINE, in the agent's spec under spec.apptainer.env:
        CCT_BOT_TOKEN_SLOT: <SLOT>
      (precedence #2 — the documented override, and the only route that
      survives a relocation).

    Read-only. Starts nothing, restarts nothing, and never reads a token value.
    Exits 1 if any agent is DOWN or UNKNOWN.
    """
    rows = _rows(include_unrequested)
    down = [r for r in rows if r["state"] == RAIL_DOWN]
    unknown = [r for r in rows if r["state"] == RAIL_UNKNOWN]
    pool_label = _pool_source_label()

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "pool_source": pool_label,
                    "agents": rows,
                    "down": len(down),
                    "unknown": len(unknown),
                },
                indent=2,
            )
        )
    else:
        _render_table(rows)
        console.print(f"[dim]pool source: {_short_pool_label(pool_label)}[/dim]")
        console.print(
            f"{len(rows)} agent(s) considered — "
            f"[red]{len(down)} DOWN[/red], [yellow]{len(unknown)} UNKNOWN[/yellow]"
        )
        if unknown:
            console.print(
                "[yellow]UNKNOWN is not an all-clear.[/yellow] sac could not "
                "read the pool it meant to read from HERE. Re-run from where "
                "the agents are started (the pool resolves from the LAUNCHING "
                "process env), or set SAC_SECRETS_ENVRC, before believing any "
                "row."
            )
        if down:
            console.print(
                "Fix each DOWN agent with ONE line in its spec under "
                "[bold]spec.apptainer.env[/bold]:  "
                "[bold]CCT_BOT_TOKEN_SLOT: <SLOT>[/bold]  — or drop "
                "'server:claude-code-telegrammer' from spec.claude.channels "
                "if it needs no Telegram rail."
            )

    if down or unknown:
        ctx.exit(1)


def register(group: click.Group) -> None:
    """Attach ``cct-audit`` to the ``sac agents`` group."""
    group.add_command(cct_audit)


__all__ = ["cct_audit", "register"]
