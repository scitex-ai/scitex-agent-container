"""Shared CLI helpers: rich console, recursive help group, agent-list formatting."""

from __future__ import annotations

import json as json_mod

import click
from rich.console import Console
from rich.table import Table

from ..config import load_config
from ..registry import Registry

console = Console()


def deprecated_alias(cmd: click.Command, *, new_path: str) -> click.Command:
    """Wrap ``cmd`` so invoking it prints a deprecation warning to stderr.

    ``new_path`` is the user-facing replacement (e.g. ``"sac render sbatch"``);
    it appears verbatim in the warning. The wrapped command keeps the same
    name, params, and behaviour — only side effect is the stderr line.
    """
    original_callback = cmd.callback
    if original_callback is None:
        raise ValueError(f"deprecated_alias: command {cmd.name!r} has no callback")

    _orig = original_callback

    def _callback(*args, **kwargs):
        click.echo(
            f"warning: '{cmd.name}' is deprecated; use '{new_path}' instead. "
            "(alias will be removed in a future release.)",
            err=True,
        )
        return _orig(*args, **kwargs)

    return click.Command(
        name=cmd.name,
        callback=_callback,
        params=list(cmd.params),
        help=(cmd.help or "") + f"\n\n[DEPRECATED] Use ``{new_path}`` instead.",
        short_help=cmd.short_help,
        epilog=cmd.epilog,
    )


def _json_flag(ctx: click.Context, local: bool) -> bool:
    """Return True if JSON output requested via local flag or top-level --json."""
    return local or bool((ctx.obj or {}).get("json", False))


from scitex_dev.click_helpers import CategorizedGroup


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


def _probe_remote(cfg) -> bool | None:
    """Probe a remote agent's liveness. Returns None on exception/timeout.

    Extracted to module level so tests can monkeypatch it (todo#254
    regression suite needs to simulate hung + fast probes without
    real SSH).
    """
    # stx-allow: fallback (reason: SSH probe may fail if host is unreachable;
    # None signals "liveness unknown" which callers convert to status="unknown")
    try:
        from ..runtimes.claude_code import ClaudeCodeRuntime

        return ClaudeCodeRuntime().is_running(cfg)
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

    from ..runtimes.screen import ScreenManager
    from ..runtimes.tmux import TmuxManager

    def _detect_multiplexer(session_name: str) -> str | None:
        """Detect which multiplexer hosts a session. Tmux preferred."""
        if not session_name or session_name == "?":
            return None
        # stx-allow: fallback (reason: tmux binary may be absent on the host;
        # None fallthrough tries screen next rather than raising)
        try:
            if TmuxManager.exists(session_name):
                return "tmux"
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass
        # stx-allow: fallback (reason: screen binary may be absent; None
        # return means multiplexer is unknown, not an error)
        try:
            if ScreenManager.exists(session_name):
                return "screen"
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            pass
        return None

    entries = registry.list_all()

    # First pass: resolve configs + filter + identify remote probes to run.
    prepared: list[dict] = []
    remote_probes: dict[int, object] = {}
    for idx, entry in enumerate(entries):
        name = entry.get("name", "?")
        screen_name = entry.get("screen", "?")
        started = entry.get("started_at", "?")
        labels: dict[str, str] = {}
        remote_host = ""
        config_path = entry.get("config")
        cfg = None
        if config_path:
            # stx-allow: fallback (reason: config YAML may be corrupt or
            # missing — agent still appears in list with empty labels)
            try:
                cfg = load_config(config_path)
                labels = cfg.labels
                if cfg.remote.is_remote:
                    remote_host = cfg.remote.host
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
            "remote_host": remote_host,
            "cfg": cfg,
        }
        prepared.append(prep)
        if cfg and cfg.remote.is_remote:
            remote_probes[prep["idx"]] = cfg

    # Second pass: parallel remote probes with per-probe timeout.
    # NOTE: Use explicit shutdown(wait=False) instead of `with ... as pool:`
    # because the context manager's __exit__ joins all worker threads, which
    # negates the per-probe timeout when a worker hangs on a wedged SSH
    # socket (todo#254 regression: the `--json` call still blocks for the
    # full hung-probe duration even though `future.result(timeout=…)` raised).
    probe_results: dict[int, bool | None] = {}
    if remote_probes:
        pool = ThreadPoolExecutor(max_workers=max_parallel_probes)
        try:
            future_to_idx = {
                pool.submit(_probe_remote, cfg): idx
                for idx, cfg in remote_probes.items()
            }
            for future in list(future_to_idx):
                idx = future_to_idx[future]
                # stx-allow: fallback (reason: per-probe SSH or runtime
                # exception maps to None = "liveness unknown", not "stopped")
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
        remote_host = prep["remote_host"]
        cfg = prep["cfg"]

        multiplexer: str | None = None
        if not (cfg and cfg.remote.is_remote):
            multiplexer = _detect_multiplexer(screen_name)

        liveness_unknown = False
        # stx-allow: fallback (reason: ScreenManager.exists may raise if the
        # screen binary is absent — liveness_unknown=True surfaces as "unknown"
        # status rather than crashing the list command)
        try:
            if cfg and cfg.remote.is_remote:
                probe = probe_results.get(prep["idx"])
                if probe is None:
                    is_running = False
                    liveness_unknown = True
                else:
                    is_running = bool(probe)
            else:
                # _detect_multiplexer returned non-None iff either the tmux
                # OR screen backend sees this session live — use it as the
                # authoritative liveness signal. Previously this path
                # hardcoded ScreenManager.exists(), which always returned
                # False for tmux agents and reported them as stopped.
                is_running = multiplexer is not None
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            is_running = False
            liveness_unknown = True

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
        if remote_host:
            row["remote"] = remote_host
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
