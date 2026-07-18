"""``sac agents auth-audit`` — compare auth verdicts against pane LAYOUT. Read-only.

WHY THIS COMMAND EXISTS INSTEAD OF A RESTARTER
    The obvious version of this feature — detect login-required agents and
    restart them — was built and then STOPPED, because the signal it would act
    on is wrong. On 2026-07-18 ``sac agents auth-status`` reported ``grant
    AUTH-FAILED / revoked``; grant was alive and working (it answered a ping,
    read files, ran shell commands, finished a background publish, Context 86%).
    The preserved capture is checked in as a regression fixture.

    A restart destroys live work, so a restarter driven by a detector that flags
    working agents is not a healing tool — it is an outage generator on a timer.
    ``auth-heal`` has logged 167 such auto-restarts in 7 days across 21 agents.

WHAT IT DOES
    For every live ``tui-<agent>`` pane it prints, side by side:

      * VERDICT — what ``sac agents auth-status`` says (the shipped detector);
      * LAYOUT  — what the POSITIONAL predicate says (:mod:`.._authheal
        ._positional`): is the banner ABOVE the startup marker (history) or
        BELOW it (this boot)?
      * LIVE    — whether the pane CHANGED over an observation window, which is
        the only positive evidence of life available without disturbing the
        agent.

    Rows where VERDICT says AUTH-FAILED but LAYOUT says ALIVE are the false
    positives. This command exists to count them BEFORE anything acts on them.

IT NEVER RESTARTS ANYTHING
    There is no ``--apply``. The restart arm ships only once this comparison is
    clean across the fleet and a true-positive pane has actually been captured.
"""

from __future__ import annotations

import json
import time

import click
from rich.table import Table

from .._authheal._journal import Journal, log_path
from .._authheal._liveness import DEFAULT_OBSERVE_S, LIVE, corroborate
from .._authheal._positional import ALIVE, DEAD, UNKNOWN, classify_positional
from ._helpers import _json_flag, console

_VERDICT_STYLE = {
    "ok": ("OK", "green"),
    "auth_failed": ("AUTH-FAILED", "red"),
    "unknown": ("UNKNOWN", "yellow"),
}

_LAYOUT_STYLE = {
    ALIVE: ("ALIVE (banner is history)", "green"),
    DEAD: ("DEAD (banner this boot)", "red"),
    UNKNOWN: ("UNKNOWN", "yellow"),
}


def _rows(observe_s: float, journal: Journal) -> list[dict]:
    """Capture each live pane twice, classify it three ways, log everything."""
    from .._authheal._detect import detect_login_expired
    from ._auth_status import _agent_of, _capture, _list_tui_sessions

    sessions = _list_tui_sessions()
    before = {_agent_of(s): _capture(s) for s in sessions}
    journal.event("OBSERVE", f"{len(sessions)} live pane(s); waiting {observe_s:.0f}s")
    time.sleep(max(0.0, observe_s))
    after = {_agent_of(s): _capture(s) for s in sessions}

    detection = detect_login_expired(
        {name: (before.get(name), after.get(name)) for name in before}
    )

    rows: list[dict] = []
    for name in sorted(before):
        if name in detection.auth_failed:
            verdict = "auth_failed"
        elif name in detection.ok:
            verdict = "ok"
        else:
            verdict = "unknown"
        layout = classify_positional(name, after.get(name))
        life = corroborate(
            name, before.get(name), after.get(name), observed_s=observe_s
        )
        disagrees = verdict == "auth_failed" and layout.state != DEAD
        rows.append(
            {
                "agent": name,
                "verdict": verdict,
                "layout": layout.state,
                "layout_detail": layout.detail,
                "marker_line": layout.marker_line,
                "banner_lines": list(layout.banner_lines),
                "banners_below": list(layout.banners_below),
                "liveness": life.state,
                "liveness_detail": life.detail,
                "false_positive": disagrees,
            }
        )
        journal.event(
            "COMPARE",
            f"agent={name} verdict={verdict} layout={layout.state} "
            f"liveness={life.state} false_positive={disagrees}",
        )
        journal.event("LAYOUT", f"agent={name} {layout.detail}")
        journal.event("LIVENESS", f"agent={name} {life.detail}")
        pane = after.get(name)
        if pane is None:
            journal.event("PANE", f"agent={name} NOT CAPTURED")
        else:
            journal.block("PANE", f"agent={name} verbatim capture:", pane)
    return rows


@click.command(name="auth-audit")
@click.option(
    "--observe",
    "observe_s",
    type=float,
    default=DEFAULT_OBSERVE_S,
    show_default=True,
    help=(
        "Seconds between the two captures. Longer than any agent hook — a "
        "shorter window photographs the moment before a reply and calls it death."
    ),
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def auth_audit(ctx: click.Context, observe_s: float, as_json: bool) -> None:
    """Compare auth-status verdicts against pane LAYOUT. Read-only, never restarts.

    \b
    THE PROBLEM THIS MEASURES:
      `sac agents auth-status` matches a "Login expired" banner near the prompt.
      A banner is the last thing an agent RENDERED — not proof it is broken NOW.
      An agent that hit a 401, recovered, and went idle keeps that banner on
      screen forever, so it is reported AUTH-FAILED permanently. Verified live
      on 2026-07-18: `grant` was flagged while working normally.

    \b
    THE LAYOUT COLUMN is the proposed fix (the operator's rule):
      sac injects a startup prompt on boot. A banner ABOVE that marker was
      printed BEFORE this boot -> history, ignore it. A banner BELOW it was
      printed by THIS boot -> current, and only that justifies acting.

    \b
    READ THE OUTPUT LIKE THIS:
      VERDICT=AUTH-FAILED + LAYOUT=ALIVE  -> a FALSE POSITIVE. Restarting this
                                             agent would destroy live work.
      VERDICT=AUTH-FAILED + LAYOUT=DEAD   -> a genuine candidate.
      anything UNKNOWN                    -> we learned nothing; do nothing.

    Exits 1 if any false positive is found — that is the gate on ever enabling
    an automated restarter.
    """
    journal = Journal.open(log_path())
    journal.event("AUDIT-START", f"observe={observe_s:.0f}s (read-only, no restarts)")
    if not journal.usable:
        console.print(f"[yellow]NOTE: no log — {journal.detail}[/yellow]")

    rows = _rows(observe_s, journal)
    bad = [r for r in rows if r["false_positive"]]
    journal.event("AUDIT-END", f"agents={len(rows)} false_positives={len(bad)}")

    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps(
                {
                    "agents": rows,
                    "false_positives": len(bad),
                    "log_file": str(journal.path),
                },
                indent=2,
            )
        )
        raise SystemExit(1 if bad else 0)

    if not rows:
        console.print("[dim](no running tui-* agents on this host)[/dim]")
        return

    table = Table(title="auth verdict vs pane layout (read-only audit)")
    table.add_column("agent", style="bold")
    table.add_column("VERDICT (shipped)")
    table.add_column("LAYOUT (positional)")
    table.add_column("LIVE?")
    table.add_column("", overflow="fold")
    for r in rows:
        vlabel, vstyle = _VERDICT_STYLE.get(r["verdict"], ("?", "yellow"))
        llabel, lstyle = _LAYOUT_STYLE.get(r["layout"], ("?", "yellow"))
        table.add_row(
            r["agent"],
            f"[{vstyle}]{vlabel}[/{vstyle}]",
            f"[{lstyle}]{llabel}[/{lstyle}]",
            "changed" if r["liveness"] == LIVE else r["liveness"],
            "[red]FALSE POSITIVE[/red]" if r["false_positive"] else "",
        )
    console.print(table)
    console.print(f"[dim]full log: {journal.path}[/dim]")

    if bad:
        console.print(
            f"\n[red]{len(bad)} agent(s) are flagged AUTH-FAILED but their "
            f"banner is HISTORY:[/red] {', '.join(r['agent'] for r in bad)}\n"
            "  Restarting these would destroy live work. An automated restarter "
            "MUST NOT be enabled while this count is non-zero."
        )
    raise SystemExit(1 if bad else 0)


def register(agent_group) -> None:
    """Attach ``auth-audit`` to the parent ``agents`` Click group."""
    agent_group.add_command(auth_audit)


__all__ = ["auth_audit", "register"]
