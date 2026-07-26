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
@click.argument("names", nargs=-1, required=True, shell_complete=agent_name_complete)
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
def tail_session(
    names: tuple[str, ...], lines: int, show_tools: bool, as_json: bool
) -> None:
    """Pretty-print the SDK runner's session.jsonl transcript.

    Reads ``<state>/<agent>/session.jsonl`` (the structured transcript
    the SDK runner writes inside the container, mounted to the host
    via /state) and renders each record as a single line so you can
    monitor a running agent without grepping the raw JSON yourself.

    Multiple agent names interleave their transcripts with a
    ``[<name>]`` line-prefix so you can spot which agent emitted what.

    \b
    Example:
      $ sac agent tail polish-scholar
      $ sac agent tail polish-scholar -n 50 --tools
      $ sac agent tail polish-scholar --json
      $ sac agent tail hello-agent hello-agent2 hello-agent3
    """
    any_err = False
    for n in names:
        if not _tail_one(n, lines, show_tools, as_json, prefix=len(names) > 1):
            any_err = True
    if any_err:
        sys.exit(1)


def _tail_one(
    name: str, lines: int, show_tools: bool, as_json: bool, prefix: bool
) -> bool:
    """Render one agent's transcript. Returns True on success, False if
    the agent is missing or has no transcript. Multi-name caller sets
    prefix=True so each line carries ``[<name>]`` for disambiguation.

    Cross-host: when the active ``state.db.instances`` row for ``name``
    records ``host != current_host``, the call ssh's to the peer and
    streams ``sac agents tail <name> ...`` line-by-line. Falls back to
    the local read path otherwise.
    """
    import json as _json
    from pathlib import Path

    from .._state.registry import Registry

    # Cross-host first — if the agent's row lives on a peer, ssh there
    # rather than trying to read the (non-existent) local session.jsonl.
    if _tail_one_remote(name, lines, show_tools, as_json, prefix):
        return True

    entry = Registry().get(name)
    if entry is None:
        console.print(f"[red]Agent '{name}' not found in registry[/red]")
        return False

    # state-dir layout: ~/.scitex/agent-container/runtime/<name>/session.jsonl
    state_root = Path.home() / ".scitex" / "agent-container" / "runtime" / name
    transcript = state_root / "session.jsonl"
    if not transcript.is_file():
        console.print(
            f"[red]No transcript at {transcript}. Agent may not have started a "
            "session yet, or runs in a non-default state-root.[/red]"
        )
        return False

    raw_lines = transcript.read_text(encoding="utf-8", errors="replace").splitlines()
    records = []
    for line in raw_lines:
        try:
            records.append(_json.loads(line))
        except _json.JSONDecodeError:
            continue

    if as_json:
        click.echo(_json.dumps(records[-lines:], default=str, indent=2))
        return True

    tag = f"[{name}] " if prefix else ""
    out: list[str] = []
    for r in records[-lines * 6 :]:
        kind = r.get("type", "?")
        if kind == "assistant":
            txt = str(r.get("text") or r.get("raw") or "")
            if txt.strip():
                out.append(f"{tag}[assistant] {txt[:300]}")
        elif kind == "user_echo" and show_tools:
            raw = str(r.get("raw") or "")[:200]
            out.append(f"{tag}[tool_result] {raw}")
        elif kind == "result":
            # Terser result line: just session_id + token deltas, no
            # dumped dict. Operators want `[result]` as a visual
            # boundary between turns, not a JSON listing — that's what
            # `--json` is for.
            usage = r.get("usage") or {}
            sid = str(r.get("session_id") or "?")[:8]
            inp = usage.get("input_tokens", 0)
            out_tok = usage.get("output_tokens", 0)
            cache_w = usage.get("cache_creation_input_tokens", 0)
            cache_r = usage.get("cache_read_input_tokens", 0)
            cost = r.get("cost_usd")
            cost_text = (
                f" cost_usd={float(cost):.6f}"
                if isinstance(cost, (int, float)) and not isinstance(cost, bool)
                else ""
            )
            out.append(
                f"{tag}[result] session={sid} "
                f"in={inp} out={out_tok} cache_w={cache_w} cache_r={cache_r}"
                f"{cost_text}"
            )
        elif kind == "error":
            out.append(f"{tag}[error] {str(r)[:300]}")
    for line in out[-lines:]:
        console.print(line, markup=False, highlight=False)
    return True


def _tail_one_remote(
    name: str,
    lines: int,
    show_tools: bool,
    as_json: bool,
    prefix: bool,
) -> bool:
    """SSH to the peer that owns ``name`` and stream the remote tail.

    Returns:
        * ``True`` when the dispatch happened (caller short-circuits).
        * ``False`` when no remote row exists (caller proceeds locally).

    Raises ``RuntimeError`` when the remote sac call itself fails — the
    no-silent-fallback rule applies: a missing row falls back to local,
    but a broken ssh connection surfaces.
    """
    import shlex
    import subprocess

    from .._state.host_config import build_ssh_argv
    from .._state.host_config import load as _load_host_config
    from .lifecycle._dispatch import lookup_remote_peer

    found = lookup_remote_peer(name)
    if found is None:
        return False
    peer, _row = found
    peers = _load_host_config().peers
    if peer not in peers:
        raise RuntimeError(
            f"Agent {name!r} active on peer {peer!r} per state.db, but "
            f"{peer!r} is NOT in ~/.scitex/agent-container/config.yaml's "
            f"peers: section. Cannot tail cross-host. Add the peer entry."
        )
    cmd: list[str] = ["sac", "agents", "tail", name, "--lines", str(lines)]
    if show_tools:
        cmd.append("--tools")
    if as_json:
        cmd.append("--json")
    ssh_argv = build_ssh_argv(peer, cmd, peers)
    # Popen+line-iter so the user sees output as the peer emits it,
    # rather than buffering the entire transcript before printing.
    proc = subprocess.Popen(
        ssh_argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    tag = f"[{name}] " if prefix else ""
    assert proc.stdout is not None
    for line in proc.stdout:
        click.echo(f"{tag}{line.rstrip()}")
    rc = proc.wait()
    if rc != 0:
        err = (proc.stderr.read() if proc.stderr else "") or ""
        raise RuntimeError(
            f"Remote `sac agents tail {name}` failed on {peer!r} "
            f"(rc={rc}):\n"
            f"argv: {' '.join(shlex.quote(a) for a in ssh_argv)}\n"
            f"stderr:\n{err}"
        )
    return True


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
