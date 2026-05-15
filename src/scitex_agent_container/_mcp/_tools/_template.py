"""``sac template ...`` tools (F-CS15) — Python API + MCP wrappers.

The ``template`` noun group renders agent spec YAML from built-in
templates. Currently exposes ``render_contributor_spec``: produce a
contributor-pattern v3 spec for a given agent name, A2A port, target
repo, and startup task.

The CLI surface (``sac template render-contributor-spec``) was retired
in the F-CS17 cleanup pass, but the MCP tool is retained per the
F-CS15 noun-group contract so agents can still materialize contributor
specs programmatically without depending on a shell verb.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# Built-in contributor template (v3). String-level ``{{ var }}`` interpolation
# only — no Jinja2 dependency. Kept tiny and inspectable; matches the
# canonical chunk-A variable surface (project, branch_kind, branch_short,
# a2a_port, startup_command).
_CONTRIBUTOR_TEMPLATE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: contributor-{{ project }}
    trigger: pr-driven
    project: {{ project }}
    branch_kind: {{ branch_kind }}
    branch_short: {{ branch_short }}
    capabilities: fork,clone,branch,commit,push,open-pr
spec:
  runtime: apptainer
  image: scitex-agent-container.sif
  model: sonnet
  multiplexer: tmux
  a2a:
    port: {{ a2a_port }}
    handler: claude_cli
    host: 127.0.0.1
  claude:
    flags:
    - --dangerously-skip-permissions
    session: continue-or-new
  skills:
    required:
    - scitex
    - scitex-agent-container
  health:
    enabled: true
    interval: 60
    timeout: 5
    method: multiplexer-alive
  restart:
    policy: on-failure
    max_retries: 3
    backoff:
      initial: 30
      max: 300
      multiplier: 2
  startup_commands:
  - delay: 5
    command: {{ startup_command }}
"""

_DEFAULT_BRANCH_KIND = "feat"
_AGENTS_DIR = Path.home() / ".scitex/agent-container/agents"


def _derive_branch_short(name: str) -> str:
    """Strip ``c-sac-`` / ``c-`` prefix from an agent name to yield a branch slug."""
    for prefix in ("c-sac-", "c-"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


def _render(template: str, mapping: dict[str, str]) -> str:
    """Tiny ``{{ var }}`` substituter (whitespace-tolerant)."""
    import re

    pattern = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in mapping:
            raise KeyError(f"template variable {key!r} not provided")
        return str(mapping[key])

    return pattern.sub(_sub, template)


def template_render_contributor_spec(
    name: str,
    port: int,
    task: str,
    target_repo: str = "scitex-agent-container",
    branch_kind: str = _DEFAULT_BRANCH_KIND,
    branch_short: str | None = None,
    output_dir: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Render a v3 contributor agent spec YAML.

    Produces a contributor-pattern spec for ``name`` listening on A2A
    ``port``, targeting ``target_repo``, with ``task`` as the startup
    mission line. When ``dry_run`` is true (the default), the rendered
    YAML is returned but no files are written; when false, writes to
    ``<output_dir>/<name>.yaml`` (defaulting to
    ``~/.scitex/agent-container/agents/<name>/<name>.yaml``).

    Returns ``{"name", "path", "yaml", "written"}``. ``written`` is
    ``False`` for dry runs.
    """
    resolved_branch_short = branch_short or _derive_branch_short(name)
    mapping = {
        "project": target_repo,
        "branch_kind": branch_kind,
        "branch_short": resolved_branch_short,
        "a2a_port": str(int(port)),
        "startup_command": task,
    }
    rendered = _render(_CONTRIBUTOR_TEMPLATE, mapping)

    dest_dir = Path(output_dir).expanduser() if output_dir else _AGENTS_DIR / name
    dest_file = dest_dir / f"{name}.yaml"

    if dry_run:
        return {
            "name": name,
            "path": str(dest_file),
            "yaml": rendered,
            "written": False,
        }

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(rendered)
    return {
        "name": name,
        "path": str(dest_file),
        "yaml": rendered,
        "written": True,
    }


def register_template_tools(mcp) -> None:
    mcp.tool()(template_render_contributor_spec)


__all__ = ["template_render_contributor_spec", "register_template_tools"]
