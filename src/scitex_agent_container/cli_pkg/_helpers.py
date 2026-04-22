"""Shared CLI helpers: rich console, recursive help group, agent-list formatting."""

from __future__ import annotations

import json as json_mod

import click
from rich.console import Console
from rich.table import Table

from ..config import load_config
from ..registry import Registry

console = Console()


def _json_flag(ctx: click.Context, local: bool) -> bool:
    """Return True if JSON output requested via local flag or top-level --json."""
    return local or bool((ctx.obj or {}).get("json", False))


class HelpRecursiveGroup(click.Group):
    """Click group that supports --help-recursive to dump every subcommand."""

    def format_help(self, ctx, formatter):
        super().format_help(ctx, formatter)

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
    try:
        from ..runtimes.claude_code import ClaudeCodeRuntime
        return ClaudeCodeRuntime().is_running(cfg)
    except Exception:
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
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout

    from ..runtimes.screen import ScreenManager
    from ..runtimes.tmux import TmuxManager

    def _detect_multiplexer(session_name: str) -> str | None:
        """Detect which multiplexer hosts a session. Tmux preferred."""
        if not session_name or session_name == "?":
            return None
        try:
            if TmuxManager.exists(session_name):
                return "tmux"
        except Exception:
            pass
        try:
            if ScreenManager.exists(session_name):
                return "screen"
        except Exception:
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
            try:
                cfg = load_config(config_path)
                labels = cfg.labels
                if cfg.remote.is_remote:
                    remote_host = cfg.remote.host
            except Exception:
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
                try:
                    probe_results[idx] = future.result(
                        timeout=remote_probe_timeout_s
                    )
                except _FuturesTimeout:
                    probe_results[idx] = None
                    future.cancel()
                except Exception:
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
        except Exception:
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
