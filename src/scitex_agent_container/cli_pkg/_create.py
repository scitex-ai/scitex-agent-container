#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents create NAME`` — scaffold a fresh v3 agent spec.

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
  * ``full`` — the PROVEN developer shape the fleet's live dev agents
    use (card sac-agents-new-template-stale, operator 2026-06-25:
    "very general, just developer like existing ones"). NOT a bare
    knob-tour: it renders a ready-to-run project-maintainer agent —
    runtime: tui, relaxed + persistent directory overlay,
    full-home reach at the canonical path, the fleet push channels
    (``server:sac`` + ``server:scitex-todo`` + telegrammer),
    ``SCITEX_TODO_AGENT_ID`` wired to the agent, an editable install of
    the agent's own repo, a generic "Start or continue." kick, opus
    model, and metadata.labels. Parameterised by ``{name}`` (the agent
    == project) and ``{home}`` (the operator's home, filled at render
    time so the whole-home bind + workdir are absolute yet
    operator-agnostic). Edit the labels + prompt for the real mission;
    delete blocks you don't need.

The CLI refuses to overwrite an existing ``spec.yaml`` unless ``--force``
is passed — protects the "60 stale ``*-quality`` specs already pending
uncommitted" scenario the card calls out.

Named ``create`` (renamed from ``new``, card
refactor/consolidate-create-into-new-templates): CRUD naming
(Create/Read/Update/Delete) lines up with the existing ``delete``
verb. The old narrow ``sac agents create`` (auto-detected
editable-install toggle, marker-block machinery) was retired in the
same card and folded into this command's dir-template system, which
freed the ``create`` name back up.
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
# {name} — fresh v3 spec scaffolded by ``sac agents create``.
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
# {name} — fresh v3 DEVELOPER spec scaffolded by ``sac agents create --template full``.
#
# This is the PROVEN developer shape the fleet's live dev agents use
# (card sac-agents-new-template-stale; operator 2026-06-25: "very
# general, just developer like existing ones") — a ready-to-run
# project-maintainer agent, NOT a bare skeleton. It ships:
#   * runtime: tui                interactive in-apptainer Claude TUI
#   * relaxed + directory overlay persistent per-agent $HOME / installs
#   * full host reach at the canonical path (so ~/proj/... paths match)
#   * fleet push channels         server:sac + server:scitex-todo + telegrammer
#   * SCITEX_TODO_AGENT_ID        todo-store writes attribute to THIS agent
#   * editable install of the agent's own repo (live dev loop)
#   * a generic "Start or continue." kick + metadata.labels + opus model
# Every field validates against the live v3 schema. Edit the labels + the
# startup prompt for the agent's real mission; delete blocks you don't need.

apiVersion: scitex-agent-container/v3
kind: Agent

metadata:
  labels:
    role: project-maintainer
    team: scitex
    project: {name}
    purpose: {name}-maintainer
    description: |
      {name} developer agent — maintains the {name} repo. Replace this
      description and the startup prompt below with the agent's real mission.
    capabilities: develop, test, review, release
    cardinality: singleton

spec:
  runtime: tui
  host: local

  # The in-container --pwd. The repo is bound at this SAME absolute path
  # below, so host and container agree (editable install, git, tooling).
  workdir: {home}/proj/{name}

  # to_home/ sibling — mirrored into the container $HOME (overlay upper
  # home under relaxed mode). Put CLAUDE.md / .mcp.json / .claude/ there.
  to_home: ./to_home

  python-venv: auto

  apptainer:
    # sac-base.sif = the minimal layer; the agent editable-installs its
    # own stack into the overlay below. Swap to sac-scitex.sif to start
    # from the full pre-baked scitex stack instead.
    image: ~/.scitex/agent-container/containers/sac-base.sif

    # Relaxed isolation — the dev agent shares the operator's identity and
    # host tree (the fleet dev default). Pairs with the raw_args below,
    # which re-declare the namespace + canonical HOME the overlay needs.
    relaxed: true

    # Persistent per-agent directory overlay: package installs, caches and
    # $HOME state survive restarts while the base SIF stays immutable. sac
    # auto-creates the overlay dir and materialises to_home/ into its upper
    # home on first start.
    overlay: ~/.scitex/agent-container/containers/overlays/{name}/

    # Full host reach at the CANONICAL path (source ~ is expanded by sac;
    # the destination must be absolute). Narrow this to specific project
    # trees for a capsule-style agent.
    binds:
      - {home}:{home}:rw

    # Per-agent env. SCITEX_TODO_AGENT_ID makes scitex-todo writes
    # attribute to THIS agent. (sac AUTO-injects
    # SCITEX_AGENT_CONTAINER_STATE_DB + binds the per-agent state dir, so
    # the state DB needs no manual entry here.)
    env:
      SCITEX_TODO_AGENT_ID: {name}

    # Relaxed mode skips sac's curated isolation prepend, so re-declare the
    # user namespace, filesystem isolation, and the canonical container HOME
    # that the overlay upper home is materialised under.
    raw_args:
      - --userns
      - --containall
      - --home=/home/agent

  claude:
    model: opus[1m]
    flags:
      - --dangerously-skip-permissions
    session: continue
    auto_accept: true

    # Fleet push channels: sac control bus + shared scitex-todo store + the
    # Telegram bridge (operator DMs wake the agent). server:sac is also
    # auto-injected by the loader; listed here for visibility.
    channels:
      - server:sac
      - server:scitex-todo
      - server:claude-code-telegrammer

    # Pin this agent to a dedicated account by uncommenting and pointing at
    # that account's LIVE .credentials.json (else the shared host OAuth is
    # forwarded automatically).
    # credentials_file: ~/.scitex/agent-container/accounts/<ACCOUNT>/.credentials.json

  # Editable-install the agent's own repo so imports resolve to the live
  # working tree (edit -> test without reinstall). The `|| true` keeps
  # startup resilient when the repo has no installable package yet.
  startup_commands:
    - command: 'uv pip install -e {home}/proj/{name} --quiet || true'

  # Generic self-resume kick — the agent picks up its board / last state.
  # Replace with the agent's real first mission.
  startup_prompts:
    - Start or continue.

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
    except Exception:  # stx-allow: fallback (reason: resolver import is best-effort; hardcoded default keeps `sac agents create` working if config pkg shifts)
        return Path.home() / ".scitex" / "agent-container" / "agents"


def _extract_base_dir_arg(raw_args: list[str]) -> Path | None:
    """Recover an explicit ``--base-dir`` value from the raw argv.

    ``--help`` is an eager click option: its callback prints help and
    exits before non-eager options (like ``--base-dir``) are parsed into
    ``ctx.params``. Scanning the raw args directly (stashed by
    :meth:`_CreateCommand.parse_args`) lets the dynamic help epilog honor
    an explicit ``--base-dir`` regardless of where ``--help`` falls in the
    invocation.
    """
    for i, arg in enumerate(raw_args):
        if arg == "--base-dir" and i + 1 < len(raw_args):
            return Path(raw_args[i + 1])
        if arg.startswith("--base-dir="):
            return Path(arg.split("=", 1)[1])
    return None


class _CreateCommand(click.Command):
    """``create`` with a live-scanned template list in its ``--help`` output.

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
        ctx.meta["_create_raw_args"] = list(args)
        return super().parse_args(ctx, args)

    def format_epilog(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        super().format_epilog(ctx, formatter)
        raw_args = ctx.meta.get("_create_raw_args", [])
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


@click.command(name="create", cls=_CreateCommand)
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
        "`sac agents create --help` to see the CURRENT live list."
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
def create(
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
        sac agents create my-agent
        sac agents create my-agent --template full
        sac agents create dev1 --template python_developer --project myproj
        sac agents create r1 --template researcher --project p --set TEAM=x
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
    # ``{home}`` is filled from the operator's home so the full template's
    # whole-home bind + workdir are ABSOLUTE (apptainer bind targets can't
    # be ``~``/``$VAR``) yet still operator-agnostic — mirrors sac's own
    # "full host reach" hint (`- {home}:{home}:rw`). The minimal template
    # has no ``{home}`` token; the surplus kwarg is harmless.
    body = template.format(name=name, home=str(Path.home()))

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


__all__ = ["create"]
