"""Info commands: find, logs, attach, list-python-apis."""

from __future__ import annotations

import importlib
import inspect
import json as json_mod
import sys
from pathlib import Path

import click
from rich.table import Table

from ..config import load_config
from ..lifecycle import agent_logs
from ..registry import Registry
from ._api_tree import get_api_tree
from ._helpers import _json_flag, console


@click.command()
@click.argument("capability")
@click.option(
    "--dir",
    "-d",
    "search_dir",
    default=None,
    help="Directory of YAML agent configs to search.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def find(
    ctx: click.Context, capability: str, search_dir: str | None, as_json: bool
) -> None:
    """Find agents with a specific capability label from YAML configs.

    Searches agent definition files for those whose ``capabilities`` label
    includes the given value. Useful for routing tasks to the right agent.

    \b
    Example:
      $ sac find HPC
      $ sac find GPU --json
    """
    if search_dir is None:
        search_dir = "."
    search_path = Path(search_dir).expanduser().resolve()

    matches: list[dict] = []
    # Dir-as-SSoT: agents live at <name>/<name>.yaml. Walk one level deep
    # and match the convention. Bare top-level *.yaml files are also
    # accepted for legacy / scratch use.
    candidates: list[Path] = []
    for sub in sorted(search_path.iterdir()) if search_path.is_dir() else []:
        if sub.is_dir():
            yaml_in = sub / f"{sub.name}.yaml"
            if yaml_in.exists():
                candidates.append(yaml_in)
        elif sub.suffix == ".yaml":
            candidates.append(sub)
    for yaml_path in candidates:
        # stx-allow: fallback (reason: individual YAML files in the search directory may be invalid or unrelated; skipping bad files lets the search return partial results rather than aborting)
        try:
            cfg = load_config(yaml_path)
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            continue
        caps = [
            c.strip()
            for c in cfg.labels.get("capabilities", "").split(",")
            if c.strip()
        ]
        if capability in caps:
            matches.append(
                {
                    "name": cfg.name,
                    "machine": cfg.labels.get("machine", ""),
                    "capabilities": caps,
                    "config": str(yaml_path),
                }
            )

    if _json_flag(ctx, as_json):
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
        table.add_row(
            m["name"],
            m["machine"],
            ",".join(m["capabilities"]),
            m["config"],
        )
    console.print(table)


@click.command()
@click.argument("name")
@click.option(
    "--lines",
    "-n",
    default=50,
    help="Number of log lines to show.",
)
def logs(name: str, lines: int) -> None:
    """Show recent agent output.

    \b
    Example:
      $ sac logs head-ywata-note-win
      $ sac logs head-ywata-note-win -n 200
    """
    # stx-allow: fallback (reason: agent_logs reads from multiplexer or log files that may be absent if the agent was never started; error is reported and CLI exits with code 1)
    try:
        output = agent_logs(name, lines)
        if output:
            console.print(output)
        else:
            console.print("[dim]No log output captured.[/dim]")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)


@click.command()
@click.argument("name")
def attach(name: str) -> None:
    """Attach to an agent's multiplexer session.

    \b
    Example:
      $ sac attach head-ywata-note-win
    """
    registry = Registry()
    entry = registry.get(name)
    if entry is None:
        console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    from ..config import load_config

    config = load_config(entry["config"])

    # slurm-tenant agents live inside a remote SLURM allocation's tmux server;
    # route through the runtime's own attach() (uses srun --pty + tmux -L).
    if config.runtime == "slurm-tenant":
        from ..runtimes.slurm_tenant import SlurmTenantRuntime

        console.print(
            f"[blue]Attaching to slurm-tenant agent '{name}' "
            f"(reservation={config.slurm.reservation}, Ctrl-B D to detach)[/blue]"
        )
        rc = SlurmTenantRuntime().attach(config)
        sys.exit(rc)

    from ..runtimes.multiplexer import get_multiplexer

    mux = get_multiplexer(config)
    session_name = config.screen_name

    if not mux.exists(session_name):
        console.print(f"[red]Session '{session_name}' not found[/red]")
        sys.exit(1)

    detach_hint = "Ctrl-B D" if config.multiplexer == "tmux" else "Ctrl-A D"
    console.print(
        f"[blue]Attaching to '{session_name}' ({detach_hint} to detach)[/blue]"
    )
    mux.attach(session_name)


@click.command(name="list-python-apis")
@click.option(
    "-v",
    "--verbose",
    count=True,
    help="Verbosity: -v docstrings, -vv full docs.",
)
@click.option(
    "-d",
    "--max-depth",
    type=int,
    default=5,
    help="Max recursion depth (default: 5).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output as JSON.",
)
@click.pass_context
def list_python_apis(
    ctx: click.Context, verbose: int, max_depth: int, as_json: bool
) -> None:
    """List all public Python APIs of scitex-agent-container.

    \b
    Example:
      $ sac list-python-apis
      $ sac list-python-apis -v
    """
    module = importlib.import_module("scitex_agent_container")
    tree = get_api_tree(module, max_depth=max_depth, docstring=(verbose >= 1))

    if _json_flag(ctx, as_json):
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
                except (
                    ValueError,
                    TypeError,
                ):  # stx-allow: fallback (reason: type coercion or format mismatch)
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
