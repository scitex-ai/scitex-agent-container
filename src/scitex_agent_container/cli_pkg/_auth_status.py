"""``sac agents auth-status`` — which running TUI agents cannot reach the API.

The reliable, prompt-anchored auth health check (TG 1497: "a command to see if
any running agent shows login-required"). For each live ``tui-<agent>`` tmux
session it captures the pane TWICE (``--interval`` apart), runs the near-prompt
+ distance-frozen matcher (:mod:`.._runners._tmux.auth_status`), and prints OK /
AUTH-FAILED. Exit code is 1 when any agent is failing, so cron / CI can alarm.

AUTH-FAILED means: a SYSTEM auth-rejection banner sits in the conversation tail
directly above the input prompt AND it did not move between the two captures
(frozen) — i.e. the agent is genuinely wedged, not merely discussing or quoting
the incident.

It does NOT mean "the token expired", and this command will not say so. Claude
Code renders every 401 as ``Login expired · Please run /login``; on this fleet
that text is usually FALSE (a sibling agent's OAuth refresh consumed the
single-use refresh_token and REVOKED the token this one still held in memory —
nothing expired, and a restart, not a login, is the cure). So we report the
verifiable fact — *this agent cannot authenticate* — and diagnose the CAUSE
separately, from ``expiresAt`` (:mod:`.._account.auth_failure_reason`).

THIS COMMAND IS THE WRITER. Detection is expensive (two pane captures, seconds
apart, for every agent), so ``sac agents list`` must never do it inline. Instead
each verdict is PERSISTED here (:func:`persist_verdicts` → the
``agent_auth_state`` table) and the list simply READS that cache. Run this on a
timer; the list then shows, for free, which green agents are actually working —
and how old that evidence is.
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

    Run 1 seeds each agent's local state; run 2 is judged against it, so a banner
    that stayed at the SAME distance across the two reads (frozen) yields
    ``auth_failed``; a banner that moved — or none at all — is ``ok``. Kept free
    of tmux so it is unit-testable against captured panes (no mocks). Rows are
    sorted by agent name for stable output.
    """
    rows: list[dict] = []
    for name in sorted(captures):
        pane1, pane2 = captures[name]
        probe1, _ = evaluate(pane1, None)
        probe2, stuck = evaluate(pane2, probe_to_state(probe1))
        present = probe2.present or probe1.present
        verdict = "auth_failed" if stuck else "ok"
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


def _config_for(name: str):
    """Load ``name``'s spec so its credential file can be located; else ``None``.

    ``None`` is a SAFE answer, not a failure: ``credential_path_for(None)``
    resolves to the host live ``~/.claude/.credentials.json``, which is exactly
    what an agent with no pinned ``spec.claude.account`` authenticates with — the
    common case. Only a PINNED agent needs its spec read, and for those the
    registry lookup below is what supplies it.
    """
    # stx-allow: fallback (reason: the spec is used only to LOCATE a credential
    # file for the cause diagnosis; an unreadable one degrades to the host live
    # file, never to a failed watchdog run.)
    try:
        from .._state.registry import Registry
        from ..config import load_config

        entry = Registry().get(name) or {}
        path = entry.get("config")
        return load_config(str(path)) if path else None
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None


def diagnose_agents(rows: list[dict], *, now: float | None = None) -> list[dict]:
    """Annotate each FAILING row with WHY it fails and what actually fixes it.

    Adds ``reason`` (revoked / expired / unknown) and ``remedy`` (restart /
    login) to the auth-failed rows by comparing the agent's on-disk credential
    ``expiresAt`` against now — see :mod:`.._account.auth_failure_reason` for why
    that comparison is decisive, and why it is worth far more than the banner's
    own (usually wrong) explanation.

    Healthy rows get an empty reason. A valid credential is the NORMAL state, so
    "diagnosing" a working agent would confidently report ``revoked`` about
    nothing at all. Only what is broken gets explained.
    """
    from .._account.auth_failure_reason import diagnose_reason, remedy_for

    for row in rows:
        if row.get("verdict") != "auth_failed":
            row["reason"] = ""
            row["remedy"] = ""
            continue
        reason = diagnose_reason(_config_for(row["agent"]), now=now)
        row["reason"] = reason
        row["remedy"] = remedy_for(reason)
    return rows


def persist_verdicts(rows: list[dict], *, db_path=None) -> int:
    """Cache these verdicts in state.db so ``sac agents list`` can READ them.

    The write half of the whole feature: detection is far too expensive to run
    per row of an agent listing, so it happens HERE, once, and the list reads
    what we leave behind.

    Only agents whose pane was genuinely CAPTURED are written. An agent we could
    not read produced no evidence, and recording "auth is fine" for it would
    manufacture exactly the false green this feature exists to abolish. Leaving
    its previous row to age — and be marked stale by the reader — is the honest
    outcome.

    Tolerant: a failed write warns on stderr and is never raised. The operator
    ran this to LEARN something; a state.db hiccup must not throw away a
    completed scan or make the command exit non-zero for the wrong reason.
    """
    observed = [r for r in rows if r.get("captured")]
    if not observed:
        return 0
    # stx-allow: fallback (reason: the scan already SUCCEEDED; a cache-write
    # hiccup must not fail the command or discard the operator's result.)
    try:
        from .._state.auth_state import record_auth_checks

        return record_auth_checks(
            [
                {
                    "name": r["agent"],
                    "auth_failed": r.get("verdict") == "auth_failed",
                    "banner": r.get("banner"),
                    "reason": r.get("reason") or "",
                    "note": r.get("note") or "",
                }
                for r in observed
            ],
            db_path=db_path,
        )
    except Exception as exc:  # stx-allow: fallback (reason: see inline comment)
        click.echo(
            f"warning: could not cache auth verdicts ({exc}); "
            "`sac agents list` will show them as never-checked",
            err=True,
        )
        return 0


def _render_table(rows: list[dict]) -> None:
    table = Table(title="TUI agent auth-status")
    table.add_column("agent", style="bold")
    table.add_column("status")
    table.add_column("cause")
    table.add_column("fix")
    table.add_column("banner")
    table.add_column("note", overflow="fold")
    for r in rows:
        failed = r["verdict"] == "auth_failed"
        status = "AUTH-FAILED" if failed else "OK"
        style = "red" if failed else "green"
        table.add_row(
            r["agent"],
            f"[{style}]{status}[/{style}]",
            r.get("reason") or "-",
            r.get("remedy") or "-",
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
    """Report each RUNNING TUI agent as OK or AUTH-FAILED, and cache the result.

    Captures every live ``tui-<agent>`` pane twice (``--interval`` apart) and
    flags an agent only when a system auth banner sits directly above its prompt
    AND stays frozen across the two reads — so an agent QUOTING the banner while
    it works is never mistaken for a wedged one. Each failure is then diagnosed
    from the agent's credential ``expiresAt`` as ``revoked`` (its token was
    rotated away by a sibling's refresh → restart it) or ``expired`` (→ log in).

    Every verdict is written to state.db, which is where ``sac agents list``
    reads its auth column from — so running this on a timer is what keeps the
    fleet view honest. Exits 1 if any agent's auth is failing.

    \b
    Example:
      $ sac agents auth-status
      $ sac agents auth-status --json --interval 6
    """
    use_json = _json_flag(ctx, as_json)
    sessions = _list_tui_sessions()
    if not sessions:
        if use_json:
            click.echo(json_mod.dumps({"agents": [], "auth_failed": 0}))
        else:
            console.print("[dim](no running tui-* agents on this host)[/dim]")
        return
    run1 = {_agent_of(s): _capture(s) for s in sessions}
    time.sleep(max(0.0, interval))
    run2 = {_agent_of(s): _capture(s) for s in sessions}
    captures = {name: (run1.get(name), run2.get(name)) for name in run1}
    rows = diagnose_agents(evaluate_agents(captures))
    persist_verdicts(rows)
    stuck = [r for r in rows if r["verdict"] == "auth_failed"]
    if use_json:
        click.echo(json_mod.dumps({"agents": rows, "auth_failed": len(stuck)}, indent=2))
    else:
        _render_table(rows)
        if stuck:
            console.print(
                f"[red]{len(stuck)} agent(s) cannot authenticate: "
                f"{', '.join(r['agent'] for r in stuck)}[/red]"
            )
    if stuck:
        sys.exit(1)


__all__ = [
    "auth_status",
    "diagnose_agents",
    "evaluate_agents",
    "persist_verdicts",
]
