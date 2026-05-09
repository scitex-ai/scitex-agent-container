"""contributor-spec: render a contributor agent spec from the Jinja2 template (chunk A)."""

from __future__ import annotations

import sys
from pathlib import Path

import click

_TEMPLATE_PATH = (
    Path.home() / ".scitex/orochi/shared/agents/.templates/contributor.yaml.j2"
)
_AGENTS_DIR = Path.home() / ".scitex/orochi/shared/agents"

# Built-in fallback matches the chunk-A canonical template variable names.
_FALLBACK_TEMPLATE = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  labels:
    role: contributor-{{ project }}
    team: orochi
    trigger: pr-driven
    project: {{ project }}
    branch_kind: {{ branch_kind }}
    branch_short: {{ branch_short }}
    capabilities: fork,clone,branch,commit,push,open-pr
spec:
  runtime: docker
  image: scitex-agent-container:sdk-persistent
  dockerfile: ./containers/Dockerfile
  model: sonnet
  multiplexer: tmux
  host:
{% for host in (hosts | default(['spartan'])) %}
  - {{ host }}
{% endfor %}
  a2a:
    port: {{ a2a_port }}
    handler: claude_cli
    host: 127.0.0.1
  orochi:
    enabled: true
    hosts:
    - scitex-orochi.com
  claude:
    flags:
    - --dangerously-skip-permissions
    - --dangerously-load-development-channels
    - server:scitex-orochi
    - --add-dir
    - /home/ywatanabe/proj/scitex-agent-container/src/scitex_agent_container/_skills/
    - --add-dir
    - /home/ywatanabe/.scitex/orochi/shared/skills/
    session: continue-or-new
  skills:
    required:
    - scitex
    - scitex-agent-container
    - scitex-orochi
  python-venv:
  - ~/.venv
  - ~/.venv-3.11
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
  context_management:
    strategy: compact
    trigger_at_percent: 70.0
    check_interval_seconds: 300
    warn_before_n_checks: 1
  startup_commands:
  - delay: {{ startup_delay | default(5) }}
    command: {{ startup_command }}
"""

_DEFAULT_BRANCH_KIND = "feat"


def _derive_branch_short(name: str) -> str:
    """Strip common c-sac- prefix to get a branch slug."""
    for prefix in ("c-sac-", "c-"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


@click.command("render-contributor-spec")
@click.option("--name", required=True, help="Agent name (e.g. c-sac-my-feature).")
@click.option("--port", required=True, type=int, help="A2A port number.")
@click.option(
    "--target-repo",
    "target_repo",
    default="scitex-agent-container",
    show_default=True,
    help="Target repo / project name (e.g. scitex-orochi).",
)
@click.option("--task", required=True, help="Startup mission text for the agent.")
@click.option(
    "--branch-kind",
    "branch_kind",
    default=_DEFAULT_BRANCH_KIND,
    show_default=True,
    help="Branch prefix: feat | fix | refactor | docs | test.",
)
@click.option(
    "--branch-short",
    "branch_short",
    default=None,
    help="Branch slug (default: derived from --name by stripping c-sac- prefix).",
)
@click.option(
    "--output-dir",
    "output_dir",
    default=None,
    help="Override output directory (default: ~/.scitex/orochi/shared/agents/<name>/).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print rendered YAML to stdout without writing files.",
)
def contributor_spec(
    name: str,
    port: int,
    target_repo: str,
    task: str,
    branch_kind: str,
    branch_short: str | None,
    output_dir: str | None,
    dry_run: bool,
) -> None:
    """Render a contributor agent spec YAML from the Jinja2 template.

    Reads ~/.scitex/orochi/shared/agents/.templates/contributor.yaml.j2
    (produced by chunk A: c-sac-spec-template-jinja) and writes
    ~/.scitex/orochi/shared/agents/<name>/<name>.yaml.

    \b
    Example:
      $ sac template render-contributor-spec --name c-sac-my-feature --port 19200 \\
            --target-repo scitex-agent-container \\
            --task "Implement X in scitex-agent-container"
    """
    try:
        from jinja2 import Environment, StrictUndefined
    except ImportError:
        click.echo("Error: jinja2 is required.  Run: pip install jinja2", err=True)
        sys.exit(1)

    if _TEMPLATE_PATH.exists():
        template_src = _TEMPLATE_PATH.read_text()
        template_origin = str(_TEMPLATE_PATH)
    else:
        click.echo(
            f"Warning: template not found at {_TEMPLATE_PATH}; "
            "using built-in fallback (run chunk A to generate the canonical template).",
            err=True,
        )
        template_src = _FALLBACK_TEMPLATE
        template_origin = "<built-in fallback>"

    resolved_branch_short = branch_short or _derive_branch_short(name)

    env = Environment(undefined=StrictUndefined, keep_trailing_newline=True)
    try:
        template = env.from_string(template_src)
        rendered = template.render(
            project=target_repo,
            branch_kind=branch_kind,
            branch_short=resolved_branch_short,
            a2a_port=port,
            startup_command=task,
        )
    except Exception as exc:
        click.echo(f"Error rendering template ({template_origin}): {exc}", err=True)
        sys.exit(1)

    if dry_run:
        click.echo(rendered)
        return

    dest_dir = Path(output_dir) if output_dir else _AGENTS_DIR / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / f"{name}.yaml"

    if dest_file.exists():
        click.echo(f"Warning: {dest_file} already exists — overwriting.", err=True)

    dest_file.write_text(rendered)
    click.echo(f"Written: {dest_file}")


__all__ = ["contributor_spec"]
