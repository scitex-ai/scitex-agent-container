#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agent-spec scaffolding shared by the CLI and Agents GUI.

``create`` validates CLI agent names and reserved slots, then delegates
writes to ``scaffold_agent``. The GUI uses that same backend and must apply
its own caller-side name validation; the backend does not repeat it.

Built-in ``minimal`` (default) and ``full`` field definitions live in
``_create_templates``. They emit explicit specs with model and endpoint
selection delegated to the engine. The full preset uses the canonical
``SCITEX_CARDS_AGENT_ID`` identity. Consult those definitions and the
matching template tests for current runtime, harness and channel settings.

Directory templates are discovered under the selected agents root. Their
placeholders are filled through ``_new_dir_template``; unresolved tokens
fail rather than leaving an incomplete successful scaffold. Existing inline
specs are not overwritten, and force replacement is unsupported.

Scaffolding writes a spec and home skeleton; it does not launch an agent or
qualify credentials, images, engine availability or a live host. Missing
credentials and failed managed-home adoption are reported separately.
"""

from __future__ import annotations

from pathlib import Path

import click

# Inline spec templates live in ``_create_templates`` (per-file line
# cap). Fully-explicit bodies per the red-start ruling 2026-07-21.
from ._create_templates import (  # noqa: F401
    _FULL_TEMPLATE,
    _MINIMAL_TEMPLATE,
    _TEMPLATES,
)
from ._helpers._console import system_msg
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


def _accounts_root() -> Path:
    """The anthropic account store, mirroring ``_account.codex_account``."""
    return Path.home() / ".scitex" / "agent-container" / "accounts" / "anthropic"


def _discover_credentials_pool_block() -> tuple[str, bool]:
    """Return ``(yaml_block, found_any)`` for every discovered OAuth file.

    Include the complete creating user's pool; a single entry is valid.
    File presence does not establish validity, remaining capacity or model
    availability. An empty pool means no OAuth files were discovered here,
    not that an explicit provider/API-key authentication path is unusable.

    Discovery uses the creating process's home, which may differ from the
    target host's home inside a container. Scaffolding reports the missing
    pool; start-time checks and actual provider requests remain separate.
    """
    root = _accounts_root()
    try:
        creds = sorted(p for p in root.glob("*/.credentials.json") if p.is_file())
    except OSError:
        creds = []
    if not creds:
        return "[]", False
    return "".join(f"\n      - {p}" for p in creds), True


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
    "--dry-run",
    is_flag=True,
    help="Preview arguments without reading templates or credentials, or writing a scaffold.",
)
def create(
    name: str,
    template_name: str,
    project: str | None,
    agent_id: str | None,
    set_pairs: tuple[str, ...],
    base_dir: Path | None,
    dry_run: bool,
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
        sac agents create my-agent --dry-run
        sac agents create my-agent --template full
        sac agents create dev1 --template python_developer --project myproj
        sac agents create r1 --template researcher --project p --set TEAM=x
    """
    force = False
    if not _is_valid_agent_name(name):
        raise click.UsageError(
            f"Invalid agent name {name!r}. Use lowercase letters, "
            "digits, '-', and '_' only (dir-as-SSoT convention)."
        )

    # Reserved slots (the host-process role name) are refused at the front
    # door, before anything is written — the collision story lives in
    # config/_reserved_names.py. Imported lazily for the same cold-start
    # budget reason as _default_base_dir (config is a heavy package).
    from ..config._reserved_names import reserved_agent_name_error

    reserved_msg = reserved_agent_name_error(name)
    if reserved_msg is not None:
        raise click.UsageError(reserved_msg)

    if dry_run:
        from ._create_preview import _show_creation_preview

        _show_creation_preview(
            name,
            template_name=template_name,
            project=project,
            agent_id=agent_id,
            set_pairs=set_pairs,
            base_dir=base_dir,
        )
        return

    scaffold_agent(
        name,
        template_name=template_name,
        project=project,
        agent_id=agent_id,
        set_pairs=set_pairs,
        base_dir=base_dir,
        force=force,
    )


def scaffold_agent(
    name: str,
    *,
    template_name: str = "minimal",
    project: str | None = None,
    agent_id: str | None = None,
    set_pairs: tuple[str, ...] = (),
    base_dir: Path | None = None,
    force: bool = False,
) -> Path:
    """Scaffold a fresh v3 ``spec.yaml`` + ``to_home/`` for ``name``.

    PUBLIC ON PURPOSE — the shared backend behind BOTH ``sac agents
    create`` and the Agents GUI create flow. The GUI must never carry
    its own copy of this logic: one template set, one validation
    chain, one failure shape. Raises :class:`click.UsageError` for an
    unknown template and :class:`click.ClickException` for an
    existing spec (callers map these to their own
    loud failure surface). Returns the written ``spec.yaml`` path.

    Name-shape validation (dir-as-SSoT chars + reserved slots) stays
    with the CALLERS — both must refuse before anything is written.
    """
    if force:
        raise click.ClickException(
            "force is unsupported; edit an existing spec explicitly"
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
        # Stamp the immutable spec identity BEFORE adoption so the adopting
        # commit carries it (card sac-identity-spec-uuid-20261026).
        # Lazy import: config parsing stays OFF the `sac --help` import
        # closure (see 21_cli-startup-budget.md).
        from ..config._spec_uuid import ensure_spec_uuid

        ensure_spec_uuid(agent_dir / "spec.yaml")
        _adopt_agent_dir(agent_dir, name)
        system_msg(f"Wrote {agent_dir} (template={kind}, dir-template).")
        return agent_dir / "spec.yaml"

    if spec_path.exists():
        raise click.ClickException(
            f"Refusing to overwrite existing spec at {spec_path}. "
            "Choose a different name or edit the existing spec explicitly."
        )

    template = _TEMPLATES[kind]
    # ``{home}`` is filled from the operator's home so the full template's
    # whole-home bind + workdir are ABSOLUTE (apptainer bind targets can't
    # be ``~``/``$VAR``) yet still operator-agnostic — mirrors sac's own
    # "full host reach" hint (`- {home}:{home}:rw`). ``{host}`` is the
    # creating machine's RESOLVED hostname — created specs carry concrete
    # placement (``host: local`` is banned; operator directive 2026-07-10).
    # Surplus kwargs for tokens a template lacks are harmless.
    from .._state.host_scratch import resolve_scratch_root, scratch_agent_dir
    from ..config._host import resolve_hostname

    scratch = resolve_scratch_root()
    if scratch.root is None:
        # ``scratch_root: none`` is an explicit, reasoned host decision.  New
        # specs remain usable there, but make the root-backed placement
        # visible; the launch-space gate will protect a constrained root LV.
        overlay = (
            Path.home()
            / ".scitex"
            / "agent-container"
            / "containers"
            / "overlays"
            / name
        )
    else:
        overlay = scratch_agent_dir(scratch.root, name) / "overlay"

    creds_block, creds_found = _discover_credentials_pool_block()
    if not creds_found:
        # Report discovery without inferring the target's auth or readiness.
        click.echo(
            click.style(
                f"WARNING: no account credentials found under "
                f"{_accounts_root()} — writing `credentials_files: []`.\n"
                f"         OAuth credentials were not discovered for {name!r}. "
                f"Configure the intended account pool or an explicit "
                f"provider/API-key authentication path before starting.\n"
                f"         Scaffolding does not verify credentials or "
                f"provider availability.",
                fg="red",
            ),
            err=True,
        )

    body = template.format(
        name=name,
        home=str(Path.home()),
        host=resolve_hostname(),
        overlay=str(overlay),
        credentials_files=creds_block,
    )

    agent_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(body)
    # Mint the immutable spec identity at create (card
    # sac-identity-spec-uuid-20261026). Loud on failure: a scaffold
    # without uuid violates the invariant this change establishes.
    # Lazy import: config parsing stays OFF the `sac --help` import
    # closure (see 21_cli-startup-budget.md).
    from ..config._spec_uuid import ensure_spec_uuid

    ensure_spec_uuid(spec_path)

    # to_home/ sibling — the runtime auto-discovers it as the materialised
    # container $HOME source (ADR-0006). Empty dir is fine; operator
    # adds CLAUDE.md / .mcp.json / .claude/... as needed.
    to_home = agent_dir / "to_home"
    to_home.mkdir(parents=True, exist_ok=True)

    _adopt_agent_dir(agent_dir, name)

    system_msg(f"Wrote {spec_path} (template={kind}).")
    return spec_path


def _adopt_agent_dir(agent_dir: Path, name: str) -> None:
    """Adopt the new spec into git management, or say why not — loudly.

    Inside a ``.scitex`` tree the containing home is adopted (tracked
    spec files committed) so the launch gate can prove the spec — the
    home is derived FROM THE SPEC PATH, never from the process user, so
    a root process with data under /home/user adopts the right tree.
    Outside any ``.scitex`` tree — custom ``--base-dir``, test fixtures —
    nothing is touched: the operator owns that repo and commits it by
    hand, and the gate proves THAT repo at launch. Both branches report;
    neither is silent.

    Create-side is warn-not-fail by deliberate asymmetry with the launch
    gate: the spec files are the deliverable and must land even where
    ``scitex-dev`` is older than the ``scitex_dev.home`` floor, while the
    launch path (``_start_preflight``) stays fail-closed and refuses with
    the same message. So an unmanaged home can never launch silently —
    but creating one never destroys work either.
    """
    from .._drift._managed_home import ensure_home_managed, home_for_spec

    spec_home = home_for_spec(agent_dir)
    if spec_home is None:
        system_msg(
            f"Spec is outside any .scitex tree ({agent_dir}): leaving its "
            f"repo untouched — commit it by hand; the launch gate will "
            f"prove that repo instead.",
            style="info",
        )
        return
    try:
        ensure_home_managed(home=spec_home, commit_message=f"sac: create agent {name}")
    except RuntimeError as exc:
        click.echo(
            click.style(
                f"WARNING: ~/.scitex is NOT git-managed ({exc}). The spec "
                f"above was written but WILL NOT LAUNCH until the home is "
                f"adopted — upgrade scitex-dev past the scitex_dev.home "
                f"floor and re-run create, or adopt the home by hand.",
                fg="red",
            ),
            err=True,
        )


__all__ = ["create", "scaffold_agent"]
