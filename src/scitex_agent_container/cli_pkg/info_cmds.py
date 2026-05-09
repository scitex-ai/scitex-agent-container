"""Info commands: find, logs, attach, list-python-apis."""

from __future__ import annotations

import importlib
import inspect
import json as json_mod
import sys
from pathlib import Path

import click
from rich.table import Table

from .._lifecycle.lifecycle import agent_logs
from ..config import load_config
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
      $ sac agent find HPC
      $ sac agent find GPU --json
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


@click.command(name="show-logs")
@click.argument("name")
@click.option(
    "--lines",
    "-n",
    default=50,
    help="Number of log lines to show.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit captured log lines as a JSON array.",
)
def logs(name: str, lines: int, as_json: bool) -> None:
    """Show recent agent output.

    \b
    Example:
      $ sac agent logs head-ywata-note-win
      $ sac agent logs head-ywata-note-win -n 200
      $ sac agent logs head-ywata-note-win --json
    """
    # stx-allow: fallback (reason: agent_logs reads from multiplexer or log files that may be absent if the agent was never started; error is reported and CLI exits with code 1)
    try:
        output = agent_logs(name, lines)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        if as_json:
            click.echo(json_mod.dumps({"error": str(exc), "lines": []}))
        else:
            console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)
    if as_json:
        captured = (output or "").splitlines()
        click.echo(json_mod.dumps({"name": name, "lines": captured}))
        return
    if output:
        # Disable Rich markup parsing — log content frequently contains
        # bracketed paths (e.g. "[/home/.../hook.sh]") that the markup
        # parser interprets as tags, raising MarkupError. Logs are raw
        # text; print as-is.
        console.print(output, markup=False, highlight=False)
    else:
        console.print("[dim]No log output captured.[/dim]")


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
