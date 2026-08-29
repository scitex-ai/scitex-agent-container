"""``sac accounts sync-live`` and ``watch-live`` click commands.

Extracted from ``account_group.py`` to keep that file under the
per-file line cap. The credential auto-sync substrate (engine in
``_account.creds_sync``, watcher in ``_account.creds_watch``) is a
cohesive concern; its two CLI faces live here and are attached onto the
``account`` group via :func:`register_sync_live_commands`.
"""

from __future__ import annotations

import click


@click.command("sync-live")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a JSON object describing what happened.",
)
@click.option(
    "--poll",
    is_flag=True,
    default=False,
    help=(
        "Treat 'no snapshottable credential' as a no-op (exit 0) instead of a "
        "failure. For scheduled callers; a hand-run invocation wants the loud "
        "default."
    ),
)
def account_sync_live(as_json: bool, poll: bool) -> None:
    """Snapshot the live credential into its matching account store.

    Reads the live ``~/.claude/.credentials.json`` + the active email
    from ``~/.claude.json``, derives the store-name (email slugified),
    and snapshots the live cred in when the store is absent / older /
    expired. Idempotent — a no-op when the store already matches.

    Fails loud (non-zero exit) when the live credential is absent,
    malformed, or expired — never saves a stale token.

    ``--poll`` MAKES THAT LAST CASE A NO-OP INSTEAD, and the distinction is
    the point: "there is nothing to snapshot" is not the same event as
    "something went wrong". A human running this by hand has just logged in
    and wants to be told if the save failed, so the loud default is right for
    them. A TIMER has no such context, and on a host nobody logged into today
    the live credential is absent or expired as a matter of course.

    MEASURED 2026-08-26, which is why this flag exists. The 2-minute
    snapshot timer was armed on five hosts and only two had a usable live
    credential:
        compute-04  valid                 -> exit 0
        nas-03      valid                 -> exit 0
        compute-01  absent                -> exit 1
        compute-03  expired ~7.9 days     -> exit 1
        laptop-01   expired ~4.6 days     -> exit 1
    Three hosts would have failed every two minutes forever — 720 failures a
    day each — which does not protect anything and makes the unit's exit
    status meaningless. A job that is always red cannot report a NEW problem.
    The refusal itself was correct every time; wrapping a fail-loud one-shot
    in a poll was the error.

    Nothing about the SAFETY contract changes: a stale token is still never
    written, an identity change is still refused. Only the exit code for
    "nothing to do" moves, and only when the caller asks for it.

    \b
    Examples:
      $ sac accounts sync-live
      $ sac accounts sync-live --json
    """
    import json as _json

    from .._account.creds_sync import LiveCredInvalidError, sync_live

    try:
        result = sync_live()
    except LiveCredInvalidError as exc:
        if as_json:
            click.echo(
                _json.dumps(
                    {"action": "live-cred-invalid", "error": str(exc)},
                    ensure_ascii=False,
                )
            )
        else:
            # "nothing to snapshot" reads as a NOTE under --poll and as an
            # ERROR otherwise, so the log line matches the exit code rather
            # than contradicting it.
            label = "Nothing to snapshot" if poll else "Error"
            click.echo(f"{label}: {exc}", err=not poll)
        raise SystemExit(0 if poll else 1)

    if as_json:
        click.echo(
            _json.dumps(
                {
                    "action": result.action,
                    "store_name": result.store_name,
                    "email": result.email,
                    "live_expires_at": result.live_expires_at,
                    "store_expires_at": result.store_expires_at,
                },
                ensure_ascii=False,
            )
        )
        return
    if result.action == "saved":
        click.echo(
            f"Saved live credential into store '{result.store_name}' "
            f"(email={result.email})."
        )
    else:
        click.echo(
            f"Store '{result.store_name}' already up-to-date (email={result.email})."
        )


@click.command("watch-live")
@click.option(
    "--interval",
    default=None,
    type=float,
    show_default=False,
    help=(
        "Poll interval in seconds for the fallback loop "
        "(default: 2.0; ignored when inotifywait is available)."
    ),
)
@click.option(
    "--poll",
    "force_poll",
    is_flag=True,
    default=False,
    help="Force the poll loop even when inotifywait is on PATH.",
)
@click.option(
    "--log-file",
    default=None,
    help=(
        "Log sync attempts to this file (default: stderr). "
        "Canonical: ~/.scitex/agent-container/runtime/logs/creds-watch.log."
    ),
)
def account_watch_live(
    interval: float | None,
    force_poll: bool,
    log_file: str | None,
) -> None:
    """Watch the live credential and auto-sync on every change.

    The "moment I log in -> auto-saved" daemon. Watches
    ``~/.claude/.credentials.json`` (inotify when ``inotifywait`` is
    available, else a poll loop) and runs ``sync-live`` on every change.
    Safe to run as a long-lived background process; a transient
    expired/mid-rewrite live cred is logged and the watcher keeps going.

    \b
    Examples:
      $ sac accounts watch-live
      $ sac accounts watch-live --poll --interval 1
      $ sac accounts watch-live --log-file ~/.scitex/agent-container/runtime/logs/creds-watch.log
    """
    from pathlib import Path

    from .._account.creds_watch import DEFAULT_POLL_INTERVAL_S, run_watch

    log_path = Path(log_file).expanduser() if log_file else None
    run_watch(
        log_path=log_path,
        interval=interval if interval is not None else DEFAULT_POLL_INTERVAL_S,
        prefer_inotify=not force_poll,
    )


def register_sync_live_commands(group: click.Group) -> None:
    """Attach ``sync-live`` and ``watch-live`` onto the ``account`` group."""
    group.add_command(account_sync_live)
    group.add_command(account_watch_live)


__all__ = [
    "account_sync_live",
    "account_watch_live",
    "register_sync_live_commands",
]
