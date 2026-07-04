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

from ._new_dir_template import (
    DirTemplateError,
    discover_dir_templates,
    instantiate_dir_template,
    parse_set_pairs,
)

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
    """Return sac's primary agents root (the resolver's ``primary`` base).

    Reuses :func:`scitex_agent_container.config._resolve._search_dirs`
    so there is a single source of truth for "where agents live" — the
    dir-template discovery scans this same root. Resolved lazily so the
    CLI can be imported without touching the filesystem (matters for
    cold-start budget + tab-completion).
    """
    try:
        from ..config._resolve import _search_dirs

        primary, _env, _fleet = _search_dirs()
        return primary
    except Exception:  # stx-allow: fallback (reason: resolver import is best-effort; hardcoded default keeps `sac agents new` working if config pkg shifts)
        return Path.home() / ".scitex" / "agent-container" / "agents"


def _extract_base_dir_arg(raw_args: list[str]) -> Path | None:
    """Recover an explicit ``--base-dir`` value from the raw argv.

    ``--help`` is an eager click option: its callback prints help and
    exits before non-eager options (like ``--base-dir``) are parsed into
    ``ctx.params``. Scanning the raw args directly (stashed by
    :meth:`_NewCommand.parse_args`) lets the dynamic help epilog honor an
    explicit ``--base-dir`` regardless of where ``--help`` falls in the
    invocation.
    """
    for i, arg in enumerate(raw_args):
        if arg == "--base-dir" and i + 1 < len(raw_args):
            return Path(raw_args[i + 1])
        if arg.startswith("--base-dir="):
            return Path(arg.split("=", 1)[1])
    return None


class _NewCommand(click.Command):
    """``new`` with a live-scanned template list in its ``--help`` output.

    The ``--template`` option's static ``help=`` text cannot name the
    current ``_template_*`` set — dir-templates are dropped into the
    agents root with no code change (see :func:`discover_dir_templates`).
    Overriding :meth:`format_epilog` defers that scan to ``--help``
    invocation time so the printed list is always live, never stale.
    """

    def parse_args(self, ctx: click.Context, args: list[str]) -> list[str]:
        # Stash the raw args so format_epilog can recover --base-dir even
        # when --help (eager) exits before --base-dir (non-eager) would
        # otherwise be parsed into ctx.params.
        ctx.meta["_new_raw_args"] = list(args)
        return super().parse_args(ctx, args)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_epilog(ctx, formatter)
        raw_args = ctx.meta.get("_new_raw_args", [])
        base = _extract_base_dir_arg(raw_args) or _default_base_dir()
        dir_templates = sorted(discover_dir_templates(base))
        with formatter.section("Available templates (live-scanned)"):
            formatter.write_text("inline: " + ", ".join(sorted(_TEMPLATES)))
            if dir_templates:
                formatter.write_text(
                    f"_template_* under {base}: " + ", ".join(dir_templates)
                )
            else:
                formatter.write_text(
                    f"_template_* under {base}: (none found — drop a "
                    "_template_<kind>/ dir there to add one, no code change "
                    "needed)"
                )


@click.command(name="new", cls=_NewCommand)
@click.argument("name", type=str)
@click.option(
    "--template",
    "template_name",
    type=str,
    default="minimal",
    show_default=True,
    help=(
        "Template kind: 'minimal'/'full' (built-in) or any "
        "_template_<kind>/ dir under the agents root. Run "
        "`sac agents new --help` to see the CURRENT live list."
    ),
)
@click.option(
    "--project",
    "project",
    type=str,
    default=None,
    help=(
        "Fill the ``SAC_PLACEHOLDER_PROJECT`` token in dir-templates. "
        "Required if the chosen dir-template carries that token."
    ),
)
@click.option(
    "--agent-id",
    "agent_id",
    type=str,
    default=None,
    help=(
        "Fill the ``SAC_PLACEHOLDER_AGENT_ID`` token in dir-templates. "
        "Defaults to <name> when omitted."
    ),
)
@click.option(
    "--set",
    "set_pairs",
    type=str,
    multiple=True,
    metavar="KEY=VALUE",
    help=(
        "Fill an arbitrary ``SAC_PLACEHOLDER_<KEY>`` token by exact name "
        "(KEY is upper-cased). Repeatable, e.g. --set EXTRA=val."
    ),
)
@click.option(
    "--base-dir",
    "base_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help=(
        "Parent directory under which ``<name>/spec.yaml`` is written "
        "AND scanned for ``_template_*`` dir-templates. Default: sac's "
        "primary agents root (resolver search-chain entry #1)."
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
def new(
    name: str,
    template_name: str,
    project: str | None,
    agent_id: str | None,
    set_pairs: tuple[str, ...],
    base_dir: Path | None,
    force: bool,
) -> None:
    """Scaffold a fresh v3 ``spec.yaml`` for a new agent.

    Writes ``<base-dir>/<name>/spec.yaml`` (plus a ``to_home/`` sibling)
    from the chosen template. Inline templates (``minimal``/``full``)
    render a string; DIR-templates (``_template_<kind>/`` under the
    agents root) are copied wholesale and have their
    ``SAC_PLACEHOLDER_*`` tokens filled from ``--project``/``--agent-id``/
    ``--set``. Any surviving placeholder fails the command loud and
    removes the partial output.

    Examples:

    \b
        sac agents new my-agent
        sac agents new my-agent --template full
        sac agents new dev1 --template python_developer --project myproj
        sac agents new r1 --template researcher --project p --set TEAM=x
    """
    if not _is_valid_agent_name(name):
        raise click.UsageError(
            f"Invalid agent name {name!r}. Use lowercase letters, "
            "digits, '-', and '_' only (dir-as-SSoT convention)."
        )

    base = base_dir if base_dir is not None else _default_base_dir()
    agent_dir = base / name
    spec_path = agent_dir / "spec.yaml"

    kind = template_name.lower()
    dir_templates = discover_dir_templates(base)

    # Dispatch: dir-template kind wins over inline names ONLY if it does
    # not collide with the reserved inline names; a same-named inline
    # preset always refers to the string template (operator can't shadow
    # 'minimal'/'full' with a dir).
    is_inline = kind in _TEMPLATES
    is_dir = kind in dir_templates and not is_inline

    if not is_inline and not is_dir:
        choices = sorted(set(_TEMPLATES) | set(dir_templates))
        raise click.UsageError(
            f"Unknown template {template_name!r}. Available: "
            f"{', '.join(choices)} "
            f"(scanned {base} for _template_* dirs)."
        )

    if is_dir:
        try:
            extra = parse_set_pairs(set_pairs)
            instantiate_dir_template(
                dir_templates[kind],
                agent_dir,
                project=project,
                agent_id=agent_id if agent_id is not None else name,
                extra=extra,
                force=force,
            )
        except DirTemplateError as exc:
            raise click.ClickException(str(exc)) from exc
        print(
            f"Wrote {agent_dir} (template={kind}, dir-template).",
            file=sys.stderr,
            flush=True,
        )
        return

    if spec_path.exists() and not force:
        raise click.ClickException(
            f"Refusing to overwrite existing spec at {spec_path}. "
            "Re-run with --force to replace, or pick a different name."
        )

    template = _TEMPLATES[kind]
    body = template.format(name=name)

    agent_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body)

    # to_home/ sibling — the runtime auto-discovers it as the materialised
    # container $HOME source (ADR-0006). Empty dir is fine; operator
    # adds CLAUDE.md / .mcp.json / .claude/... as needed.
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True, exist_ok=True)

    print(
        f"Wrote {spec_path} (template={kind}).",
        file=sys.stderr,
        flush=True,
    )


__all__ = ["new"]
