#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents new NAME`` — scaffold a fresh v3 agent spec.

Card sac-fresh-agent-specs (2026-06-13). "Author fresh specs from a
clean template, not in-place repair" — operators get a v3-clean
``spec.yaml`` + ``to_home/`` skeleton next to it, ready to edit. The
templates here are the canonical reference; they must always satisfy
:func:`scitex_agent_container.config._validation.validate_config`
without any operator edits (the tests assert this invariant).

Two templates ship out of the box:

  * ``minimal`` (default) — bare-minimum spec mirroring
    ``examples/agents/minimal-agent/``. Three blocks: ``apptainer.image``,
    ``claude.model``, ``claude.flags``. Use as the starting point for new
    agents you'll customise yourself.
  * ``full`` — full-fat spec mirroring
    ``examples/agents/full-agent/`` (sans the inline tutorial comments).
    Use when you want every knob present so you can delete what you
    don't need rather than look up what's available.

The CLI refuses to overwrite an existing ``spec.yaml`` unless ``--force``
is passed — protects the "60 stale ``*-quality`` specs already pending
uncommitted" scenario the card calls out.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

# Agent names follow the dir-as-SSoT convention: lowercase letters,
# digits, dashes, underscores. Slashes / dots would write outside the
# base dir or shadow YAML extension suffixes. Mirrors the constraints
# enforced by the lifecycle dispatcher.
_VALID_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")


def _is_valid_agent_name(name: str) -> bool:
    """Return True iff ``name`` is safe to use as a directory name.

    Pure predicate — used by the CLI for an early reject so we never
    write ``spec.yaml`` outside ``base_dir`` via a slash-bearing name.
    """
    if not name:
        return False
    return all(ch in _VALID_NAME_CHARS for ch in name)


_MINIMAL_TEMPLATE = """\
# {name} — fresh v3 spec scaffolded by ``sac agents new``.
#
# This is the minimal shape — every field the validator REQUIRES (no hidden
# defaults): placement, workdir, image + binds, model, health, restart. Add
# startup_prompts, channels, etc. as you need them — see
# ``examples/agents/full-agent/spec.yaml`` for the full annotated tour.

apiVersion: scitex-agent-container/v3
kind: Agent

spec:
  runtime: apptainer
  # Placement: `local` = the invoking host (edit to a peer name to pin it
  # elsewhere, or use `hosts:` for one instance per host).
  host: local
  workdir: ~/proj/{name}

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-base.sif
    binds: []

  claude:
    model: haiku
    flags:
      - --dangerously-skip-permissions

  health:
    enabled: true
    interval: 60

  restart:
    policy: on-failure
    max_retries: 3

# EOF
"""


_FULL_TEMPLATE = """\
# {name} — fresh v3 spec scaffolded by ``sac agents new --template full``.
#
# Every field below validates against the live v3 schema. Delete blocks
# you don't need rather than chase the spec reference for what's
# available.

apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:
    role: worker
    team: scitex
    description: |
      {name} — fresh v3 agent. Replace this description and the
      startup prompt with your agent's actual mission.
    function: scaffolded
    capabilities: scaffolded
    skills: ""
    cardinality: singleton

spec:
  runtime: apptainer
  host: local
  workdir: ~/proj/{name}

  to_home: ./to_home

  python-venv: auto

  apptainer:
    image: ~/.scitex/agent-container/containers/sac-scitex.sif
    binds: []
    relaxed: false

  claude:
    model: sonnet
    flags:
      - --dangerously-skip-permissions
    session: continue
    auto_accept: true

  startup_prompts:
    - |
      You are {name}. Replace this prompt with your agent's actual
      mission. Reply READY when initialized.

  health:
    enabled: true
    interval: 60
    timeout: 10
    method: sdk-alive

  restart:
    policy: on-failure
    max_retries: 3
    backoff:
      initial: 10
      max: 120
      multiplier: 2

  a2a:
    port: auto

# EOF
"""


_TEMPLATES = {
    "minimal": _MINIMAL_TEMPLATE,
    "full": _FULL_TEMPLATE,
}


def _default_base_dir() -> Path:
    """Return ``~/.scitex/agent-container/agents`` (sac's primary root).

    Resolved lazily so the CLI can be imported without touching the
    filesystem (matters for cold-start budget + tab-completion).
    """
    return Path.home() / ".scitex" / "agent-container" / "agents"


@click.command(name="new")
@click.argument("name", type=str)
@click.option(
    "--template",
    "template_name",
    type=click.Choice(sorted(_TEMPLATES), case_sensitive=False),
    default="minimal",
    show_default=True,
    help=(
        "Template preset to scaffold. 'minimal' = bare-minimum spec "
        "(image + model). 'full' = annotated v3 spec with every "
        "common block pre-filled."
    ),
)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Parent directory under which ``<name>/spec.yaml`` is written. "
        "Default: ~/.scitex/agent-container/agents/ (sac's primary "
        "agents root, matches the resolver search-chain entry #1)."
    ),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help=(
        "Overwrite an existing spec.yaml. Off by default so accidental "
        "re-runs cannot clobber a customised spec."
    ),
)
def new(name: str, template_name: str, base_dir: Path | None, force: bool) -> None:
    """Scaffold a fresh v3 ``spec.yaml`` for a new agent.

    Writes ``<base-dir>/<name>/spec.yaml`` (plus an empty ``to_home/``
    sibling) from the chosen template. The template is guaranteed to
    pass ``validate_config`` without operator edits.

    Examples:

    \b
        sac agents new my-agent
        sac agents new my-agent --template full
        sac agents new my-agent --base-dir ./agents --force
    """
    if not _is_valid_agent_name(name):
        raise click.UsageError(
            f"Invalid agent name {name!r}. Use lowercase letters, "
            "digits, '-', and '_' only (dir-as-SSoT convention)."
        )

    base = base_dir if base_dir is not None else _default_base_dir()
    agent_dir = base / name
    spec_path = agent_dir / "spec.yaml"

    if spec_path.exists() and not force:
        raise click.ClickException(
            f"Refusing to overwrite existing spec at {spec_path}. "
            "Re-run with --force to replace, or pick a different name."
        )

    template = _TEMPLATES[template_name.lower()]
    body = template.format(name=name)

    agent_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body)

    # to_home/ sibling — the runtime auto-discovers it as the materialised
    # container $HOME source (ADR-0006). Empty dir is fine; operator
    # adds CLAUDE.md / .mcp.json / .claude/... as needed.
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True, exist_ok=True)

    print(
        f"Wrote {spec_path} (template={template_name}).",
        file=sys.stderr,
        flush=True,
    )


__all__ = ["new"]
