"""Click-based CLI for scitex-agent-container."""

from __future__ import annotations

import importlib
import inspect
import json as json_mod
import sys
import traceback
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from .config import load_config, validate_config
from .lifecycle import agent_logs, agent_restart, agent_start, agent_status, agent_stop
from .registry import Registry

console = Console()


class _HelpRecursiveGroup(click.Group):
    """Click group that supports --help-recursive."""

    def format_help(self, ctx, formatter):
        super().format_help(ctx, formatter)

    def get_help_recursive(self, ctx) -> str:
        """Return help text for all commands recursively."""
        lines = []
        lines.append("=" * 60)
        lines.append("SciTeX Agent Container - Complete Command Reference")
        lines.append("=" * 60)
        lines.append("")

        # Main group help
        with ctx.scope() as _:
            lines.append(self.get_help(ctx))
            lines.append("")

        # Each subcommand
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


@click.group(cls=_HelpRecursiveGroup, invoke_without_command=True)
@click.version_option(package_name="scitex-agent-container")
@click.option("--help-recursive", is_flag=True, default=False,
              help="Show help for all commands recursively.")
@click.pass_context
def main(ctx, help_recursive):
    """SciTeX Agent Container -- Declarative agent management."""
    ctx.ensure_object(dict)
    if help_recursive:
        click.echo(ctx.command.get_help_recursive(ctx))
        ctx.exit(0)
    elif ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
@click.option("--no-preflight", is_flag=True, default=False,
              help="Skip preflight checks (useful for slow SSH hosts).")
def start(config_path: str, no_preflight: bool):
    """Start an agent from a YAML definition."""
    try:
        config = load_config(config_path)
        location = (
            f"REMOTE: {config.remote.host}"
            if config.remote.is_remote
            else "LOCAL"
        )
        console.print(
            f"[blue]Starting agent '{config.name}' "
            f"(runtime: {config.runtime}, {location})...[/blue]"
        )
        if no_preflight:
            console.print("[dim]Preflight checks skipped (--no-preflight)[/dim]")
        agent_start(config_path, no_preflight=no_preflight)
        console.print(f"[green]Agent '{config.name}' started successfully [{location}][/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        traceback.print_exc()
        sys.exit(1)


@main.command()
@click.argument("name")
def stop(name: str):
    """Stop a running agent."""
    try:
        agent_stop(name)
        console.print(f"[green]Agent '{name}' stopped[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@main.command()
@click.argument("name")
def restart(name: str):
    """Restart an agent."""
    try:
        agent_restart(name)
        console.print(f"[green]Agent '{name}' restarted[/green]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@main.command()
@click.argument("name", required=False)
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def status(name: str | None, as_json: bool):
    """Show agent status (one agent or all)."""
    registry = Registry()

    if name:
        try:
            info = agent_status(name)
        except Exception as exc:
            if as_json:
                click.echo(json_mod.dumps({"error": str(exc)}))
            else:
                console.print(f"[red]Error: {exc}[/red]")
            sys.exit(1)

        if as_json:
            click.echo(json_mod.dumps(info, indent=2))
            return

        table = Table(title=f"Agent: {name}")
        table.add_column("Field", style="bold")
        table.add_column("Value")
        for key, value in info.items():
            style = "green" if key == "status" and value == "running" else ""
            style = "red" if key == "status" and value == "stopped" else style
            table.add_row(key, str(value), style=style)
        console.print(table)
    else:
        if as_json:
            _print_agent_list_json(registry)
        else:
            _print_agent_list(registry)


@main.command(name="list")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
@click.option("--capability", "-c", default=None,
              help="Filter by capability label (comma-separated in YAML).")
@click.option("--machine", "-m", default=None,
              help="Filter by machine label.")
def list_agents(as_json: bool, capability: str | None, machine: str | None):
    """List all registered agents."""
    registry = Registry()
    if as_json:
        _print_agent_list_json(registry, capability=capability, machine=machine)
    else:
        _print_agent_list(registry, capability=capability, machine=machine)


@main.command()
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def ps(as_json: bool):
    """List all registered agents (alias for list)."""
    registry = Registry()
    if as_json:
        _print_agent_list_json(registry)
    else:
        _print_agent_list(registry)


def _get_agent_list_data(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> list[dict]:
    """Get agent list as plain dicts for JSON or table output.

    Args:
        registry: The agent registry to query.
        capability: If set, only include agents whose ``capabilities`` label
            contains this value (comma-separated matching).
        machine: If set, only include agents whose ``machine`` label matches.
    """
    from .runtimes.screen import ScreenManager

    entries = registry.list_all()
    results = []
    for entry in entries:
        name = entry.get("name", "?")
        screen_name = entry.get("screen", "?")
        started = entry.get("started_at", "?")
        # Load config for labels, remote info, and filtering
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

        # Check if running — remote or local
        try:
            if cfg and cfg.remote.is_remote:
                from .runtimes.claude_code import ClaudeCodeRuntime
                is_running = ClaudeCodeRuntime().is_running(cfg)
            else:
                is_running = ScreenManager.exists(screen_name)
        except Exception:
            is_running = False  # Graceful degradation on SSH timeout etc.

        # Apply filters
        if machine and labels.get("machine") != machine:
            continue
        if capability:
            caps = [c.strip() for c in labels.get("capabilities", "").split(",") if c.strip()]
            if capability not in caps:
                continue

        row: dict = {
            "name": name,
            "status": "running" if is_running else "stopped",
            "screen": screen_name,
            "started_at": started,
        }
        if remote_host:
            row["remote"] = remote_host
        if labels:
            row["labels"] = labels
        results.append(row)
    return results


def _print_agent_list_json(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print agent list as JSON."""
    data = _get_agent_list_data(registry, capability=capability, machine=machine)
    click.echo(json_mod.dumps(data, indent=2))


def _print_agent_list(
    registry: Registry,
    capability: str | None = None,
    machine: str | None = None,
) -> None:
    """Print a rich table of all registered agents."""
    data = _get_agent_list_data(registry, capability=capability, machine=machine)
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
            "[green]running[/green]" if row["status"] == "running"
            else "[red]stopped[/red]"
        )
        remote = row.get("remote", "")
        location = f"[cyan]REMOTE: {remote}[/cyan]" if remote else "LOCAL"
        table.add_row(row["name"], status_str, location, row["screen"], row["started_at"])

    console.print(table)


@main.command()
@click.argument("capability")
@click.option("--dir", "-d", "search_dir", default=None,
              help="Directory of YAML agent configs to search.")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def find(capability: str, search_dir: str | None, as_json: bool):
    """Find agents with a specific capability label from YAML configs.

    Searches agent definition files for those whose ``capabilities`` label
    includes the given value.  Useful for routing tasks to the right agent.
    """
    import glob as glob_mod

    if search_dir is None:
        search_dir = "."
    search_path = Path(search_dir).expanduser().resolve()

    matches = []
    for yaml_path in sorted(search_path.glob("*.yaml")):
        try:
            cfg = load_config(yaml_path)
        except Exception:
            continue
        caps = [c.strip() for c in cfg.labels.get("capabilities", "").split(",") if c.strip()]
        if capability in caps:
            matches.append({
                "name": cfg.name,
                "machine": cfg.labels.get("machine", ""),
                "capabilities": caps,
                "config": str(yaml_path),
            })

    if as_json:
        click.echo(json_mod.dumps(matches, indent=2))
        return

    if not matches:
        console.print(f"[dim]No agents found with capability '{capability}'[/dim]")
        return

    table = Table(title=f"Agents with capability: {capability}")
    table.add_column("Name", style="bold")
    table.add_column("Machine")
    table.add_column("Capabilities")
    table.add_column("Config")
    for m in matches:
        table.add_row(m["name"], m["machine"], ",".join(m["capabilities"]), m["config"])
    console.print(table)


@main.command()
@click.argument("name")
@click.option("--lines", "-n", default=50, help="Number of log lines to show.")
def logs(name: str, lines: int):
    """Show recent agent output."""
    try:
        output = agent_logs(name, lines)
        if output:
            console.print(output)
        else:
            console.print("[dim]No log output captured.[/dim]")
    except Exception as exc:
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@main.command()
@click.argument("name")
def attach(name: str):
    """Attach to an agent's screen session."""
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    screen_name = entry.get("screen", "")
    from .runtimes.screen import ScreenManager

    if not ScreenManager.exists(screen_name):
        console.print(f"[red]Screen session '{screen_name}' not found[/red]")
        sys.exit(1)

    console.print(f"[blue]Attaching to '{screen_name}' (Ctrl-A D to detach)[/blue]")
    ScreenManager.attach(screen_name)


@main.command()
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def health(name: str, as_json: bool):
    """Run a health check on an agent."""
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        if as_json:
            click.echo(json_mod.dumps({"error": f"Agent '{name}' not found"}))
        else:
            console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    try:
        config = load_config(entry["config"])
    except Exception as exc:
        if as_json:
            click.echo(json_mod.dumps({"error": str(exc)}))
        else:
            console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    from .health import health_check

    is_healthy, message = health_check(config)

    if as_json:
        click.echo(json_mod.dumps({
            "name": name,
            "healthy": is_healthy,
            "message": message,
        }, indent=2))
        if not is_healthy:
            sys.exit(1)
        return

    if is_healthy:
        console.print(f"[green]{message}[/green]")
    else:
        console.print(f"[red]{message}[/red]")
        sys.exit(1)


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
def check(config_path: str):
    """Run preflight checks for an agent deployment.

    Verifies that all dependencies (SSH, screen, python, etc.) are
    available before starting the agent.  Useful for debugging deployment
    failures.
    """
    import shutil

    try:
        config = load_config(config_path)
    except Exception as exc:
        console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"[blue]Checking {config.name}"
        + (f" (remote: {config.remote.host})" if config.remote.is_remote else " (local)")
        + "...[/blue]"
    )

    all_ok = True

    if config.remote.is_remote:
        from .runtimes.claude_code import _SSHRemote

        results = _SSHRemote.preflight(config)
        for name, passed, detail in results:
            if passed:
                # Right-align the check name for readability
                console.print(f"  {name + ':':30s} [green]{detail}[/green]")
            else:
                all_ok = False
                console.print(f"  {name + ':':30s} [red]FAIL[/red]")
                for line in detail.split("\n"):
                    console.print(f"    [red]{line}[/red]")
    else:
        # Local checks
        # 1. screen
        screen_bin = shutil.which("screen")
        if screen_bin:
            console.print(f"  {'screen:':30s} [green]OK ({screen_bin})[/green]")
        else:
            all_ok = False
            console.print(f"  {'screen:':30s} [red]FAIL[/red]")
            console.print("    [red]GNU screen not found[/red]")
            console.print("    [red]  Fix: sudo apt install screen[/red]")

        # 2. python
        import subprocess as _sp
        try:
            proc = _sp.run(
                ["python3", "--version"], capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                console.print(f"  {'python:':30s} [green]OK ({proc.stdout.strip()})[/green]")
            else:
                all_ok = False
                console.print(f"  {'python:':30s} [red]FAIL[/red]")
        except FileNotFoundError:
            all_ok = False
            console.print(f"  {'python:':30s} [red]FAIL (python3 not found)[/red]")

        # 3. scitex-agent-container
        sac_bin = shutil.which("scitex-agent-container")
        if sac_bin:
            try:
                proc = _sp.run(
                    ["scitex-agent-container", "--version"],
                    capture_output=True, text=True, timeout=5,
                )
                ver = proc.stdout.strip() if proc.returncode == 0 else "unknown"
            except Exception:
                ver = "unknown"
            console.print(f"  {'scitex-agent-container:':30s} [green]OK ({ver})[/green]")
        else:
            all_ok = False
            console.print(f"  {'scitex-agent-container:':30s} [red]FAIL[/red]")
            console.print("    [red]  Fix: pip install scitex-agent-container[/red]")

        # 4. disk space
        try:
            proc = _sp.run(
                ["df", "-h", "/"], capture_output=True, text=True, timeout=5,
            )
            if proc.returncode == 0:
                lines = proc.stdout.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    usage = parts[4] if len(parts) >= 5 else "unknown"
                    console.print(f"  {'disk space:':30s} [green]OK ({usage} used)[/green]")
        except Exception:
            console.print(f"  {'disk space:':30s} [dim]unknown[/dim]")

    if all_ok:
        console.print("[green]Ready to deploy.[/green]")
    else:
        console.print("[red]Preflight checks failed. Fix the issues above before deploying.[/red]")
        sys.exit(1)


@main.command()
@click.argument("config_path", type=click.Path(exists=True))
def validate(config_path: str):
    """Validate a YAML config file."""
    errors = validate_config(config_path)
    if not errors:
        console.print(f"[green]Config is valid: {config_path}[/green]")
    else:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)


@main.command()
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer"]),
    default="docker",
    help="Container runtime to build for.",
)
@click.option("--image", default="scitex-agent-container:latest", help="Image name/tag.")
def build(runtime: str, image: str):
    """Build container base image."""
    # Resolve containers/ directory relative to package
    containers_dir = Path(__file__).resolve().parent.parent.parent / "containers"

    if runtime == "docker":
        from .runtimes.docker import DockerRuntime

        console.print(f"[blue]Building Docker image: {image}[/blue]")
        success = DockerRuntime.build_image(image=image, context=str(containers_dir))
        if success:
            console.print(f"[green]Docker image built: {image}[/green]")
        else:
            console.print("[red]Docker build failed[/red]")
            sys.exit(1)
    elif runtime == "apptainer":
        from .runtimes.apptainer import ApptainerRuntime

        def_file = str(containers_dir / "apptainer.def")
        sif_path = str(containers_dir / "claude-code-container.sif")
        console.print(f"[blue]Building Apptainer image: {sif_path}[/blue]")
        success = ApptainerRuntime.build_image(def_file=def_file, sif_path=sif_path)
        if success:
            console.print(f"[green]Apptainer image built: {sif_path}[/green]")
        else:
            console.print("[red]Apptainer build failed[/red]")
            sys.exit(1)


@main.command()
def cleanup():
    """Remove stale registry entries."""
    registry = Registry()
    cleaned = registry.cleanup_stale()
    if cleaned:
        console.print(f"[green]Cleaned {cleaned} stale registry entries[/green]")
    else:
        console.print("[dim]No stale entries found.[/dim]")


@main.command(name="list-python-apis")
@click.option("-v", "--verbose", count=True,
              help="Verbosity: -v docstrings, -vv full docs.")
@click.option("-d", "--max-depth", type=int, default=5,
              help="Max recursion depth (default: 5).")
@click.option("--json", "as_json", is_flag=True, default=False,
              help="Output as JSON.")
def list_python_apis(verbose: int, max_depth: int, as_json: bool):
    """List all public Python APIs of scitex-agent-container."""
    module = importlib.import_module("scitex_agent_container")
    tree = _get_api_tree(module, max_depth=max_depth, docstring=(verbose >= 1))

    if as_json:
        click.echo(json_mod.dumps(tree, indent=2))
        return

    click.echo(f"API tree of scitex_agent_container ({len(tree)} items):")
    click.echo("Legend: [M]=Module [C]=Class [F]=Function [V]=Variable")

    for row in tree:
        indent = "  " * row["Depth"]
        t = row["Type"]
        name = row["Name"].split(".")[-1]

        if t == "F":
            parts = row["Name"].split(".")
            obj = module
            for part in parts[1:]:
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if obj and callable(obj):
                try:
                    sig = str(inspect.signature(obj))
                except (ValueError, TypeError):
                    sig = "()"
                click.echo(f"{indent}[{t}] {name}{sig}")
            else:
                click.echo(f"{indent}[{t}] {name}")
        else:
            click.echo(f"{indent}[{t}] {name}")

        if verbose >= 1 and row.get("Docstring"):
            if verbose == 1:
                doc = row["Docstring"].split("\n")[0][:60]
                click.echo(f"{indent}    - {doc}")
            else:
                for ln in row["Docstring"].split("\n"):
                    click.echo(f"{indent}    {ln}")


def _get_api_tree(module, max_depth: int = 5, docstring: bool = False) -> list[dict]:
    """Get API tree for a module."""
    results: list[dict] = []

    def _visit(obj, name: str, depth: int, visited: set):
        if depth > max_depth:
            return
        obj_id = id(obj)
        if obj_id in visited:
            return
        visited.add(obj_id)

        if inspect.ismodule(obj):
            obj_type = "M"
        elif inspect.isclass(obj):
            obj_type = "C"
        elif callable(obj):
            obj_type = "F"
        else:
            obj_type = "V"

        entry = {"Name": name, "Type": obj_type, "Depth": depth}
        if docstring:
            entry["Docstring"] = inspect.getdoc(obj) or ""
        results.append(entry)

        if inspect.ismodule(obj) and depth < max_depth:
            if hasattr(obj, "__all__"):
                members = [(n, getattr(obj, n, None)) for n in obj.__all__]
            else:
                members = [
                    (n, v) for n, v in inspect.getmembers(obj) if not n.startswith("_")
                ]
            for member_name, member_obj in members:
                if member_obj is not None:
                    _visit(member_obj, f"{name}.{member_name}", depth + 1, visited)

    _visit(module, module.__name__.split(".")[-1], 0, set())
    return results


if __name__ == "__main__":
    main()
