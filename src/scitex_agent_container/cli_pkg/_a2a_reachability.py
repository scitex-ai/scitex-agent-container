"""``sac a2a reachability`` — is the cross-host a2a transport up, per peer?

Split out of :mod:`.a2a_group` (over the per-file cap) and registered onto
it by :func:`register`, exactly as :mod:`._host_sync` attaches to ``sac
host``. The probe itself lives in :mod:`.._network._reachability`; this
module is the operator's window onto it and the scheduled job's entry
point (``scitex-agent-container-a2a-reachability``, every 15 minutes).

Rendering rule, inherited from ``sac host sync``: **there is no quiet
path.** Every host gets a line saying what was concluded and WHY —
including, above all, the ones that could not be probed. An UNKNOWN row
rendered as nothing is how a fleet with no peer tokens reads as healthy.
"""

from __future__ import annotations

import json

import click

from ._helpers import _json_flag, console

#: Colour per three-valued verdict. Anything that is not a measured
#: success is loud on purpose.
_STYLE = {
    True: ("green", "REACHABLE"),
    False: ("red", "UNREACHABLE"),
    None: ("magenta", "UNKNOWN"),
}


def _print_row(row) -> None:
    colour, label = _STYLE[row.reachable]
    ms = f"{row.elapsed_ms} ms" if row.elapsed_ms is not None else "-"
    alias = f"ssh://{row.ssh_alias}" if row.ssh_alias else "(no ssh alias)"
    # soft_wrap: a wrapped host name is one you cannot grep out of a log.
    console.print(
        f"[{colour}]{label:<12}[/{colour}] {row.host:<20} {alias:<28} {ms}",
        soft_wrap=True,
    )
    if row.error:
        console.print(f"    [dim]{row.error}[/dim]", soft_wrap=True)


def _print_report(report, *, alarm_line: str | None) -> None:
    console.print(
        f"[bold]sac a2a reachability[/bold]  from {report.probed_from} "
        f"-> {len(report.rows)} host(s), listen port {report.port}\n"
    )
    for row in report.rows:
        _print_row(row)
    counts = report.counts()
    console.print("")
    if report.exit_code == 3:
        console.print(
            "[magenta]nothing measurable[/magenta] — every host is UNKNOWN, so "
            "this pass proves nothing about the fleet. Fix the reasons above "
            "(peer tokens: `sac host add-peer <host> <token>`; aliases: "
            "hosts.yaml / config.yaml peers)."
        )
    elif report.exit_code == 1:
        down = [r.host for r in report.rows if r.reachable is False]
        console.print(
            f"[red]cross-host a2a is DOWN to {len(down)} host(s):[/red] "
            f"{', '.join(down)} — a2a_send to agents there fails from here."
        )
    else:
        console.print(
            f"[green]all {counts['reachable']} measured host(s) reachable[/green] "
            f"[dim]({counts['unknown']} unknown, listed above)[/dim]"
        )
    if alarm_line:
        console.print(f"[dim]{alarm_line}[/dim]")


@click.command("reachability")
@click.option(
    "--host",
    "hosts",
    multiple=True,
    help="Probe only this host (repeatable). Default: --all.",
)
@click.option(
    "--all",
    "all_hosts",
    is_flag=True,
    default=False,
    help="Every host the fleet knows (config.yaml peers + the host registry).",
)
@click.option(
    "--port",
    type=int,
    default=7878,
    show_default=True,
    help="The PEER's `sac listen` port to curl on its loopback.",
)
@click.option(
    "--timeout",
    "timeout_s",
    type=float,
    default=10.0,
    show_default=True,
    help="Per-host curl deadline on the peer (s); ssh gets 15 s on top.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit the report as JSON.")
@click.option(
    "--record",
    is_flag=True,
    default=False,
    help=(
        "Also write the report to the sac runtime dir "
        "(runtime/a2a-reachability.json) for `--last` to read back."
    ),
)
@click.option(
    "--last",
    "show_last",
    is_flag=True,
    default=False,
    help="Print the last --record'ed report instead of probing; exits with ITS code.",
)
@click.pass_context
def a2a_reachability(
    ctx: click.Context,
    hosts: tuple[str, ...],
    all_hosts: bool,
    port: int,
    timeout_s: float,
    as_json: bool,
    record: bool,
    show_last: bool,
) -> None:
    """Probe the cross-host a2a transport to every peer, from THIS host.

    Exercises exactly what ``sac listen``'s forwarder does for a cross-host
    send — ssh to the peer's alias, curl ``127.0.0.1:<port>/v1/health`` on
    the peer with that peer's bearer from ``peer-tokens/<host>.token`` —
    and reports one three-valued row per host.

    \b
    Verdicts, per host:
      REACHABLE    the leg worked end to end and a sac-listen answered
      UNREACHABLE  the leg was dispatched and failed (ssh, curl, or a bad answer)
      UNKNOWN      the leg could not be dispatched: no ssh alias in the host
                   registry, no peer token, or the host is this machine.
                   NEVER counted as reachable.
    \b
    Exit codes:
      0  every measured host is REACHABLE
      1  at least one host is UNREACHABLE
      3  nothing measurable — every host is UNKNOWN (not a success)
      (2 is Click's usage error and carries no fleet meaning)
    \b
    Every run also records each verdict in sac's own event log
    (runtime/sac-events.jsonl, subsystem a2a-reachability): unreachable and
    unknown hosts on every pass, a recovery on the transition back.
    \b
    Examples:
      $ sac a2a reachability
      $ sac a2a reachability --host scitex-compute-03 --json
      $ sac a2a reachability --all --json --record     # the scheduled form
      $ sac a2a reachability --last
    """
    from .._network._reachability import (
        read_report,
        run_probe,
        write_report,
    )
    from .._network._reachability_alarm import record_pass_completed, record_report

    if hosts and all_hosts:
        raise click.UsageError("give either --host ... or --all, not both")
    if show_last and (hosts or all_hosts or record):
        raise click.UsageError(
            "--last reads the recorded report; it takes no probe options"
        )
    if port <= 0:
        raise click.UsageError(f"--port must be positive (got {port})")
    if timeout_s <= 0:
        raise click.UsageError(f"--timeout must be positive (got {timeout_s})")

    if show_last:
        report = read_report()
        if report is None:
            from .._network._reachability import default_report_path

            raise click.ClickException(
                f"no recorded report at {default_report_path()} — run "
                "`sac a2a reachability --all --record` first"
            )
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(report.to_dict(), indent=2))
        else:
            _print_report(report, alarm_line=None)
        raise SystemExit(report.exit_code)

    from .._state.host_config import load as _load_cfg
    from .lifecycle._host_identity import _local_host_names

    cfg = _load_cfg()
    probed_from = cfg.canonical_host()
    try:
        report = run_probe(
            peers=cfg.peers,
            local_names=_local_host_names(probed_from),
            probed_from=probed_from,
            only=list(hosts) if hosts else None,
            port=port,
            timeout_s=timeout_s,
        )
    except KeyError as exc:
        raise click.UsageError(str(exc.args[0]) if exc.args else str(exc)) from exc

    # Make the shout DURABLE, in both output modes: the record is independent
    # of whatever the console prints. A subset run only touches the hosts it
    # probed, so a by-hand `--host x` can never record a recovery for a peer
    # it did not look at.
    mode = "subset" if hosts else "all"
    outcome = record_report(report)
    record_pass_completed(report, mode=mode)

    recorded_to = None
    if record:
        recorded_to = write_report(report)

    if _json_flag(ctx, as_json):
        payload = report.to_dict()
        payload["alarm"] = {
            "degraded": list(outcome.degraded),
            "unknown": list(outcome.unknown),
            "recovered": list(outcome.recovered),
            "failed": list(outcome.failed),
        }
        payload["recorded_to"] = str(recorded_to) if recorded_to else None
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(report.exit_code)

    line = outcome.summary_line()
    if recorded_to:
        line += f"; report -> {recorded_to}"
    _print_report(report, alarm_line=line)
    raise SystemExit(report.exit_code)


def register(a2a_group) -> None:
    """Attach ``reachability`` to the parent ``a2a`` Click group."""
    a2a_group.add_command(a2a_reachability)


__all__ = ["a2a_reachability", "register"]
