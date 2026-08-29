"""``sac accounts pause`` / ``sac accounts resume`` — stop an account, keep it.

OPERATOR REQUEST 2026-08-26, verbatim::

    WYSU、u、Ke のほうは除外というか休止ってできますか？…また
    アカウント復活させるので…クオーターを見ながら無駄遣いをしないように
    止めたり再開したりしてるんですけど、なのでその休止の間も失敗しない
    ようにしてほしいんですよ。

He offered 「除外」 (exclusion) and rejected it, in the same sentence,
for 「休止」 — a PAUSE. The two verbs here are that word, and the word
is load-bearing: ``disable`` was the obvious English alternative and is
refused, because "disabled" reads as a capability judgement. That is
already what FORBIDDEN means (a measured 403 from the API), and reusing
its vocabulary here would collapse the two ideas this whole change
exists to keep apart. ``pause`` is also existing house vocabulary for a
deliberate, reversible stop — see the fleet-wide periodic pause in
:mod:`.._lifecycle._periodic_drive`.

WHY ``--reason`` IS REQUIRED
----------------------------
It is not decoration and it is not a nicety. This record has no expiry
(:mod:`.._creds._pause` explains why: a decision does not go stale
because nobody re-asserted it), so nothing will ever come along and
clean up a pause the operator has forgotten. The reason is the only
thing that distinguishes, months later, a deliberate rest from an
account somebody abandoned — they are otherwise the same file on disk.
The fleet already ruled the same way on ``scitex-cards``' ``parked``
field, for the same reason and in nearly the same words: "a park with
no stated reason is exactly the abandonment the sweep should still
catch." Whitespace-only is refused for that reason, not on principle.

WHY ``pause`` CAN REFUSE
------------------------
Pausing every usable account leaves the picker with nothing and every
``sac agents start`` preflight fails. So ``pause`` refuses when it
would leave zero accounts that read VALID right now, names them, and
``--yes`` overrides. What it deliberately does NOT do is make the
picker ignore a pause when everything is paused: that would be a gate
that cannot fail — the account would be paused right up until the
moment the pause mattered.

WHAT NEITHER VERB DOES
----------------------
Neither touches a credential, a token, an entitlement verdict or a
directory. ``pause`` writes one small JSON file; ``resume`` deletes it.
That is the whole of it, and it is why 「また復活させる」 costs one
command. In particular the entitlement probe keeps running THROUGH a
pause on purpose, so the subscription verdict underneath is already
current the moment the operator resumes.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

__all__ = ["register_pause_commands"]


def _who() -> str:
    """``user@host`` for the audit trail. Best-effort, never a secret.

    FALLS THROUGH THE OS, NOT STRAIGHT TO A LITERAL. Reviewed
    2026-08-26: the first cut read only ``$USER`` / ``$LOGNAME`` and
    recorded ``unknown@<host>`` when neither was set — which is the
    NORMAL case for anything run from systemd, cron or a bare container
    shell, and was in fact what the reviewer's own run wrote. A pause
    has no expiry, so months later this field and the reason are the
    only two things separating a deliberate rest from an abandoned
    account; answering half of that with the string "unknown", and
    doing so most reliably in non-interactive contexts, is the wrong
    way to be approximate. :func:`getpass.getuser` consults ``pwd``
    when the environment is silent.
    """
    import getpass
    import socket

    # stx-allow: fallback (reason: an unresolvable uid or hostname must
    # not stop an operator from pausing an account; both fields are
    # audit hints, and the reason is the load-bearing one.)
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = "unknown"
    try:
        host = socket.gethostname()
    except OSError:
        host = "unknown-host"
    return f"{user}@{host}"


def _known_accounts() -> list[str]:
    from .._state.account_store import list_accounts

    return sorted(str(a.get("name", "")) for a in list_accounts() if a.get("name"))


def _resolve_account_dir(name: str) -> Path:
    """The account's directory, or raise a click error naming the real ones.

    MEMBERSHIP IN THE ENUMERATOR, NOT MERE EXISTENCE ON DISK. Anything
    :func:`._state.account_store.list_accounts` does not name is
    refused, because that enumerator is what every reader of a pause
    goes through: ``sac accounts send-credentials --all`` reaches it via
    ``refresh_holder_accounts``, and ``sac accounts list`` calls it
    directly.

    Reviewed 2026-08-26, measured on a store with the real shape. The
    first cut checked ``account_dir.is_dir()``, which is true of things
    that are not accounts: ``pause anthropic`` (the PROVIDER dir),
    ``pause _backup`` and ``pause .swap-backup-20260815`` (store
    bookkeeping and editor litter) all exited 0 saying "paused", wrote
    a ``pause.json`` where nothing would ever read it, and the very
    next keepalive run still enumerated and still failed. A verb that
    reports a decision recorded somewhere inert is the wrong direction
    to degrade in for a change whose whole premise is that an authored
    decision must never be silently lost.

    This is reachable in the real store rather than hypothetical: the
    accounts are symlinked short names (``wyusuuke-gmail-com ->
    anthropic/wyusuuke-gmail-com``), so an ``ls`` shows ``anthropic``
    sitting among the account names, and ``anthropic`` used to pause
    successfully. Membership implies existence, so nothing legal is
    lost — ``_``/``.``-prefixed names are documented as never being
    account names, and a BARE account dir (no metadata, no credential)
    is still enumerated, so it stays pausable.
    """
    from .._state.account_store import _store_path

    known = _known_accounts()
    if name not in known:
        raise click.ClickException(
            f"unknown account '{name}' — this host stores: "
            f"{', '.join(known) if known else '(none)'}"
        )
    return _store_path(None, Path.home()) / name


def _still_servable(exclude: str) -> list[str]:
    """Accounts that read VALID right now, ignoring ``exclude``."""
    from .._creds._account_health import account_health

    out: list[str] = []
    for name in _known_accounts():
        if name == exclude:
            continue
        if account_health(name).state == "VALID":
            out.append(name)
    return out


def _would_strand_the_picker(name: str) -> bool:
    """Would pausing ``name`` take the LAST pickable account out of service?

    Both halves are load-bearing, and the second was missing.

    Reviewed 2026-08-26: the guard asked only "is anything ELSE
    VALID?", so it fired on a target that was not VALID either — and
    then stated, as its reason, that the target "currently reads
    VALID". Measured on a store holding one EXPIRED and one FORBIDDEN
    account: ``pause <the FORBIDDEN one>`` was refused, both clauses of
    the refusal false, and the operator had to reach for ``--yes`` to
    do something that could not have broken anything.

    That is precisely the account class this feature exists for. His
    FORBIDDEN account is the one he wants to rest, and the fleet's
    others read EXPIRED between keepalive passes, so the real workflow
    walked straight into a refusal built for a different situation.

    An account that is not VALID cannot be picked, so pausing it
    removes nothing from the picker's set and there is nothing to
    refuse. The guard now asks the TARGET's own state first.
    """
    from .._creds._account_health import account_health

    if account_health(name).state != "VALID":
        return False
    return not _still_servable(name)


def register_pause_commands(group: click.Group) -> None:
    """Attach ``pause`` and ``resume`` onto the accounts group."""

    @group.command("pause")
    @click.argument("name")
    @click.option(
        "--reason",
        required=True,
        help=(
            "WHY this account is being rested, in your own words. Required, "
            "and whitespace-only is refused. A pause never expires, so this "
            "sentence is the only thing that will tell you months from now "
            "whether the account is resting or forgotten — and it is what "
            "every keepalive run prints back at you until you resume it."
        ),
    )
    @click.option(
        "--yes",
        "-y",
        is_flag=True,
        default=False,
        help=(
            "Pause even when it would leave NO account reading VALID. "
            "Without it that case is refused, because an all-paused store "
            "fails every agent boot."
        ),
    )
    def account_pause(name: str, reason: str, yes: bool) -> None:
        """Stop using an account WITHOUT deleting it — a decision, not a fault.

        For the workflow of stopping a subscription and putting it back
        later. Nothing is deleted and no credential is touched;
        `sac accounts resume NAME` puts it back in one command.

        EXACTLY WHAT A PAUSE STOPS, stated narrowly on purpose. No NEW
        agent boot picks the account, quota rotation never lands on it,
        its credential is no longer refreshed or pushed to peers, and
        both credential timers report it SKIPPED instead of failing.

        WHAT IT DOES NOT STOP: an agent already running on that
        credential keeps its token until it restarts, a peer keeps
        serving the copy it already has until that copy expires (hours),
        and `sac accounts switch` will still put the account on this
        host's live login if you ask it to by name.

        A pause is never discovered and never lifted by any probe.
        `sac accounts probe-entitlement` keeps recording whether the
        subscription is live underneath, so that verdict is already
        current when you resume — but only these verbs, run by you, can
        pause or resume.

        A pause does NOT expire. Nothing will un-pause it for you; that
        is what makes it trustworthy, and it is why --reason is
        required.

        It is written HERE, on this host, beside this host's copy of the
        credential — the path is printed. Peers stop receiving the
        credential immediately and drop the account when their copy
        expires.
        """
        from .._creds._pause import Pause, pause_path, read_pause, write_pause

        if not reason.strip():
            raise click.ClickException(
                "--reason is empty. A pause with no stated reason is "
                "indistinguishable from an abandoned account, and nothing "
                "will ever expire it for you. Say why."
            )

        account_dir = _resolve_account_dir(name)
        existing = read_pause(name, account_dir)
        if existing.problem:
            click.echo(
                f"note: the existing pause record was not usable "
                f"({existing.problem}). Overwriting it.",
                err=True,
            )

        if not yes and not existing.active and _would_strand_the_picker(name):
            raise click.ClickException(
                f"refusing to pause '{name}': it is the last stored "
                "account that currently reads VALID, so pausing it "
                "would leave the picker with nothing and fail every "
                "agent boot. Pass --yes if that is what you want."
            )

        record = Pause(
            name=name,
            active=True,
            reason=reason.strip(),
            since=time.time(),
            by=_who(),
        )
        if not write_pause(account_dir, record):
            raise click.ClickException(
                f"could not write {pause_path(account_dir)} — the account is "
                "NOT paused. Check the store's permissions and free space; "
                "reporting a pause we failed to record would be the one lie "
                "this command must not tell."
            )
        click.echo(
            f"paused {name}: {record.reason}\n"
            f"  record: {pause_path(account_dir)}\n"
            f"  nothing was deleted; `sac accounts resume {name}` lifts it."
        )

    @group.command("resume")
    @click.argument("name")
    def account_resume(name: str) -> None:
        """Put a paused account back in service. The inverse of `pause`.

        Prints the reason that was lifted, how long the pause stood, and
        what the account's health reads NOW — a resumed account whose
        subscription is still cancelled reads FORBIDDEN, not VALID. That
        is a different problem with a different fix (restore the
        subscription; the entitlement probe picks it up within 30
        minutes on its own).
        """
        from .._creds._account_health import account_health
        from .._creds._pause import clear_pause, pause_path, read_pause

        account_dir = _resolve_account_dir(name)
        existing = read_pause(name, account_dir)
        # :func:`clear_pause` deliberately propagates every OSError that
        # is not FileNotFoundError, because a permission-denied unlink
        # means the pause is STILL THERE and calling that "nothing to
        # lift" would be the lie in the other direction. That decision
        # is right and the RENDERING of it was missing: without this,
        # the one verb whose whole promise is that 「また復活させる」
        # costs one command ended in a Python stack trace on a
        # read-only store.
        try:
            removed = clear_pause(account_dir)
        except OSError as exc:
            raise click.ClickException(
                f"could not remove {pause_path(account_dir)} ({exc}) — "
                f"'{name}' is STILL PAUSED. Fix the store's permissions "
                "and re-run; nothing else was changed."
            ) from exc

        if not removed:
            click.echo(f"{name} was not paused — nothing to lift.")
        elif existing.active:
            click.echo(
                f"resumed {name} after {existing.age_human()} — lifted: "
                f"{existing.reason}"
            )
        else:
            click.echo(
                f"resumed {name}: removed an unusable pause record "
                f"({existing.problem or 'no reason recorded'}), which was "
                "not holding the account back anyway."
            )

        # State the RESULT, never imply it. A pause was one reason this
        # account was out of service; it may not have been the only one,
        # and an operator told "resumed" who then watches it keep failing
        # has been answered about the wrong question.
        health = account_health(name)
        click.echo(f"  health now: {health.state}")
        if health.state == "FORBIDDEN":
            click.echo(
                "  the pause is lifted, but the API still refuses this "
                f"account: {health.entitlement_detail}. Restore the "
                "subscription — probe-entitlement clears this on its own "
                "within 30 minutes."
            )
