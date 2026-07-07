"""``watch-quota`` CLI commands (extracted from ``account_group``).

Both the ``account watch-quota`` subcommand and the legacy top-level
``quota_watch`` command live here to keep ``account_group.py`` under the
per-file line cap. ``account_group`` re-exports ``quota_watch`` so the
lazy entry-point path ``account_group:quota_watch`` (see ``_main.py``)
keeps resolving unchanged.
"""

from __future__ import annotations

import click


@click.command("watch-quota")
@click.option(
    "--threshold",
    default=80.0,
    show_default=True,
    help="Rotate when usage exceeds this %.",
)
@click.option(
    "--interval",
    default=300,
    show_default=True,
    help="Check interval in seconds.",
)
@click.option("--dry-run", is_flag=True, help="Check but do not actually rotate.")
@click.option("--once", is_flag=True, help="Run once instead of looping.")
@click.option(
    "--daemon",
    is_flag=True,
    help="Double-fork into background (UNIX only). Logs to --log-file.",
)
@click.option(
    "--log-file",
    default=None,
    show_default=False,
    help="Log file path when running as daemon (default: ~/.scitex/logs/quota-watch.log).",
)
def quota_watch(
    threshold: float,
    interval: int,
    dry_run: bool,
    once: bool,
    daemon: bool,
    log_file: str | None,
) -> None:
    """Monitor quota and auto-rotate credentials when threshold exceeded.

    \b
    Examples:
      # single check
      scitex-agent-container watch-quota --once
      # foreground loop every 5 min
      scitex-agent-container watch-quota
      # background daemon
      scitex-agent-container watch-quota --daemon
    """
    from pathlib import Path

    from .._account.quota_watch import check_and_rotate, run_loop, survival_mode_check

    if once or dry_run:
        result = check_and_rotate(threshold=threshold, dry_run=dry_run)
        click.echo(f"[{result['action']}] {result['message']}")
        # Also report survival mode in single-check mode
        sv = survival_mode_check()
        if sv["survival_mode"]:
            click.echo(f"[SURVIVAL] {sv['message']}", err=True)
        return

    log_path = Path(log_file) if log_file else None
    if daemon:
        click.echo(
            f"Forking quota-watch daemon (interval={interval}s, threshold={threshold}%). "
            f"Log: {log_path or '~/.scitex/logs/quota-watch.log'}"
        )
    run_loop(
        threshold=threshold,
        interval=interval,
        daemon=daemon,
        log_path=log_path,
    )


def register_quota_watch_commands(group: click.Group) -> None:
    """Attach the ``account watch-quota`` subcommand onto ``group``."""

    @group.command("watch-quota")
    @click.option(
        "--threshold",
        default=80.0,
        show_default=True,
        help="Rotate when usage exceeds this %.",
    )
    @click.option(
        "--interval",
        default=300,
        show_default=True,
        help="Check interval in seconds.",
    )
    @click.option("--dry-run", is_flag=True, help="Check but do not actually rotate.")
    @click.option("--once", is_flag=True, help="Run once instead of looping.")
    @click.option(
        "--daemon",
        is_flag=True,
        help="Double-fork into background (UNIX only). Logs to --log-file.",
    )
    @click.option(
        "--log-file",
        default=None,
        show_default=False,
        help="Log file path when running as daemon (default: ~/.scitex/logs/quota-watch.log).",
    )
    def account_watch_quota(
        threshold: float,
        interval: int,
        dry_run: bool,
        once: bool,
        daemon: bool,
        log_file: str | None,
    ) -> None:
        """Monitor quota and auto-rotate credentials when threshold exceeded.

        \b
        Examples:
          $ sac account watch-quota --once
          $ sac account watch-quota
          $ sac account watch-quota --daemon
        """
        from pathlib import Path

        from .._account.quota_watch import (
            check_and_rotate,
            run_loop,
            survival_mode_check,
        )

        if once or dry_run:
            result = check_and_rotate(threshold=threshold, dry_run=dry_run)
            click.echo(f"[{result['action']}] {result['message']}")
            sv = survival_mode_check()
            if sv["survival_mode"]:
                click.echo(f"[SURVIVAL] {sv['message']}", err=True)
            return

        log_path = Path(log_file) if log_file else None
        if daemon:
            click.echo(
                f"Forking quota-watch daemon (interval={interval}s, threshold={threshold}%). "
                f"Log: {log_path or '~/.scitex/logs/quota-watch.log'}"
            )
        run_loop(
            threshold=threshold,
            interval=interval,
            daemon=daemon,
            log_path=log_path,
        )


__all__ = ["quota_watch", "register_quota_watch_commands"]
