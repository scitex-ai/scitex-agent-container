"""``sac agents auth-status`` — which running TUI agents are login-stuck.

The reliable, prompt-anchored replacement for the operator's ad-hoc auth
health check (TG 1497: "a command to see if any running agent shows
login-required"). For each live ``tui-<agent>`` tmux session it captures the
pane TWICE (``--interval`` apart), runs the near-prompt + distance-frozen
matcher (:mod:`.._runners._tmux.auth_status`), and prints OK / LOGIN-REQUIRED.

LOGIN-REQUIRED means: a SYSTEM auth banner sits in the conversation tail
directly above the input prompt AND it did not move between the two captures
(frozen) — i.e. the agent is wedged on "Login expired * Please run /login",
not merely discussing or quoting it. Exit code is 1 when any agent is
LOGIN-REQUIRED so cron / CI can alarm.
"""

from __future__ import annotations

import json as json_mod
import subprocess
import sys
import time

import click
from rich.table import Table

from .._runners._tmux.auth_status import evaluate, probe_to_state
from ._helpers import _json_flag, console

# The TUI runtime names its sessions ``tui-<agent>`` on the DEFAULT tmux server
# (``runtimes/tui_session.session_name_for``) — NOT the ``-L sac`` server that
# ``_runners/_tmux/pane_capture`` targets — so we enumerate + capture on the
# server the live fleet actually runs on.
_TUI_PREFIX = "tui-"


def _list_tui_sessions() -> list[str]:
    """Live ``tui-<agent>`` tmux sessions on the default server (sorted)."""
    # stx-allow: fallback (reason: tmux may be absent or have no server; an
    # empty list is the correct "nothing to check" result, never a crash)
    try:
        out = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # stx-allow: fallback (reason: catch-all — see comment above)
        return []
    if out.returncode != 0:
        return []
    return sorted(s for s in out.stdout.split() if s.startswith(_TUI_PREFIX))


def _capture(session: str) -> str | None:
    """Capture a session's visible pane; ``None`` on any error.

    Reuses the capture SHAPE of ``pane_capture`` (``-p -J`` — ``-J`` joins a
    banner wrapped across physical lines) but on the default server + ``tui-``
    session where the live fleet runs. ``None`` lets the matcher distinguish
    "uncapturable" from "clean pane".
    """
    # stx-allow: fallback (reason: the session can vanish between list and
    # capture; None is the honest "could not read" sentinel — never a crash)
    try:
        out = subprocess.run(
            ["tmux", "capture-pane", "-t", session, "-p", "-J"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:  # stx-allow: fallback (reason: catch-all — see comment above)
        return None
    return out.stdout if out.returncode == 0 else None


def _agent_of(session: str) -> str:
    return session[len(_TUI_PREFIX) :]


def evaluate_agents(
    captures: dict[str, tuple[str | None, str | None]],
) -> list[dict]:
    """Pure core: map ``agent -> (pane_run1, pane_run2)`` to verdict rows.

    Run 1 seeds each agent's local state; run 2 is judged against it, so a
    banner that stayed at the SAME distance across the two reads (frozen)
    yields ``login_required``; a banner that moved — or none at all — is
    ``ok``. Kept free of tmux so it is unit-testable against captured panes
    (no mocks). Rows are sorted by agent name for stable output.
    """
    rows: list[dict] = []
    for name in sorted(captures):
        pane1, pane2 = captures[name]
        probe1, _ = evaluate(pane1, None)
        probe2, stuck = evaluate(pane2, probe_to_state(probe1))
        present = probe2.present or probe1.present
        verdict = "login_required" if stuck else "ok"
        note = ""
        if verdict == "ok" and present:
            note = "banner seen but moving (working/quoting)"
        elif not probe2.prompt_found and pane2 is not None:
            note = "no prompt line found"
        rows.append(
            {
                "agent": name,
                "verdict": verdict,
                "banner_present": probe2.present,
                "distance": probe2.distance,
                "banner": probe2.banner,
                "captured": pane2 is not None,
                "note": note,
            }
        )
    return rows


def _render_table(rows: list[dict]) -> None:
    table = Table(title="TUI agent auth-status")
    table.add_column("agent", style="bold")
    table.add_column("status")
    table.add_column("distance", justify="right")
    table.add_column("banner")
    table.add_column("note", overflow="fold")
    for r in rows:
        login = r["verdict"] == "login_required"
        status = "LOGIN-REQUIRED" if login else "OK"
        style = "red" if login else "green"
        dist = "-" if r["distance"] is None else str(r["distance"])
        table.add_row(
            r["agent"],
            f"[{style}]{status}[/{style}]",
            dist,
            r["banner"] or "-",
            r["note"] or "",
        )
    console.print(table)


@click.command(name="auth-status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.option(
    "--interval",
    "interval",
    type=float,
    default=4.0,
    show_default=True,
    help="Seconds between the two pane captures used for the frozen check.",
)
@click.pass_context
def auth_status(ctx: click.Context, as_json: bool, interval: float) -> None:
    """Report each RUNNING TUI agent as OK or LOGIN-REQUIRED.

    Captures every live ``tui-<agent>`` pane twice (``--interval`` apart) and
    flags an agent only when a system auth banner sits directly above its
    prompt AND stays frozen across the two reads — so an agent QUOTING the
    banner while it works is never mistaken for a wedged one. Exits 1 if any
    agent needs login.

    \b
    Example:
      $ sac agents auth-status
      $ sac agents auth-status --json --interval 6
    """
    use_json = _json_flag(ctx, as_json)
    sessions = _list_tui_sessions()
    if not sessions:
        if use_json:
            click.echo(json_mod.dumps({"agents": [], "login_required": 0}))
        else:
            console.print("[dim](no running tui-* agents on this host)[/dim]")
        return
    run1 = {_agent_of(s): _capture(s) for s in sessions}
    time.sleep(max(0.0, interval))
    run2 = {_agent_of(s): _capture(s) for s in sessions}
    captures = {name: (run1.get(name), run2.get(name)) for name in run1}
    rows = evaluate_agents(captures)
    stuck = [r for r in rows if r["verdict"] == "login_required"]
    if use_json:
        click.echo(
            json_mod.dumps({"agents": rows, "login_required": len(stuck)}, indent=2)
        )
    else:
        _render_table(rows)
        if stuck:
            console.print(
                f"[red]{len(stuck)} agent(s) need login: "
                f"{', '.join(r['agent'] for r in stuck)}[/red]"
            )
    if stuck:
        sys.exit(1)


__all__ = ["auth_status", "evaluate_agents"]
