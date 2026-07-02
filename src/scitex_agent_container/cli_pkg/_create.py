#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents create NAME`` — stamp a proven-shape developer/scientist agent.

Card sac-templated-agent-create (2026-06-25). Folds the two prototype
shell stampers (``scripts/agents/new_agent_spec.sh`` +
``scripts/gen_ecosystem_dev_specs.sh``) into one reproducible CLI so
agents can create agents without hand-written specs or one-off scripts —
the CLI is the SSoT.

Two templates ship as underscore-agent skeletons under
``_create_templates/`` (``_developer`` / ``_scientist``), modeled on the
proven ``figrecipe`` (developer) and ``paper-scitex-clew`` (scientist)
shapes. They share one TUI core (sac-base.sif, full-home bind, directory
overlay, opus, generic boot kick) and differ on only:

  axis              developer            scientist
  ----------------- -------------------- ----------------------------------
  groups (list)     [developer]          [scientist]
  purpose suffix    -maintainer          -research

Two blocks are AUTO-detected at create time, regardless of template:

  * editable install — emitted iff the workdir ships ``pyproject.toml``
    or ``setup.py`` (paper repos ship none -> no install).
  * per-agent Telegram bot — emitted iff a bot-token file is present
    (``--telegram-token F`` or the default
    ``<secrets>/telegram_<name>_bot.txt``).

Only MINIMAL identity overrides are exposed (name / workdir / group /
telegram-token). Deeper changes = edit the generated spec.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import click

from ._new import _default_base_dir, _is_valid_agent_name

_TEMPLATE_DIR = Path(__file__).parent / "_create_templates"
_DEFAULT_PROJ_ROOT = "/home/ywatanabe/proj"
_DEFAULT_SECRETS_DIR = "/home/ywatanabe/.bash.d/secrets/access_tokens"
_TEMPLATES = ("developer", "scientist")

# Whole-line ``# >>>name`` / ``# <<<name`` markers wrap each optional block
# in the skeletons. ``_apply_block`` either drops just the markers (keep) or
# drops the markers AND the lines between them (omit).
_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def _apply_block(text: str, name: str, keep: bool) -> str:
    """Resolve one ``# >>>name`` … ``# <<<name`` region.

    ``keep=True`` strips only the two marker lines and leaves the content;
    ``keep=False`` removes the markers and everything between them.
    """
    open_marker = f"# >>>{name}"
    close_marker = f"# <<<{name}"
    out: list[str] = []
    skipping = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == open_marker:
            skipping = not keep
            continue
        if stripped == close_marker:
            skipping = False
            continue
        if skipping:
            continue
        out.append(line)
    body = "\n".join(out)
    return body + "\n" if text.endswith("\n") else body


def _render_tokens(text: str, mapping: dict[str, str]) -> str:
    """Substitute ``{{ TOKEN }}`` occurrences (whitespace-tolerant)."""

    def _sub(match: "re.Match[str]") -> str:
        key = match.group(1)
        if key not in mapping:
            raise KeyError(f"template token {key!r} not provided")
        return str(mapping[key])

    return _TOKEN_RE.sub(_sub, text)


def _has_package(workdir: str) -> bool:
    """True iff ``workdir`` ships an installable Python package."""
    base = Path(workdir)
    return (base / "pyproject.toml").is_file() or (base / "setup.py").is_file()


def render_spec(
    name: str,
    template: str,
    workdir: str,
    group: str,
    token_path: str,
    telegram: bool,
) -> str:
    """Render the final ``spec.yaml`` text for one agent from a skeleton.

    Pure function (no filesystem writes) so tests can assert the rendered
    shape directly. ``telegram`` decides both the bind region and the
    telegrammer channel; the editable-install region is auto-detected from
    ``workdir``.
    """
    skeleton = (_TEMPLATE_DIR / f"_{template}" / "spec.yaml").read_text()
    body = _apply_block(skeleton, "install", _has_package(workdir))
    body = _apply_block(body, "telegram", telegram)
    body = _apply_block(body, "tg_channel", telegram)
    return _render_tokens(
        body,
        {
            "NAME": name,
            "WORKDIR": workdir,
            "GROUP": group,
            "TELEGRAM_TOKEN": token_path,
        },
    )


@click.command(name="create")
@click.argument("name", type=str)
@click.option(
    "--template",
    "template",
    type=click.Choice(_TEMPLATES, case_sensitive=False),
    default="developer",
    show_default=True,
    help="Proven-shape preset: 'developer' (package maintainer) or "
    "'scientist' (paper / research agent).",
)
@click.option(
    "--workdir",
    "workdir",
    type=str,
    default=None,
    help="Canonical project path (the --pwd). Default: "
    "/home/ywatanabe/proj/<name>.",
)
@click.option(
    "--telegram-token",
    "telegram_token",
    type=str,
    default=None,
    help="Path to the per-agent Telegram bot-token file. Wires the bot iff "
    "the file exists. Default probe: "
    "<secrets>/telegram_<name>_bot.txt.",
)
@click.option(
    "--group",
    "group",
    type=str,
    default=None,
    help="Override the group label. Default: the template name "
    "(developer/scientist).",
)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Parent dir under which <name>/spec.yaml is written. Default: "
    "~/.scitex/agent-container/agents/.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite an existing spec.yaml (off by default).",
)
@click.option(
    "--start",
    is_flag=True,
    default=False,
    help="Run `sac agents start <name>` after writing the spec.",
)
def create(
    name: str,
    template: str,
    workdir: str | None,
    telegram_token: str | None,
    group: str | None,
    base_dir: Path | None,
    force: bool,
    start: bool,
) -> None:
    """Stamp a proven-shape developer/scientist agent spec.

    Writes ``<base-dir>/<name>/spec.yaml`` from the matching underscore-agent
    skeleton, filling identity (name -> project / workdir / overlay /
    state-db / SCITEX_TODO_AGENT_ID) and auto-detecting the editable-install and
    Telegram-bot blocks.

    Examples:

    \b
        sac agents create scitex-io --template developer
        sac agents create paper-ripple-wm --template scientist \\
            --workdir /home/ywatanabe/proj/ripple-wm
    """
    template = template.lower()
    if not _is_valid_agent_name(name):
        raise click.UsageError(
            f"Invalid agent name {name!r}. Use lowercase letters, "
            "digits, '-', and '_' only (dir-as-SSoT convention)."
        )

    resolved_workdir = workdir or f"{_DEFAULT_PROJ_ROOT}/{name}"
    resolved_group = group or template
    secrets_dir = os.environ.get("SAC_TG_SECRETS_DIR", _DEFAULT_SECRETS_DIR)
    token_path = telegram_token or f"{secrets_dir}/telegram_{name}_bot.txt"
    telegram = Path(token_path).is_file()

    body = render_spec(
        name=name,
        template=template,
        workdir=resolved_workdir,
        group=resolved_group,
        token_path=token_path,
        telegram=telegram,
    )

    base = base_dir if base_dir is not None else _default_base_dir()
    agent_dir = base / name
    spec_path = agent_dir / "spec.yaml"
    if spec_path.exists() and not force:
        raise click.ClickException(
            f"Refusing to overwrite existing spec at {spec_path}. "
            "Re-run with --force to replace, or pick a different name."
        )

    agent_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body)

    # Emit an empty ``to_home/.mcp.json`` to match the proven spec shape
    # (figrecipe/scitex-todo). The .mcp.json deep-merge cascade expects a
    # per-agent file present even when empty; without it a generated agent
    # diverges from the proven shape and a hand-added file is dropped on the
    # next regen (the retired gen_ecosystem_dev_specs.sh bug, now folded into
    # this CLI). Create-if-absent so an existing custom .mcp.json is never
    # clobbered.
    mcp_path = agent_dir / "to_home" / ".mcp.json"
    if not mcp_path.exists():
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text('{\n  "mcpServers": {}\n}\n')

    print(
        f"Wrote {spec_path} (template={template}, group={resolved_group}, "
        f"install={'yes' if _has_package(resolved_workdir) else 'no'}, "
        f"telegram={'yes' if telegram else 'no'}).",
        file=sys.stderr,
        flush=True,
    )

    if start:
        import subprocess

        subprocess.run(["sac", "agents", "start", name], check=False)


__all__ = ["create", "render_spec"]
