"""Shared CLI helpers: rich console, recursive help group, agent-list formatting."""

from __future__ import annotations

import json as json_mod

import click
from rich.console import Console
from rich.table import Table

from .._state.registry import Registry
from ..config import load_config

console = Console()


def renamed_redirect(
    cmd: click.Command,
    *,
    new_path: str,
    old_path: str | None = None,
) -> click.Command:
    """Wrap ``cmd`` so invoking the old name hard-errors with a redirect.

    Per scitex CLI convention §5: renamed commands MUST exit non-zero
    with a redirect message, never silently warn-then-run. Soft warnings
    let stale scripts persist indefinitely; hard errors force the fix
    in one iteration.

    The wrapped command keeps its own ``params`` so ``--help`` still
    documents the surface the user invoked, but the callback is replaced:
    invoking the renamed command prints a single-line redirect to stderr
    and exits with code 2 (the convention's standard).

    Args:
        cmd: The Click command being redirected.
        new_path: The user-facing replacement (e.g. ``"sac agent start"``).
        old_path: The path the user actually typed, when it doesn't
            match ``"sac <cmd.name>"`` — typically a subcommand of a
            noun group (``"sac registry clean"`` rather than just
            ``"sac clean"``). Defaults to ``f"sac {cmd.name}"``.
    """
    rendered_old = old_path or f"sac {cmd.name}"

    def _callback(*args, **kwargs):
        del args, kwargs
        click.echo(
            f"error: '{rendered_old}' was renamed to '{new_path}'.\n"
            f"Re-run with: {new_path}",
            err=True,
        )
        raise SystemExit(2)

    return click.Command(
        name=cmd.name,
        callback=_callback,
        params=list(cmd.params),
        help=(
            (cmd.help or "")
            + f"\n\n[RENAMED] Use ``{new_path}`` instead. The old form "
            "exits with code 2."
        ),
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


def _json_flag(ctx: click.Context, local: bool) -> bool:
    """Return True if JSON output requested via local flag or top-level --json."""
    return local or bool((ctx.obj or {}).get("json", False))


# Inline CategorizedGroup — historically lived in scitex_dev.click_helpers,
# but pinning sac's runtime to a specific scitex-dev version made cross-repo
# releases brittle. Owned locally now.
class CategorizedGroup(click.Group):
    """Click `Group` that renders `--help` commands under named sections.

    Subclass and set ``COMMAND_CATEGORIES`` as a class attribute. Categories
    are ``(section_name, [command_names])``; anything not listed falls into
    a final ``Other`` section so nothing silently disappears.
    """

    COMMAND_CATEGORIES: list = []

    def format_commands(self, ctx, formatter):
        commands = {}
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            if cmd is not None and not cmd.hidden:
                commands[subcommand] = cmd
        if not commands:
            return
        displayed: set = set()
        for section, names in self.COMMAND_CATEGORIES:
            items = []
            for name in names:
                if name in commands and name not in displayed:
                    cmd = commands[name]
                    items.append((name, cmd.get_short_help_str(limit=formatter.width)))
                    displayed.add(name)
            if items:
                with formatter.section(section):
                    formatter.write_dl(items)
        leftover = [
            (n, commands[n].get_short_help_str(limit=formatter.width))
            for n in sorted(commands)
            if n not in displayed
        ]
        if leftover:
            with formatter.section("Other"):
                formatter.write_dl(leftover)


class HelpRecursiveGroup(CategorizedGroup):
    """Click group that supports --help-recursive AND categorized commands.

    Inherits categorization from `scitex_dev.click_helpers.CategorizedGroup`
    (per general/03_interface_02_cli §6). Subclasses set
    `COMMAND_CATEGORIES` (or the historical alias `command_categories` —
    see :meth:`__init_subclass__`) to opt into grouping; otherwise the
    output falls through to Click's default flat list.

    Adds the `--help-recursive` machinery on top.
    """

    # Backwards-compat alias: older sac code sets `command_categories` on
    # subclasses. Map it onto the canonical `COMMAND_CATEGORIES` slot at
    # subclass creation time so both names work.
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        if "command_categories" in cls.__dict__ and not cls.__dict__.get(
            "COMMAND_CATEGORIES"
        ):
            cls.COMMAND_CATEGORIES = tuple(cls.__dict__["command_categories"])

    def get_help_recursive(self, ctx) -> str:
        """Return help text for all commands recursively."""
        lines = []
        lines.append("=" * 60)
        lines.append("SciTeX Agent Container - Complete Command Reference")
        lines.append("=" * 60)
        lines.append("")

        with ctx.scope() as _:
            lines.append(self.get_help(ctx))
            lines.append("")

        for name in sorted(self.list_commands(ctx)):
            cmd = self.get_command(ctx, name)
            if cmd is None:
                continue
            lines.append("-" * 60)
            lines.append(f"Command: {name}")
            lines.append("-" * 60)
            sub_ctx = click.Context(cmd, info_name=name, parent=ctx)
            lines.append(cmd.get_help(sub_ctx))
            lines.append("")

        return "\n".join(lines)


def _probe_local(cfg) -> bool | None:
    """Probe an agent's liveness via ContainerRuntime.

    F-CS17 stage 3b: replaces the legacy ``_probe_remote`` /
    ``_detect_multiplexer`` machinery. Sac is a container wrapper —
    liveness is whatever ``docker inspect`` reports for the
    container_id sidecar in the agent's state dir. Cross-host
    liveness moved to F-CS12's ``sac --on <peer> agent status``
    pattern; the local helper here NEVER does its own ssh.

    Returns None on exception (e.g. malformed config) so the caller
    surfaces ``status='unknown'`` rather than crashing the list.
    """
    # stx-allow: fallback (reason: container engine may be missing or
    # state-dir may not exist for an agent that never ran; either case
    # maps to liveness_unknown rather than a hard error.)
    try:
        from ..runtimes.claude_session import ClaudeSessionRuntime

        return ClaudeSessionRuntime().is_running(cfg)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None


def get_agent_list_data(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
    remote_probe_timeout_s: float = 2.0,
    max_parallel_probes: int = 8,
) -> list[dict]:
    """Get agent list as plain dicts for JSON or table output.

    Args:
        registry: The agent registry to query.
        capability: If set, only include agents whose ``capabilities`` label
            contains this value (comma-separated matching).
        machine: If set, only include agents whose ``machine`` label matches.
        remote_probe_timeout_s: Per-agent SSH probe timeout for the
            ``is_running`` check. Short by default (2s) so the list
            command doesn't block indefinitely when the remote host is
            unreachable or the local ulimit wall throttles SSH fan-out
            (todo#254 regression). Exceeding this returns
            ``is_running=None`` (liveness unknown) instead of blocking.
        max_parallel_probes: How many remote probes to run concurrently.
            Kept small to stay under the macOS ``kern.maxproc`` wall
            that today's SSH fan-out regression exposed.

    Rows with a remote probe that timed out have ``status="unknown"``
    and ``liveness_unknown=True`` so JSON consumers can surface a
    soft-warning rather than treating unreachable remotes as offline.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as _FuturesTimeout

    entries = registry.list_all()

    # First pass: resolve configs + filter.
    # F-CS17 stage 3b: there are no longer "remote" agents from sac's
    # POV. Every agent is a container on this host. Cross-host work
    # routes through F-CS12's ``sac --on <peer>`` which spawns a fresh
    # sac on the remote host; the remote sac then reports its own
    # local list. So this function probes every agent locally.
    prepared: list[dict] = []
    for idx, entry in enumerate(entries):
        name = entry.get("name", "?")
        screen_name = entry.get("screen", "?")
        started = entry.get("started_at", "?")
        labels: dict[str, str] = {}
        config_path = entry.get("config")
        cfg = None
        if config_path:
            # stx-allow: fallback (reason: config YAML may be corrupt or
            # missing — agent still appears in list with empty labels)
            try:
                cfg = load_config(config_path)
                labels = cfg.labels
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                pass

        if machine and labels.get("machine") != machine:
            continue
        if capability:
            caps = [
                c.strip()
                for c in labels.get("capabilities", "").split(",")
                if c.strip()
            ]
            if capability not in caps:
                continue

        prep = {
            "idx": idx,
            "name": name,
            "screen_name": screen_name,
            "started": started,
            "labels": labels,
            "cfg": cfg,
        }
        prepared.append(prep)

    # Second pass: parallel local liveness probes with per-probe
    # timeout. The thread pool keeps the wall-clock cost low when many
    # agents are registered (each probe is ``docker inspect`` and
    # takes ~50ms-ish on a healthy host).
    #
    # Explicit shutdown(wait=False) instead of ``with ... as pool:``
    # so the context manager's __exit__ doesn't join all workers
    # (todo#254 regression: that would defeat the per-probe timeout).
    probe_results: dict[int, bool | None] = {}
    probe_targets = [
        (prep["idx"], prep["cfg"]) for prep in prepared if prep["cfg"] is not None
    ]
    if probe_targets:
        pool = ThreadPoolExecutor(max_workers=max_parallel_probes)
        try:
            future_to_idx = {
                pool.submit(_probe_local, cfg): idx for idx, cfg in probe_targets
            }
            for future in list(future_to_idx):
                idx = future_to_idx[future]
                # stx-allow: fallback (reason: per-probe runtime exception
                # maps to None = "liveness unknown", not "stopped")
                try:
                    probe_results[idx] = future.result(timeout=remote_probe_timeout_s)
                except _FuturesTimeout:  # stx-allow: fallback (reason: expected failure — see inline comment)
                    probe_results[idx] = None
                    future.cancel()
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    probe_results[idx] = None
        finally:
            pool.shutdown(wait=False)

    # Third pass: build result rows.
    results: list[dict] = []
    for prep in prepared:
        name = prep["name"]
        screen_name = prep["screen_name"]
        started = prep["started"]
        labels = prep["labels"]
        cfg = prep["cfg"]

        # ``multiplexer`` is the F-CS17 successor of the screen / tmux
        # column: it now reports the container engine the agent runs
        # on (docker / podman / apptainer), or None when the yaml is
        # missing / unparseable. Backwards compat: existing JSON
        # consumers still see a "multiplexer" key in each row.
        multiplexer: str | None = (
            getattr(cfg, "runtime", None) if cfg is not None else None
        )

        liveness_unknown = False
        probe = probe_results.get(prep["idx"])
        if cfg is None:
            # Couldn't load the yaml — can't probe.
            is_running = False
            liveness_unknown = True
        elif probe is None:
            is_running = False
            liveness_unknown = True
        else:
            is_running = bool(probe)

        status_val: str
        if liveness_unknown:
            status_val = "unknown"
        else:
            status_val = "running" if is_running else "stopped"

        row: dict = {
            "name": name,
            "status": status_val,
            "screen": screen_name,
            "multiplexer": multiplexer,
            "started_at": started,
        }
        if liveness_unknown:
            row["liveness_unknown"] = True
        if labels:
            row["labels"] = labels
        results.append(row)
    return results


def print_agent_list_json(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print agent list as JSON."""
    data = get_agent_list_data(registry, capability=capability, machine=machine)
    click.echo(json_mod.dumps(data, indent=2))


def print_agent_list(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print a rich table of all registered agents."""
    data = get_agent_list_data(registry, capability=capability, machine=machine)
    if not data:
        console.print("[dim]No agents registered.[/dim]")
        return

    table = Table(title="Registered Agents")
    table.add_column("Name", style="bold")
    table.add_column("Status")
    table.add_column("Location")
    table.add_column("Screen")
    table.add_column("Started")

    for row in data:
        status_str = (
            "[green]running[/green]"
            if row["status"] == "running"
            else "[red]stopped[/red]"
        )
        remote = row.get("remote", "")
        location = f"[cyan]REMOTE: {remote}[/cyan]" if remote else "LOCAL"
        table.add_row(
            row["name"], status_str, location, row["screen"], row["started_at"]
        )

    console.print(table)
