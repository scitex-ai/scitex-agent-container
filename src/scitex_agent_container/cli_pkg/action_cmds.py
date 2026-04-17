"""CLI for the action subsystem — ``run | query | stats | purge``.

Resolves an agent name through the local :class:`Registry`, builds an
:class:`ActionContext` from the multiplexer's capture function, and
dispatches to :func:`run_action`. The ``query`` / ``stats`` / ``purge``
subcommands are thin wrappers over :mod:`action_store`.

This module intentionally contains no policy (when to probe, when to
compact) — those belong in a future auto-response scheduler layer.
The CLI is the manual-and-scripting interface.
"""

from __future__ import annotations

import json as json_mod
import sys
from typing import Any, Optional

import click

from .. import action_store
from ..action_base import ActionContext, ActionOutcome, run_action
from ..actions.compact import CompactAction
from ..actions.nonce_probe import NonceProbeAction
from ..registry import Registry

# Mapping of CLI action name to the constructor. Extend by appending
# one line when a new PaneAction subclass is added.
_ACTION_FACTORIES: dict[str, Any] = {
    "nonce-probe": NonceProbeAction,
    "compact": CompactAction,
}


@click.group("actions")
def actions_cli() -> None:
    """Run, query, and aggregate agent-action attempts."""


def _json_echo(obj: Any) -> None:
    click.echo(json_mod.dumps(obj, indent=2, default=str))


# ── run ─────────────────────────────────────────────────────────────────────


@actions_cli.command("run")
@click.argument("action_name", type=click.Choice(sorted(_ACTION_FACTORIES.keys())))
@click.argument("agent", type=str)
@click.option(
    "--timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Wall-clock cap for completion polling.",
)
@click.option(
    "--poll-interval",
    type=float,
    default=2.0,
    show_default=True,
    help="Seconds between post-send snapshots.",
)
@click.option(
    "--skip-reason",
    type=str,
    default=None,
    help="If given, action is SKIPPED_BY_POLICY (used by schedulers).",
)
@click.option(
    "--min-drop-pct",
    type=float,
    default=None,
    help="CompactAction only: override context_pct drop threshold.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the attempt record as JSON on stdout.",
)
def run_cmd(
    action_name: str,
    agent: str,
    timeout: float,
    poll_interval: float,
    skip_reason: Optional[str],
    min_drop_pct: Optional[float],
    as_json: bool,
) -> None:
    """Run a PaneAction against AGENT (registry name).

    Examples::

        scitex-agent-container actions run nonce-probe head-ywata-note-win
        scitex-agent-container actions run compact head-ywata-note-win \\
            --min-drop-pct 30 --timeout 60
    """
    # --- resolve agent -> config + multiplexer session -----------------
    registry = Registry()
    entry = registry.get(agent)
    if entry is None:
        click.echo(f"Agent '{agent}' not found in registry.", err=True)
        sys.exit(2)

    try:
        from ..config import load_config  # local import: keeps the

        # ``actions`` CLI importable
        # in test environments that
        # mock the config system.
        config = load_config(entry["config"])
    except Exception as exc:
        click.echo(f"Error loading config for '{agent}': {exc}", err=True)
        sys.exit(2)

    from ..runtimes.multiplexer import get_multiplexer

    mux = get_multiplexer(config)
    session = config.screen_name
    if not mux.exists(session):
        click.echo(
            f"Multiplexer session '{session}' is not alive; nothing to act on.",
            err=True,
        )
        sys.exit(2)

    # --- build the ActionContext ----------------------------------------
    def _capture() -> str:
        try:
            return mux.capture_content(session) or ""
        except Exception:
            return ""

    def _context_pct() -> Optional[float]:
        """Best-effort context_pct read via agent_meta statusline parser.

        Failures degrade to ``None`` — the action engine treats that as
        "cannot confirm" and will time out instead of crashing.
        """
        try:
            from .. import agent_meta

            rich = agent_meta.collect_rich(
                name=agent,
                workdir=str(getattr(config, "workdir", "") or ""),
                session=session,
            )
            val = rich.get("context_pct")
            return float(val) if val is not None else None
        except Exception:
            return None

    ctx = ActionContext(
        agent=agent,
        session=session,
        mux=mux,
        capture_fn=_capture,
        context_pct_fn=_context_pct,
    )

    # --- construct the action -------------------------------------------
    factory = _ACTION_FACTORIES[action_name]
    if action_name == "compact" and min_drop_pct is not None:
        action = factory(min_drop_pct=min_drop_pct)
    else:
        action = factory()

    attempt = run_action(
        action,
        ctx,
        timeout_s=timeout,
        poll_interval_s=poll_interval,
        skip_reason=skip_reason,
    )

    record = attempt.as_store_record()
    if as_json:
        _json_echo(record)
    else:
        color = {
            ActionOutcome.SUCCESS: "green",
            ActionOutcome.PRECONDITION_FAIL: "yellow",
            ActionOutcome.SEND_ERROR: "red",
            ActionOutcome.COMPLETION_TIMEOUT: "red",
            ActionOutcome.SKIPPED_BY_POLICY: "cyan",
        }.get(attempt.outcome, "white")
        click.secho(
            f"{action_name} on {agent}: {attempt.outcome.value} "
            f"({attempt.elapsed_s:.2f}s)",
            fg=color,
        )
        if attempt.extras:
            click.echo(f"  extras: {json_mod.dumps(attempt.extras, default=str)}")

    # Non-zero exit for anything other than SUCCESS / SKIPPED so
    # schedulers can distinguish them from a clean run.
    if attempt.outcome in (ActionOutcome.SUCCESS, ActionOutcome.SKIPPED_BY_POLICY):
        sys.exit(0)
    sys.exit(1)


# ── query ───────────────────────────────────────────────────────────────────


@actions_cli.command("query")
@click.option("--agent", type=str, default=None, help="Filter by agent.")
@click.option("--action", type=str, default=None, help="Filter by action name.")
@click.option(
    "--outcome",
    type=click.Choice(list(action_store.OUTCOMES)),
    default=None,
    help="Filter by outcome.",
)
@click.option(
    "--since",
    type=str,
    default=None,
    help='Only rows newer than this. Accepts "2h", "7d", or ISO-8601.',
)
@click.option("--limit", type=int, default=50, show_default=True)
@click.option("--offset", type=int, default=0)
@click.option("--json", "as_json", is_flag=True, default=False)
def query_cmd(
    agent: Optional[str],
    action: Optional[str],
    outcome: Optional[str],
    since: Optional[str],
    limit: int,
    offset: int,
    as_json: bool,
) -> None:
    """List recent attempts, most recent first."""
    rows = action_store.query(
        agent=agent,
        action=action,
        outcome=outcome,
        since=since,
        limit=limit,
        offset=offset,
    )
    if as_json:
        _json_echo(rows)
        return
    if not rows:
        click.echo("No matching attempts.")
        return
    for r in rows:
        click.echo(
            f"{r['ts']}  {r['agent']:<24}  {r['action']:<12}  "
            f"{r['outcome']:<20}  {r['elapsed_s']:.2f}s"
        )


# ── stats ───────────────────────────────────────────────────────────────────


@actions_cli.command("stats")
@click.option("--agent", type=str, default=None)
@click.option(
    "--since",
    type=str,
    default=None,
    help='Only rows newer than this. Accepts "2h", "7d", or ISO-8601.',
)
@click.option("--json", "as_json", is_flag=True, default=False)
def stats_cmd(agent: Optional[str], since: Optional[str], as_json: bool) -> None:
    """Per-(action, outcome) counts + mean / p95 elapsed."""
    rows = action_store.stats(agent=agent, since=since)
    if as_json:
        _json_echo(rows)
        return
    if not rows:
        click.echo("No attempts.")
        return
    click.echo(
        f"{'action':<14} {'outcome':<22} {'count':>6} {'mean_s':>8} {'p95_s':>8}"
    )
    click.echo("-" * 62)
    for r in rows:
        click.echo(
            f"{r['action']:<14} "
            f"{r['outcome']:<22} "
            f"{r['count']:>6} "
            f"{(r['mean_elapsed_s'] or 0):>8.2f} "
            f"{(r['p95_elapsed_s'] or 0):>8.2f}"
        )


# ── purge ───────────────────────────────────────────────────────────────────


@actions_cli.command("purge")
@click.option(
    "--days",
    type=int,
    default=None,
    help="Override SCITEX_AGENT_ACTION_RETENTION_DAYS (default 30).",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def purge_cmd(days: Optional[int], as_json: bool) -> None:
    """Delete rows older than ``--days``."""
    deleted = action_store.purge_old(days=days)
    if as_json:
        _json_echo({"deleted": deleted})
    else:
        click.echo(f"Deleted {deleted} rows.")
