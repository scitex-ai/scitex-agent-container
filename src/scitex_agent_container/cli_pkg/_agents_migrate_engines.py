"""``sac agents migrate-engines`` — give every spec a HARNESS x ENGINE block.

The operator's direction (Telegram, 2026-09-05/06): rewrite every agent spec
so HARNESS and ENGINE are separate axes, so any agent can be started on the
Qwen engine instead of Claude, and so Qwen can eventually become the DEFAULT.
The axis already exists in code — ``--engine`` at start, ``spec.engines`` in
the loader; what was missing is the sweep that gives all 119 specs the block.
1 of them has it today.

And the condition attached to it, verbatim:「一気に書き換えるコマンドめちゃ
くちゃ怖いので、ちゃんと git で管理してくださいね。大体置換のコードって
いつも失敗するんですよね。」— a command that rewrites everything at once is
frightening, keep it in git, bulk-replacement code always fails. Three things
answer that and they are the command's shape:

  * **Dry-run is the DEFAULT.** ``--apply`` is the deliberate act, and the
    dry-run prints a real unified diff per spec rather than a count to trust.
  * **Batches that ADVANCE.** ``--agent`` names an explicit set, ``--host``
    takes one machine at a time, and ``--limit N`` caps what gets WRITTEN —
    not what gets examined — so running the same command again takes the
    NEXT N. Capping the selection instead re-picked the same first N
    forever: batch two wrote nothing and announced a completed sweep.
  * **A measured gate on the apply.** Every selected spec is loaded through
    the production loader before the write and again after; unless the
    effective backend is identical for every one, every original is restored
    from the archive taken first. Every write is a temp file plus
    ``os.replace``, and a failure part-way rolls the batch back instead of
    aborting the process on a traceback over a half-migrated fleet.

An unmigratable spec is REFUSED BY NAME with its reason, never skipped —
skipping is how a sweep reports 118 done over a fleet of 119. No filter may
make one vanish either: ``--host`` KEEPS a spec it could not read, so it
reaches the plan as unreadable rather than out of the count.

**A VERSION FLOOR, ENFORCED AT PLAN TIME.** A sac that predates engines
support REJECTS an unknown ``engines:`` key rather than ignoring it, so
writing the block into a spec pinned on such a host stops that agent
starting. The floor refuses those specs by name BEFORE the write — see
:mod:`..._maintenance._engines_floor` for the measurement, the fleet roster
and the fail-closed rule (an unmeasured host is refused, never assumed
capable). ``--host-supports-engines HOST`` lifts it for a named machine.

**Where it writes.** ``--root``, else ``$SCITEX_AGENT_CONTAINER_AGENTS_DIR``,
else every user-scope root the rest of the CLI resolves EXCEPT the
project-local one, de-duplicated by agent name. The old default read a
different env var and landed on the container's own ``$HOME`` — one spec next
to the fleet's 123, reported as a finished sweep. Every report names the roots
it searched, the roots it resolved and did not search, and the PATH of every
spec it would write.

**Exit codes.** ``0`` the plan is sound (a named refusal is NOT a failure),
``1`` a spec is unreadable or no roster was searched, ``2`` the apply was
refused or rolled back. ``exit 0`` does NOT mean the migration is finished —
a run whose every spec was refused also exits 0. ``migration_complete`` in
``--json`` is the field that answers that question, and it is FALSE for any
run that did not cover the whole roster: a ``--agent``/``--host`` filter, a
``--limit`` batch, an unmigrated template, or a spec.yaml shadowed by an
earlier root. The selectors themselves are echoed into the payload, so a
filtered census can never be mistaken for a full one.

``--preflight`` probes the gateway the migration points at and reports a
NAMED state rather than a boolean, because the two failure shapes look
identical through curl: ``scitex-compute-04:18772`` answers 401 (reachable
and auth-gating) while ``compute-04:18772`` answers ``000`` — which reads as
"the gateway is down" and means "the hostname does not resolve". It dials
``/v1/models`` rather than the base, which answers 404: a 401 there proves
the inference API is present AND gating, while the base's 404 proves only
that some process holds the port. See :mod:`...config._engine_reach`.
"""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path as _Path

import click

from .._maintenance._engines_floor import EngineFloor
from .._maintenance._engines_migration import (
    apply_engines_migration,
    plan_engines_migration,
    select_spec_paths_over_roots,
)
from ._agents_migrate_engines_preflight import preflight_payload, render_preflight
from ._agents_migrate_engines_report import (
    plan_payload,
    render_apply,
    render_diffs,
    render_plan,
)
from ._helpers import _json_flag, console

_EXIT_OK = 0
_EXIT_PLAN_UNSOUND = 1
_EXIT_APPLY_REFUSED = 2

#: The one env var the RUNTIME actually exports for this purpose.
_AGENTS_DIR_ENV = "SCITEX_AGENT_CONTAINER_AGENTS_DIR"


def _project_local_roots() -> "tuple[_Path, ...]":
    """The registry found by WALKING UP FROM CWD, which is why it is excluded.

    ``config._resolve`` prepends "the first ``.scitex/agent-container/agents``
    found by walking upward from cwd" to the search path. That is right for
    ``sac agents start`` — a repo's checked-in fixtures should win for a name
    typed inside that repo — and wrong for a bulk rewrite, in two ways
    measured 2026-09-06 with ``$SCITEX_AGENT_CONTAINER_AGENTS_DIR`` unset::

        cwd = <the sac repo, i.e. this agent's own workdir>
            3 roots, 119 specs   (includes the repo's own sdk-test + self)
        cwd = /uvwork/tmp   or   /home/agent
            2 roots, 117 specs

    First, the sweep's scope — and with ``--apply``, its WRITE SET — changed
    with the working directory. Second, ``git ls-files`` confirms
    ``.scitex/agent-container/agents/{sdk-test,self}/spec.yaml`` are TRACKED
    repo test fixtures: from the normal invocation, ``--apply`` would rewrite
    them. Both escaped today only by accident of unrelated guards, and
    ``--host-supports-engines local`` — a plausible developer flag — put the
    tracked fixture straight back into the write set.

    So the default sweep does not include it. ``SAC_AGENT_SCOPE=project`` is
    the one exception: there the operator asked for project scope by name,
    and honouring an explicit request is not the same as inheriting a cwd.
    ``--root`` still sweeps whatever directory is named.
    """
    # Private siblings on purpose: this is a statement about THAT resolver's
    # project-local rule, so it has to consult that resolver rather than
    # re-deriving the walk-up and drifting from it.
    from ..config._resolve import _project_local_dirs, _read_scope

    if _read_scope() == "project":
        return ()
    return tuple(_Path(d) for d in _project_local_dirs())


def default_spec_roots() -> "tuple[_Path, ...]":
    """Where to sweep when neither ``--root`` nor the env var says.

    Every user-scope root except the cwd-derived project-local one (see
    :func:`_project_local_roots`), in the order
    ``config._resolve.resolve_config`` itself resolves them: the home primary,
    then ``$SCITEX_AGENT_CONTAINER_YAML_DIRS``. Measured inside this container
    on 2026-09-06, with ``$SCITEX_AGENT_CONTAINER_AGENTS_DIR`` unset and
    ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` set by the runtime to the
    operator's tree::

        fleet_agents_dir() -> /home/agent/.scitex/…/agents            1 spec
        this function     -> /home/agent/.scitex/…/agents            1 spec
                             /home/ywatanabe/.scitex/…/agents      123 specs

    ``fleet_agents_dir`` reads a DIFFERENT variable, which the runtime does
    not set, and lands on the container's own ``$HOME`` — a root holding one
    spec, next to the fleet's 123. A sweep defaulting there does not refuse:
    it reports one spec and exits 0, which is the shape of a finished
    migration.

    THE ORDER IS THE LOADER'S. ``user_scope_roots`` returns ``[primary,
    *project_local, *operator_env_dirs]`` while ``resolve_config`` resolves
    ``project_local -> primary -> operator_env_dirs``: on a name collision the
    sweep would have migrated the copy the loader does not load, and the
    report could not have said which. Dropping project-local removes that
    disagreement rather than papering over it — what is left, primary before
    the operator dirs, is the loader's own order. A collision between the two
    that remain is reported as ``shadowed`` either way.

    ``fleet_agents_dir`` is deliberately NOT fixed here — four other callers
    share it and that consolidation is its own card.
    """
    import os

    override = (os.environ.get(_AGENTS_DIR_ENV) or "").strip()
    if override:
        return (_Path(override).expanduser(),)
    from ._helpers._agent_list_roots import user_scope_roots

    excluded = set(_project_local_roots())
    return tuple(_Path(r) for r in user_scope_roots() if _Path(r) not in excluded)


def excluded_spec_roots() -> "tuple[_Path, ...]":
    """Roots this run RESOLVED and deliberately did not sweep.

    Reported for the same reason ``roots_absent`` is: a root that was resolved
    and then left out has to be visible, or the count reads as covering it.
    """
    import os

    if (os.environ.get(_AGENTS_DIR_ENV) or "").strip():
        return ()
    from ._helpers._agent_list_roots import user_scope_roots

    resolved = {_Path(r) for r in user_scope_roots()}
    return tuple(r for r in _project_local_roots() if r in resolved)


def _selectors(agents: "tuple[str, ...]", hosts: "tuple[str, ...]") -> "tuple[str, ...]":
    """The narrowing flags this run was given, spelled back as flags.

    A filtered run is a census of a SUBSET, and nothing recorded that: the
    payload carried no field naming the filter, so ``-a business --apply``
    over a 113-spec root reported ``specs: 1``, ``migration_complete: true``
    and printed "this is what a completed one looks like" while naming the
    full root it had not covered.
    """
    return tuple(
        [f"--agent {a}" for a in agents if a] + [f"--host {h}" for h in hosts if h]
    )


def _show_preflight(payload: dict) -> None:
    """Print the gateway verdict on EVERY path that paid for the probe.

    ``--preflight`` is computed before the apply/dry-run branch, so the probe
    is a real network round trip on every path — and it was rendered only in
    the dry-run branch. ``--preflight --apply``, the natural "check the
    gateway, then write" invocation, therefore printed no red, no yellow and
    no green line at all. Exactly the defect this module's docstring records
    having fixed for ``--diff``.
    """
    if payload.get("preflight"):
        render_preflight(payload["preflight"])
        console.print("")


def _archive_dir():
    from .._runtime_paths import runtime_base_dir

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return runtime_base_dir() / "engines-migration" / stamp


def _complete_after_apply(plan, result) -> bool:
    """Is the migration finished, given what this apply actually did?

    Distinct from ``plan.is_complete``, which is the question BEFORE the
    write: a successful apply retires the ``migrated`` bucket, and what is
    left is whatever no further write of this batch can clear.

    Everything else :attr:`EnginesPlan.outstanding` names still counts. The
    two claims are the same claim minus one bucket, so this asks the plan
    rather than re-listing the conditions — re-listing them is how the
    ``--agent`` filter and the skipped templates came to be invisible here
    while the dry-run reported them.
    """
    if not result.applied:
        return False
    return not plan.remaining(migrated_written=True)


@click.command(name="migrate-engines")
@click.option(
    "--apply",
    "apply",
    is_flag=True,
    default=False,
    help="ACTUALLY write the engines blocks. Without this, nothing is written.",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Report only, write nothing. This is already the DEFAULT.",
)
@click.option(
    "-a",
    "--agent",
    "agents",
    multiple=True,
    metavar="NAME",
    help="Restrict to these agents. Repeatable. The smallest batch.",
)
@click.option(
    "--host",
    "hosts",
    multiple=True,
    metavar="HOST",
    help="Restrict to specs placed on this host. Repeatable.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    metavar="N",
    help=(
        "Write at most N specs this run. Already-migrated and refused specs "
        "do not consume the cap, so repeating the command ADVANCES."
    ),
)
@click.option(
    "--root",
    "root_opt",
    type=click.Path(file_okay=False, path_type=_Path),
    default=None,
    metavar="DIR",
    help=(
        "The agents/ directory to sweep. Defaults to "
        "$SCITEX_AGENT_CONTAINER_AGENTS_DIR, else EVERY user-scope root the "
        "rest of the CLI resolves, de-duplicated by agent name."
    ),
)
@click.option(
    "--host-supports-engines",
    "floor_overrides",
    multiple=True,
    metavar="HOST",
    help=(
        "Assert that HOST runs a sac new enough to parse spec.engines, "
        "lifting the version floor for the specs pinned there. Repeatable. "
        "Per-host on purpose: the claim is explicit and lands in --json."
    ),
)
@click.option(
    "--templates",
    is_flag=True,
    default=False,
    help="Also migrate the _-prefixed template specs that `agents create` copies.",
)
@click.option(
    "--diff/--no-diff",
    default=True,
    help="Print a unified diff per spec. On by default; the whole point.",
)
@click.option(
    "--preflight",
    is_flag=True,
    default=False,
    help="Probe the Qwen gateway and report a NAMED state (401 means reachable).",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
@click.pass_context
def migrate_engines(
    ctx: click.Context,
    apply: bool,
    dry_run: bool,
    agents: "tuple[str, ...]",
    hosts: "tuple[str, ...]",
    limit: "int | None",
    root_opt: "_Path | None",
    floor_overrides: "tuple[str, ...]",
    templates: bool,
    diff: bool,
    preflight: bool,
    as_json: bool,
) -> None:
    """Declare HARNESS x ENGINE in every spec. Dry-run by default.

    Each spec's CURRENT backend becomes the DEFAULT engine, restated verbatim,
    and a `qwen38-27b` entry is added pointing at the fleet gateway by NAME —
    the address stays in config/_provider_registry, not in 119 files.
    `spec.claude.model` and `spec.claude.provider` are emptied because the
    engines now carry them; `spec.harness` stays and agrees with the default
    engine. Comments are preserved, and the edit verifies that itself.

    \b
    Preview (read-only — the DEFAULT):
      $ sac agents migrate-engines
      $ sac agents migrate-engines --no-diff --json
    \b
    In batches (`--limit` advances: run it again for the next N):
      $ sac agents migrate-engines -a business -a handyman-02
      $ sac agents migrate-engines --host scitex-compute-04 --limit 5 --apply
    \b
    Somewhere other than the live copy:
      $ sac agents migrate-engines --root ~/.dotfiles/src/.scitex/agent-container/agents
    \b
    Gateway reachability, named states (it dials /v1/models, not the base):
      $ sac agents migrate-engines --preflight --no-diff
    \b
    Lifting the version floor for a host you have checked yourself:
      $ sac agents migrate-engines --host-supports-engines scitex-compute-02

    WHERE IT WRITES. `--root`, else $SCITEX_AGENT_CONTAINER_AGENTS_DIR, else
    EVERY user-scope root the rest of the CLI resolves, de-duplicated by agent
    name. Every report names the roots it searched; pass `--root` to sweep the
    tracked tree instead.

    THE VERSION FLOOR. A sac older than 2026-09-03 rejects `engines:` as an
    unknown spec field, so a spec written for such a host stops loading and
    the agent stops starting. Specs pinned on a host measured as pre-engines,
    or on one nobody measured, are REFUSED by name before anything is written
    — fail closed. `--host-supports-engines HOST` lifts it per machine.

    Exits 0 when the plan is sound (a named REFUSAL is not a failure), 1 when a
    spec is unreadable or no roster was searched, 2 when the apply was refused
    or rolled back. `migration_complete` in --json is the answer to "is the
    sweep finished" — the exit code is not.
    """
    if apply and dry_run:
        raise click.UsageError(
            "--apply and --dry-run are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to write."
        )
    if limit is not None and limit < 1:
        # A bare slice accepted this: `--limit -1` silently dropped the LAST
        # spec and reported success, so a typo turned "one spec" into "all
        # but one".
        raise click.UsageError(
            f"--limit must be a positive number of specs to write; got {limit}."
        )

    explicit_root = root_opt is not None
    roots = (_Path(root_opt),) if explicit_root else default_spec_roots()
    excluded = () if explicit_root else excluded_spec_roots()
    floor = EngineFloor.with_overrides(floor_overrides)
    selection = select_spec_paths_over_roots(
        roots, hosts=hosts, agents=agents, templates=templates
    )
    plan = plan_engines_migration(
        list(selection.paths),
        roots=roots,
        skipped_templates=list(selection.skipped_templates),
        shadowed=selection.shadowed,
        selectors=_selectors(agents, hosts),
        unmatched_agents=selection.unmatched_agents,
        unmatched_hosts=selection.unmatched_hosts,
        limit=limit,
        floor=floor,
    )
    payload = plan_payload(plan, roots, floor=floor, excluded_roots=excluded)
    payload["engine_floor_overrides"] = sorted(set(floor.allowed))
    payload["mode"] = "apply" if apply else "dry-run"
    payload["preflight"] = preflight_payload() if preflight else None

    if diff:
        payload["diffs"] = {o.agent: o.diff for o in plan.migrated}

    if not apply:
        code = _EXIT_OK if plan.safe_to_apply else _EXIT_PLAN_UNSOUND
        payload["exit_code"] = code
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(code)
        console.print("[bold]sac agents migrate-engines[/bold]  dry-run (read-only)\n")
        _show_preflight(payload)
        render_plan(plan, payload, diff=diff)
        if plan.migrated:
            console.print(
                "\nNothing was written — this is a dry-run. To act:\n"
                "    sac agents migrate-engines --apply"
            )
        raise SystemExit(code)

    if not plan.safe_to_apply:
        payload["exit_code"] = _EXIT_PLAN_UNSOUND
        payload["apply_refused"] = f"plan is not safe to apply: {plan.summary()}"
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(_EXIT_PLAN_UNSOUND)
        console.print("[bold]sac agents migrate-engines[/bold]  apply\n")
        _show_preflight(payload)
        render_plan(plan, payload, diff=diff)
        console.print(
            "\n[red]REFUSED[/red] — nothing was written. A plan that cannot "
            "describe every spec does not describe the sweep."
        )
        raise SystemExit(_EXIT_PLAN_UNSOUND)

    result = apply_engines_migration(plan, _archive_dir())
    code = _EXIT_OK if result.applied else _EXIT_APPLY_REFUSED
    payload.update(
        {
            "written": list(result.written),
            "written_paths": list(result.written_paths),
            "archive_dir": str(result.archive_dir) if result.archive_dir else None,
            "applied": result.applied,
            "apply_refused": result.refused,
            "rolled_back": result.rolled_back,
            "drift": list(result.drift),
            "errors": list(result.errors),
            "migration_complete": _complete_after_apply(plan, result),
            # Re-derived AFTER the write, from the same source as the boolean
            # above: a successful apply retires the `still to write` line and
            # nothing else, and the prose must not keep claiming it.
            "outstanding": list(plan.remaining(migrated_written=result.applied)),
            "exit_code": code,
        }
    )
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)
    console.print("[bold]sac agents migrate-engines[/bold]  apply\n")
    _show_preflight(payload)
    if diff:
        # `--diff` is on by DEFAULT and its help calls it "the whole point".
        # It used to be accepted and dropped on this path, so an operator who
        # believed they were reviewing what was written saw nothing at all.
        render_diffs(plan)
        console.print("")
    render_apply(result, payload)
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``migrate-engines`` to the parent ``agents`` Click group."""
    agent_group.add_command(migrate_engines)


__all__ = ["migrate_engines", "register"]
