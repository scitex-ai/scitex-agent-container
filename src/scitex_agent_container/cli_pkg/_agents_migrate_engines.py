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
  * **Batches.** ``--agent`` names an explicit set, ``--host`` takes one
    machine at a time, ``--limit`` caps whatever those selected. Nobody has
    to rewrite 119 files to rewrite one.
  * **A measured gate on the apply.** Every selected spec is loaded through
    the production loader before the write and again after; unless the
    effective backend is identical for every one, every original is restored
    from the archive taken first.

An unmigratable spec is REFUSED BY NAME with its reason, never skipped —
skipping is how a sweep reports 118 done over a fleet of 119.

**Exit codes.** ``0`` the plan is sound (a named refusal is NOT a failure),
``1`` a spec is unreadable or no roster was searched, ``2`` the apply was
refused or rolled back.

``--preflight`` probes the gateway the migration points at and reports a
NAMED state rather than a boolean, because the two failure shapes look
identical through curl: ``scitex-compute-04:18772`` answers 401 (reachable
and auth-gating) while ``compute-04:18772`` answers ``000`` — which reads as
"the gateway is down" and means "the hostname does not resolve". See
:mod:`...config._engine_reach`.
"""

from __future__ import annotations

import datetime as _dt
import json

import click
from rich.markup import escape

from .._maintenance._engines_migration import (
    apply_engines_migration,
    plan_engines_migration,
    select_spec_paths,
)
from ._helpers import _json_flag, console

_EXIT_OK = 0
_EXIT_PLAN_UNSOUND = 1
_EXIT_APPLY_REFUSED = 2


def _lit(text: str) -> str:
    """Escape before printing through rich.

    Not habit — MEASURED on the sibling sweep: a value rendering as
    ``[user-shared]`` is parsed by rich as a style tag and SWALLOWED, so a
    report printed a correct histogram with every row blank. Engine keys and
    model ids here include ``opus[1m]``, which is exactly that shape.
    """
    return escape(str(text))


def _archive_dir():
    from .._runtime_paths import runtime_base_dir

    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return runtime_base_dir() / "engines-migration" / stamp


def _preflight_payload() -> dict:
    """Probe the gateway the migration writes into every spec."""
    from ..config._engine_reach import reach_verdict
    from ..config._qwen_gateway import QWEN_GATEWAY_PROVIDER, qwen_gateway_url

    url = qwen_gateway_url()
    verdict = reach_verdict(url)
    return {
        "provider": QWEN_GATEWAY_PROVIDER,
        "url": url,
        "state": verdict.state,
        "detail": verdict.detail,
        "http_status": verdict.http_status,
        "proves_listening": verdict.proves_listening,
        "proves_absent": verdict.proves_absent,
        "undetermined": verdict.undetermined,
    }


def _render_preflight(payload: dict) -> None:
    colour = "green" if payload["proves_listening"] else "red"
    if payload["undetermined"]:
        colour = "yellow"
    console.print(
        f"[bold]gateway preflight[/bold] {_lit(payload['url'])} "
        f"([{colour}]{_lit(payload['state'])}[/{colour}])\n"
        f"  {_lit(payload['detail'])}",
        soft_wrap=True,
    )
    if payload["undetermined"]:
        console.print(
            "  [dim]UNDETERMINED is not a negative. Nothing here says the "
            "gateway is down.[/dim]",
            soft_wrap=True,
        )


def _plan_payload(plan, root) -> dict:
    return {
        "root": str(root),
        "roster": plan.roster.state if plan.roster else None,
        "specs": len(plan.outcomes),
        "would_migrate": len(plan.migrated),
        "already_migrated": [o.agent for o in plan.already],
        "refused": [
            {"agent": o.agent, "reason": o.reason, "detail": o.detail}
            for o in plan.refused
        ],
        "unreadable": [{"agent": o.agent, "detail": o.detail} for o in plan.unreadable],
        "skipped_templates": list(plan.skipped_templates),
        "engine_sets": _engine_histogram(plan),
        "safe_to_apply": plan.safe_to_apply,
        "summary": plan.summary(),
    }


def _engine_histogram(plan) -> "dict[str, int]":
    hist: dict[str, int] = {}
    for outcome in plan.migrated:
        key = ", ".join(outcome.engine_keys)
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def _render_plan(plan, payload: dict, *, diff: bool) -> None:
    if plan.roster is not None and not plan.roster.is_populated:
        console.print(f"[red]NO ROSTER SEARCHED[/red] — {_lit(plan.roster.describe())}")
        return
    console.print(
        f"[bold]{payload['specs']} spec(s)[/bold] under {_lit(payload['root'])} — "
        f"{payload['would_migrate']} would gain a spec.engines block\n"
    )
    for keys, count in payload["engine_sets"].items():
        console.print(f"  [green]{count:4d}[/green]  engines: {_lit(keys)}")
    if payload["already_migrated"]:
        console.print(
            f"\n[dim]{len(payload['already_migrated'])} spec(s) already declare "
            f"spec.engines — nothing to do for those.[/dim]"
        )
    for entry in payload["refused"]:
        console.print(
            f"\n[yellow]REFUSED[/yellow] {_lit(entry['agent'])}: "
            f"{_lit(entry['reason'])}",
            soft_wrap=True,
        )
        if entry["detail"]:
            console.print(f"    [dim]{_lit(entry['detail'])}[/dim]", soft_wrap=True)
    for entry in payload["unreadable"]:
        console.print(
            f"\n[red]UNREADABLE[/red] {_lit(entry['agent'])}: {_lit(entry['detail'])}",
            soft_wrap=True,
        )
    if payload["skipped_templates"]:
        # Named, never silent: `sac agents create` copies these, so a template
        # left behind re-introduces the legacy shape on every agent made after
        # the sweep — the migration would then never finish.
        console.print(
            f"\n[yellow]NOT SEARCHED[/yellow] "
            f"{len(payload['skipped_templates'])} template spec(s) "
            f"({_lit(', '.join(payload['skipped_templates']))}). "
            f"`sac agents create` copies them, so an unmigrated template "
            f"re-introduces the legacy shape on every new agent. Pass "
            f"--templates to include them.",
            soft_wrap=True,
        )
    if diff:
        for outcome in plan.migrated:
            console.print(f"\n[bold]{_lit(outcome.agent)}[/bold]")
            console.print(_lit(outcome.diff), soft_wrap=True, highlight=False)
    console.print(f"\n[bold]{_lit(payload['summary'])}[/bold]")


def _render_apply(result, payload: dict) -> None:
    if result.applied and not result.written:
        # Names the population it is making this claim about. The unqualified
        # form of this sentence was printed by a sibling sweep that had
        # discovered ZERO specs; an assertion that a migration is FINISHED is
        # the last place to omit what it counted.
        console.print(
            f"[green]Nothing to write[/green] — all "
            f"{payload['specs']} spec(s) under {_lit(payload['root'])} already "
            f"declare spec.engines. The sweep is idempotent; this is what a "
            f"completed one looks like."
        )
        return
    if result.applied:
        console.print(
            f"[green]APPLIED[/green] {len(result.written)} spec(s) written and "
            f"verified — every one still resolves the SAME backend.\n"
            f"  [dim]originals archived at {_lit(result.archive_dir)}[/dim]"
        )
        return
    if result.rolled_back:
        console.print(
            f"[red]ROLLED BACK[/red] — {_lit(result.rolled_back)}", soft_wrap=True
        )
        for entry in result.drift:
            console.print(f"    [magenta]{_lit(entry)}[/magenta]", soft_wrap=True)
        return
    console.print(
        f"[red]REFUSED[/red] — nothing was written.\n  {_lit(result.refused)}",
        soft_wrap=True,
    )


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
    help="Cap the batch at N specs, after --agent/--host have selected.",
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
    In batches:
      $ sac agents migrate-engines -a business -a handyman-02
      $ sac agents migrate-engines --host scitex-compute-04 --limit 5 --apply
    \b
    Gateway reachability, three-valued:
      $ sac agents migrate-engines --preflight --no-diff

    Exits 0 when the plan is sound (a named REFUSAL is not a failure), 1 when a
    spec is unreadable or no roster was searched, 2 when the apply was refused
    or rolled back.
    """
    if apply and dry_run:
        raise click.UsageError(
            "--apply and --dry-run are contradictory. Dry-run is the DEFAULT: "
            "drop both flags to preview, pass --apply to write."
        )

    from .._maintenance._layers_migration_plan import fleet_agents_dir

    root = fleet_agents_dir()
    paths, skipped = select_spec_paths(
        root, hosts=hosts, agents=agents, templates=templates, limit=limit
    )
    plan = plan_engines_migration(paths, root=root, skipped_templates=skipped)
    payload = _plan_payload(plan, root)
    payload["mode"] = "apply" if apply else "dry-run"
    payload["preflight"] = _preflight_payload() if preflight else None

    if not apply:
        code = _EXIT_OK if plan.safe_to_apply else _EXIT_PLAN_UNSOUND
        payload["exit_code"] = code
        if diff:
            payload["diffs"] = {o.agent: o.diff for o in plan.migrated}
        if _json_flag(ctx, as_json):
            click.echo(json.dumps(payload, indent=2))
            raise SystemExit(code)
        console.print("[bold]sac agents migrate-engines[/bold]  dry-run (read-only)\n")
        if payload["preflight"]:
            _render_preflight(payload["preflight"])
            console.print("")
        _render_plan(plan, payload, diff=diff)
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
        _render_plan(plan, payload, diff=False)
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
            "archive_dir": str(result.archive_dir) if result.archive_dir else None,
            "applied": result.applied,
            "apply_refused": result.refused,
            "rolled_back": result.rolled_back,
            "drift": list(result.drift),
            "exit_code": code,
        }
    )
    if _json_flag(ctx, as_json):
        click.echo(json.dumps(payload, indent=2))
        raise SystemExit(code)
    console.print("[bold]sac agents migrate-engines[/bold]  apply\n")
    _render_apply(result, payload)
    raise SystemExit(code)


def register(agent_group) -> None:
    """Attach ``migrate-engines`` to the parent ``agents`` Click group."""
    agent_group.add_command(migrate_engines)


__all__ = ["migrate_engines", "register"]
