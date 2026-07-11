"""Status commands: status, list, health."""

from __future__ import annotations

import json as json_mod
import os
import sys

import click
from rich.table import Table

from .._lifecycle.health import health_check
from .._lifecycle.lifecycle import agent_status
from .._state.registry import Registry
from ..config import load_config
from ._helpers import (
    _json_flag,
    agent_name_complete,
    console,
    print_agent_list,
)


def _status_via_host_listen(name: str) -> None:
    """In-SIF per-agent status proxy — GET /agents/<name>/status.

    PR-3 Checkpoint 3 — the path the ``sac agents status <name>``
    CLI takes when running inside an apptainer SIF. Emits one
    :func:`_in_sif_outcome.outcome_to_stdout_json` line to stdout
    (the wire-stable shape pinned in Checkpoint 2) and exits with
    the table-mapped code.

    Never raises — every failure mode (transport, ACL deny, etc.)
    is mapped into an outcome with the structured ``kind`` tag the
    consumer branches on. The host's lineage-scoped ACL gate is
    the authority; an unauthorised caller gets ``kind=acl_deny``
    + exit 5.
    """
    from .._lifecycle._in_sif_http_client import (
        HostListenTransportError,
        host_listen_call,
    )
    from .._lifecycle._in_sif_outcome import (
        build_outcome,
        outcome_to_stdout_json,
        transport_outcome,
    )

    try:
        status, body = host_listen_call("GET", f"/agents/{name}/status")
        outcome = build_outcome(http_status=status, body=body)
    except HostListenTransportError as exc:
        outcome = transport_outcome(str(exc), url=exc.url)
    sys.stdout.write(outcome_to_stdout_json(outcome))
    sys.exit(outcome.exit_code)


def _encode_safe_cell(value: object, encoding: str) -> str:
    """Stringify ``value`` for a rich table cell, coerced to round-trip
    through ``encoding``.

    ``agent_status()`` surfaces free-form runtime content verbatim
    (``extensions``, tmux ``pane_text``, ``CLAUDE.md`` snippets, tool-input
    previews, ...) that can carry non-ASCII characters -- including the
    Claude Code TUI's own prompt glyph, which shows up in almost every
    live agent's captured pane text. When the process's stdout encoding
    is not UTF-8 (locale-stripped containers, cron, some SSH sessions --
    exactly this project's own apptainer SIF deployments), rendering that
    text raises ``UnicodeEncodeError`` partway through ``console.print``.
    Replacing unencodable characters here -- before the cell is ever
    added to the table -- prevents the crash instead of masking it with
    a broad try/except around the print call.
    """
    text = str(value)
    try:
        text.encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return text.encode(encoding, errors="replace").decode(encoding)
    return text


def _format_claude_account_block(meta: dict) -> list[str]:
    """Render the ``Claude Code account`` section as a list of text lines.

    Missing values render as ``-``. Returns ``[]`` if no fields are set
    (i.e. every value is ``None``) so the section is omitted entirely.
    """
    if not any(v is not None for v in meta.values()):
        return []

    def _fmt(value):
        return "-" if value is None else str(value)

    email = _fmt(meta.get("email_address"))
    org = _fmt(meta.get("organization_name"))
    display = _fmt(meta.get("display_name"))
    billing = _fmt(meta.get("billing_type"))
    sub_type = meta.get("subscription_type")
    tier = meta.get("rate_limit_tier")
    if sub_type is None and tier is None:
        sub_line = "-"
    else:
        sub_line = f"{_fmt(sub_type)}  (tier: {_fmt(tier)})"
    avail = meta.get("has_available_subscription")
    if avail is None:
        avail_line = "-"
    else:
        avail_line = "yes" if avail else "no"
    extra_enabled = meta.get("has_extra_usage_enabled")
    extra_reason = meta.get("cached_extra_usage_disabled_reason")
    if extra_enabled is None and extra_reason is None:
        extra_line = "-"
    elif extra_enabled:
        extra_line = "enabled"
    else:
        extra_line = "disabled"
        if extra_reason:
            extra_line += f" (reason: {extra_reason})"
    since = _fmt(meta.get("subscription_created_at"))

    return [
        "Claude Code account",
        f"  Email:          {email}",
        f"  Organization:   {org}",
        f"  Display name:   {display}",
        f"  Billing type:   {billing}",
        f"  Subscription:   {sub_line}",
        f"  Available:      {avail_line}",
        f"  Extra usage:    {extra_line}",
        f"  Since:          {since}",
    ]


@click.command(name="show-status")
@click.argument("name", required=False, shell_complete=agent_name_complete)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.option(
    "--terse",
    "terse",
    is_flag=True,
    default=False,
    help="Project JSON output onto the fleet_watch whitelist (todo#300). "
    "Implies --json. Reduces per-agent payload ~18x.",
)
@click.option(
    "--capability",
    "-c",
    default=None,
    help="Fleet view: filter by capability label (comma-separated in YAML).",
)
@click.option(
    "--machine",
    "-m",
    default=None,
    help="Fleet view: filter by machine label.",
)
@click.option(
    "--tags",
    "-t",
    default=None,
    help="Fleet view: filter by tags label (comma-separated in YAML; "
    "matches if the agent carries ANY of the given comma-separated "
    "values). A free-form lifecycle/status marker, e.g. "
    "'active-development' -- separate from --capability (what an agent "
    "can do) and from the ACL group label (metadata.labels.groups).",
)
@click.option(
    "--verbose",
    "-v",
    "verbose",
    is_flag=True,
    default=False,
    help="Fleet view: show the FULL list — every status "
    "(running/stopped/invalid/definition) WITH per-agent validation-error "
    "detail and the spec.yaml Path column. The default view shows only "
    "running agents (the full roster is an unusable wall on a real fleet).",
)
@click.option(
    "--all",
    "show_all",
    is_flag=True,
    default=False,
    help="Fleet view: like -v (full roster + validation detail) AND "
    "additionally include stale/ghost agents (dead registry entries whose "
    "spec file is gone). Both are hidden by default.",
)
@click.option(
    "--snapshot",
    "with_snapshot",
    is_flag=True,
    default=False,
    help="Per-agent: also take and persist a self-snapshot (with diff against prior).",
)
@click.option(
    "--priority",
    "with_priority",
    is_flag=True,
    default=False,
    help="Per-agent: also include a priority report (should this host yield to a higher-priority host?).",
)
@click.option(
    "--workdir-audit",
    "with_workdir_audit",
    is_flag=True,
    default=False,
    help=(
        "Per-agent: include the F-CS8 workdir audit (file count, total "
        "bytes, bloat-source subdirs) under the `workdir_audit` key. "
        "Surfaces silent SDK-discovery footprint without needing "
        "`find <workdir>/.claude/ -type f | wc -l`. See F-CS8."
    ),
)
@click.pass_context
def status(
    ctx: click.Context,
    name: str | None,
    as_json: bool,
    terse: bool,
    capability: str | None,
    machine: str | None,
    tags: str | None,
    verbose: bool,
    show_all: bool,
    with_snapshot: bool,
    with_priority: bool,
    with_workdir_audit: bool,
) -> None:
    """Show agent status.

    Without ``NAME``: fleet view — every registered agent in a table,
    optionally filtered by ``--capability`` / ``--machine`` / ``--tags``.

    With ``NAME``: rich per-agent payload (registry entry + config-derived
    fields + resource snapshot).

    \b
    Example:
      $ sac agent status                            # fleet view
      $ sac agent status orchestrator               # rich per-agent
      $ sac agent status --json                     # fleet view, JSON
      $ sac agent status --capability HPC           # fleet view, filtered
      $ sac agent status --tags active-development  # fleet view, by tag
    """
    use_json = _json_flag(ctx, as_json) or terse
    registry = Registry()

    if terse and not name:
        click.echo(
            json_mod.dumps(
                {"error": "--terse requires an agent NAME (per-agent mode only)"}
            )
        )
        sys.exit(2)

    if name:
        # PR-3 — in-SIF auto-fallback for per-agent status. When the
        # CLI is invoked inside an apptainer SIF, the local registry
        # is the SIF's own (not the host's), so a `sac agents status
        # <name>` for any host-side agent would error out. Auto-proxy
        # to `GET /agents/<name>/status` on the host listen, mapped
        # through the PR-3 outcome layer for stable stdout JSON +
        # exit code. The lineage-scoped ACL on the host side enforces
        # that the caller can only inspect itself or its lineage
        # descendants.
        #
        # PR#316 (lead msg 4cb474fc, clew L3 diag 2026-06-06): the
        # host-listen-call path needs SAC_LISTEN_BASE_URL. In sac-from-sac
        # L2 broker-self / bare-host-with-stale-SINGULARITY_CONTAINER
        # contexts that env var IS absent — the operator's shell isn't
        # a real apptainer container, and there's no host listen to
        # broker the read to. Pre-PR#316 this surfaced as
        # ``HostListenTransportError: in-SIF host-listen call requires
        # SAC_LISTEN_BASE_URL`` (a stillborn-read masquerading as a
        # transport error). Fix: degrade gracefully — fall through to
        # the local registry read when the env var is missing. The
        # local read picks up the agent's instances/state.db row
        # directly; on the broker-self happy path the agent's runtime
        # dir is bind-visible to the same state.db the local read uses.
        from .._lifecycle._in_sif_broker import is_in_sif

        if is_in_sif() and (os.environ.get("SAC_LISTEN_BASE_URL") or "").strip():
            _status_via_host_listen(name)
            return  # noreturn — _status_via_host_listen sys.exits

        # stx-allow: fallback (reason: agent_status queries registry and multiplexer state that may be unavailable; CLI exits with code 1 and reports the error in the requested format)
        try:
            info = agent_status(name)
        except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            if use_json:
                click.echo(json_mod.dumps({"error": str(exc)}))
            else:
                console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        if with_snapshot:
            from .._state.snapshot import take_snapshot

            # stx-allow: fallback (reason: snapshot capture is best-effort; status output should still be produced)
            try:
                info["snapshot"] = take_snapshot(name, with_diff=True)
            except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                info["snapshot_error"] = str(exc)

        if with_workdir_audit:
            # F-CS8 surface — fleet sweep 2026-06-03 showed two distinct
            # bloat types (worktrees + hooks/pre-tool-use/.pending) that
            # silently trip SDK auto-discovery. Expose the per-agent
            # audit so operators can spot bloat without spelunking via
            # `find <workdir>/.claude/ -type f | wc -l`.
            from .._workdir_audit import audit_workdir_claude
            from .._workdir_audit import to_dict as _audit_to_dict

            workdir = info.get("expanded_workdir") or info.get("workdir") or ""
            # stx-allow: fallback (reason: workdir audit walks a real fs
            # tree; permission-denied or network-stale entries should not
            # break the status command — surface as audit_error)
            try:
                info["workdir_audit"] = _audit_to_dict(audit_workdir_claude(workdir))
            except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                info["workdir_audit_error"] = str(exc)

        if with_priority:
            from ..config._host import resolve_hostname
            from .priority_cmds import _priority_report

            # stx-allow: fallback (reason: priority report involves SSH probes; missing/unreachable peers should not break status)
            try:
                entry = registry.get(name)
                config_path = entry["config"] if entry else name
                # stx-allow: fallback (reason: hostname resolution may fail in odd network environments)
                try:
                    current_host = resolve_hostname()
                except Exception:
                    import socket

                    current_host = socket.gethostname().split(".")[0]
                info["priority"] = _priority_report(config_path, current_host)
            except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                info["priority_error"] = str(exc)

        if use_json:
            if terse:
                from ..terse import TERSE_STATUS_FIELDS, project_terse

                info = project_terse(info, TERSE_STATUS_FIELDS)
            click.echo(json_mod.dumps(info, indent=2, default=str))
            return

        table = Table(title=f"Agent: {name}")
        table.add_column("Field", style="bold")
        # overflow="fold" (not the default "ellipsis"): a too-wide cell
        # (e.g. a long config path) is hard-wrapped instead of truncated
        # with Rich's own "…" marker, which is itself non-ASCII and would
        # defeat the encode-safety below.
        table.add_column("Value", overflow="fold")
        cell_encoding = getattr(console.file, "encoding", None) or "utf-8"
        for key, value in info.items():
            style = "green" if key == "status" and value == "running" else ""
            style = "red" if key == "status" and value == "stopped" else style
            cell = _encode_safe_cell(value, cell_encoding)
            table.add_row(key, cell, style=style)
        console.print(table)
    else:
        # `agents status` only shows agents now. Claude-account info
        # moved to `sac accounts list` — different noun, different
        # concern. Keeping both here turned every status print into a
        # crowded mix of "what's running" + "who I'm logged in as".
        if use_json:
            from ._helpers import get_agent_list_data

            click.echo(
                json_mod.dumps(
                    {
                        "agents": get_agent_list_data(
                            registry,
                            capability=capability,
                            machine=machine,
                            tags=tags,
                        ),
                    },
                    indent=2,
                )
            )
        else:
            print_agent_list(
                registry,
                capability=capability,
                machine=machine,
                tags=tags,
                verbose=verbose,
                show_all=show_all,
            )


@click.command(name="check-health")
@click.argument("name", shell_complete=agent_name_complete)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def health(ctx: click.Context, name: str, as_json: bool) -> None:
    """Run a health check on an agent.

    \b
    Example:
      $ sac agent health head-ywata-note-win
      $ sac agent health head-ywata-note-win --json
    """
    use_json = _json_flag(ctx, as_json)
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if use_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    # stx-allow: fallback (reason: config YAML may be corrupted or missing after registry entry was created; CLI exits with code 1 in both JSON and human output modes)
    try:
        config = load_config(entry["config"])
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if use_json:
            click.echo(json_mod.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    is_healthy, message = health_check(config)

    if use_json:
        click.echo(
            json_mod.dumps(
                {"name": name, "healthy": is_healthy, "message": message},
                indent=2,
            )
        )
        if not is_healthy:
            sys.exit(1)
        return

    if is_healthy:
        console.print(f"[green]{message}[/green]")
    else:
        console.print(f"[red]{message}[/red]")
        sys.exit(1)
