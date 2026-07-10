"""``sac accounts login`` — semi-automated ``claude /login`` re-auth.

Split out of ``account_group.py`` to keep that orchestrator under the
per-file line cap; the command is attached onto the ``account`` group at
import time via :func:`register_login_command` (same pattern as
``_account_refresh.register_refresh_command``).

The heavy lifting — driving the interactive ``claude`` in a tmux pane,
extracting + delivering the OAuth URL, and awaiting completion — lives in
:mod:`scitex_agent_container._account.interactive_login`. This wrapper is
a thin click front that, on success, reuses the existing ``sac accounts
save`` logic to snapshot the fresh credential and then prints its expiry.

The account NAME and the (public) auth URL are the only things ever
emitted — never a token/credential value.
"""

from __future__ import annotations

import click

__all__ = ["account_login", "register_login_command"]


def _print_expiry(name: str) -> None:
    """Print the freshly-saved credential's expiry (never the token)."""
    from datetime import datetime, timezone

    from .._account.credentials import read_credentials_metadata

    # stx-allow: fallback (reason: the login + save already succeeded;
    # a cosmetic expiry read that hiccups must not turn a good login into
    # a non-zero exit — degrade to a plain "saved" line.)
    try:
        meta = read_credentials_metadata()
        expires_ms = meta.get("oauth_expires_at")
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        expires_ms = None
    if isinstance(expires_ms, int):
        expires_at = datetime.fromtimestamp(expires_ms / 1000, tz=timezone.utc)
        hours = (expires_at - datetime.now(timezone.utc)).total_seconds() / 3600
        click.echo(
            f"[sac accounts login] account '{name}' credential expires "
            f"{expires_at.isoformat()} (~{hours:.1f}h from now)."
        )
    else:
        click.echo(
            f"[sac accounts login] account '{name}' saved (expiry unavailable)."
        )


@click.command("login")
@click.argument("name")
@click.option(
    "--notify/--no-notify",
    "notify",
    default=True,
    show_default=True,
    help=(
        "Push the auth URL to the operator via the notify rail (in addition "
        "to always printing it to stdout). --no-notify prints to stdout only."
    ),
)
@click.option(
    "--code-file",
    "code_file",
    type=click.Path(),
    default=None,
    help=(
        "Path polled for the pasted authorization code when the CLI uses the "
        "code-paste flow. A remote deliverer (the operator / a bridge) writes "
        "the code here; the first non-empty line is typed into the pane."
    ),
)
@click.option(
    "--timeout",
    "human_timeout",
    type=float,
    default=600.0,
    show_default=True,
    help="Seconds to wait for the human browser/code step before failing loud.",
)
@click.option(
    "--url-timeout",
    "url_timeout",
    type=float,
    default=120.0,
    show_default=True,
    help="Seconds to wait for the OAuth URL to appear after /login.",
)
@click.option(
    "--claude-bin",
    "claude_bin",
    default="claude",
    show_default=True,
    help="The claude executable to drive (override for testing / alt installs).",
)
@click.option(
    "--workdir",
    "workdir",
    default=None,
    help="Working directory for the claude session (defaults to $HOME).",
)
@click.option(
    "--save/--no-save",
    "save",
    default=True,
    show_default=True,
    help="On success, snapshot the fresh credential into the account store.",
)
def account_login(
    name: str,
    notify: bool,
    code_file: str | None,
    human_timeout: float,
    url_timeout: float,
    claude_bin: str,
    workdir: str | None,
    save: bool,
) -> None:
    """Re-authenticate account NAME via a semi-automated ``claude /login``.

    Drives ``claude`` in a tmux pane, extracts the OAuth authorize URL and
    delivers it to the operator (stdout + notify rail), then waits for the
    login to complete — either the browser-only flow or the code-paste
    flow (supply the code via --code-file, $SAC_LOGIN_CODE, or an
    interactive prompt). On success the fresh credential is snapshotted
    with the existing ``sac accounts save`` logic and its expiry printed.

    \b
    Examples:
      $ sac accounts login work
      $ sac accounts login work --code-file /run/sac/login-code.txt
      $ sac accounts login work --no-notify --timeout 300
    """
    from .._account.interactive_login import LoginError, run_interactive_login

    try:
        run_interactive_login(
            name,
            notify=notify,
            code_file=code_file,
            human_timeout_s=human_timeout,
            url_timeout_s=url_timeout,
            claude_bin=claude_bin,
            workdir=workdir,
            echo=click.echo,
        )
    except LoginError as exc:
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc

    if not save:
        click.echo(f"[sac accounts login] --no-save: skipped snapshotting '{name}'.")
        return

    # Reuse the existing `sac accounts save` logic (import the function,
    # do not shell out) so the fresh ~/.claude/.credentials.json lands in
    # the account store exactly as a manual `sac accounts save` would.
    from .account_group import account_save

    account_save.callback(name=name, email=None, dry_run=False, yes=True)
    _print_expiry(name)


def register_login_command(group: click.Group) -> None:
    """Attach the ``login`` command onto the ``account`` group."""
    group.add_command(account_login)
