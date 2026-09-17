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

from ...config._validation import validate_raw

# Built-in contributor template (v3). String-level ``{{ var }}`` interpolation
# only — no Jinja2 dependency. Kept tiny and inspectable; matches the
# canonical chunk-A variable surface (project, branch_kind, branch_short,
# a2a_port, startup_command).
# The field set is NOT defined here. It comes from the canonical scaffold in
# ``cli_pkg/_create_templates.py``, the same one ``sac agents create`` uses.
#
# WHY, measured 2026-09-17: this module used to carry its own embedded template,
# written before the v3 realignment, and the two drifted. The embedded one still
# wrote top-level ``image`` / ``model`` / ``skills``, a ``multiplexer`` key and
# ``health.method: multiplexer-alive``, declared no ``host``, and omitted 65
# required fields — so the validator rejected every spec this tool rendered (8
# errors, the missing set dominating), 28 agents in the fleet inventory carry
# that shape, and a twin inherits its parent's spec verbatim. The canonical
# scaffolds render clean (0 errors) because they are kept in step with the
# grammar by their own tests. One source, or a second one that goes stale.

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
    dest_dir = Path(output_dir).expanduser() if output_dir else _AGENTS_DIR / name
    dest_file = dest_dir / f"{name}.yaml"

    rendered = _contributor_spec(
        name=name,
        port=int(port),
        task=task,
        target_repo=target_repo,
        branch_kind=branch_kind,
        branch_short=resolved_branch_short,
        path=str(dest_file),
    )

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


_CONTRIBUTOR_LABELS = ("role", "trigger", "project", "branch_kind", "branch_short", "capabilities")


def _contributor_spec(
    *,
    name: str,
    port: int,
    task: str,
    target_repo: str,
    branch_kind: str,
    branch_short: str,
    path: str,
) -> str:
    """The canonical v3 scaffold, decorated with the contributor pattern.

    TEXT-LEVEL DECORATION, deliberately. The scaffold carries the operator-facing
    documentation for every field (the red-start ruling, the two axes, which
    values are portable), and a parse-and-redump would silently drop all of it —
    a caller would receive a valid spec and no explanation of a single line in
    it. So the three contributor-specific facts are written into the text, the
    result is PARSED AND VALIDATED, and the text is what is returned.

    ``host: ${HOSTNAME}`` is the portable-fixture form the validator names
    itself: a template is materialised on whichever host claims it, so pinning a
    hostname here would be a lie the moment it is copied.
    """
    import yaml as _yaml

    from ...cli_pkg import _create_templates

    text = _create_templates._TEMPLATES["minimal"].format(
        name=name,
        host="${HOSTNAME}",
        credentials_files="[]",
        overlay='""',
    )

    labels = (
        "metadata:\n"
        "  labels:\n"
        f"    role: contributor-{target_repo}\n"
        "    trigger: pr-driven\n"
        f"    project: {target_repo}\n"
        f"    branch_kind: {branch_kind}\n"
        f"    branch_short: {branch_short}\n"
        "    capabilities: fork,clone,branch,commit,push,open-pr\n"
    )
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    for line in lines:
        if line.rstrip("\n") == "kind: Agent":
            out.append(line)
            out.append(labels)
            continue
        if line.strip() == "port: auto":
            out.append(line.replace("auto", str(port), 1))  # the agent's own a2a port
            continue
        if line.strip() == "startup_commands: []":
            out.append("  startup_commands:\n")
            out.append("  - delay: 5\n")
            out.append(f"    command: {task}\n")
            continue
        out.append(line)
    rendered = "".join(out)

    errors = validate_raw(_yaml.safe_load(rendered), path)
    if errors:
        # FAIL CLOSED. Emitting an invalid spec is how 28 agents in this fleet
        # came to be unloadable; a generator that cannot refuse is not a gate.
        raise ValueError(
            f"refusing to render an invalid contributor spec for {name!r} "
            f"({len(errors)} error(s)): "
            + " | ".join(e.split("\n")[0] for e in errors[:5])
        )
    return rendered


def register_template_tools(mcp) -> None:
    mcp.tool()(template_render_contributor_spec)


__all__ = ["template_render_contributor_spec", "register_template_tools"]
