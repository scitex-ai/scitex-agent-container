"""Info commands: find, tail, list-python-apis."""

from __future__ import annotations

import importlib
import inspect
import json as json_mod
import sys
from pathlib import Path

import click
from rich.table import Table

from ..config import load_config
from ._api_tree import get_api_tree
from ._helpers import _json_flag, agent_name_complete, console


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
    # Dir-as-SSoT: agents live at <name>/spec.yaml. Walk one level deep
    # and match the convention.
    candidates: list[Path] = []
    for sub in sorted(search_path.iterdir()) if search_path.is_dir() else []:
        if sub.is_dir():
            spec = sub / "spec.yaml"
            if spec.exists():
                candidates.append(spec)
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


@click.command(name="tail")
@click.argument("name", shell_complete=agent_name_complete)
@click.option(
    "--lines", "-n", default=20, help="Number of recent assistant turns to show."
)
@click.option("--tools", "show_tools", is_flag=True, help="Also show tool_use entries.")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit raw session.jsonl records as JSON array.",
)
def tail_session(name: str, lines: int, show_tools: bool, as_json: bool) -> None:
    """Pretty-print the SDK runner's session.jsonl transcript.

    Reads ``<state>/<agent>/<agent>/session.jsonl`` (the structured
    transcript the SDK runner writes inside the container, mounted to
    the host via /state) and renders each record as a single line so
    you can monitor a running agent without grepping the raw JSON
    yourself.

    \b
    Example:
      $ sac agent tail polish-scholar
      $ sac agent tail polish-scholar -n 50 --tools
      $ sac agent tail polish-scholar --json
    """
    import json as _json
    from pathlib import Path

    from .._state.registry import Registry

    entry = Registry().get(name)
    if entry is None:
        console.print(f"[red]Agent '{name}' not found in registry[/red]")
        sys.exit(1)

    # state-dir layout: ~/.scitex/agent-container/runtime/<name>/<name>/session.jsonl
    state_root = Path.home() / ".scitex" / "agent-container" / "runtime" / name / name
    transcript = state_root / "session.jsonl"
    if not transcript.is_file():
        console.print(
            f"[red]No transcript at {transcript}. Agent may not have started a "
            "session yet, or runs in a non-default state-root.[/red]"
        )
        sys.exit(1)

    raw_lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    records = []
    for line in raw_lines:
        try:
            records.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue

    if as_json:
        click.echo(_json.dumps(records[-lines:], default=str, indent=2))
        return

    out: list[str] = []
    for r in records[-lines * 6 :]:
        kind = r.get("type", "?")
        if kind == "assistant":
            txt = str(r.get("text") or r.get("raw") or "")
            if txt.strip():
                out.append(f"[assistant] {txt[:300]}")
        elif kind == "user_echo" and show_tools:
            raw = str(r.get("raw") or "")[:200]
            out.append(f"[tool_result] {raw}")
        elif kind == "result":
            out.append(f"[result] {str(r)[:300]}")
        elif kind == "error":
            out.append(f"[error] {str(r)[:300]}")
    for line in out[-lines:]:
        console.print(line, markup=False, highlight=False)


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
