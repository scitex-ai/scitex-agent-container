"""What ``sac agents migrate-engines`` PRINTS, and the payload behind it.

Split out of the command module, which had outgrown its line budget. The
command decides and writes; this decides what a human and a scheduled runner
are told, and those are the two places this sweep has been wrong in ways no
exit code showed:

* an apply that wrote nothing printed "this is what a completed one looks
  like" over a fleet where every spec had been REFUSED, and counted the
  SELECTED specs as proof. ``migration_complete`` is now the claim, and it
  is about the migration rather than about this invocation;
* the human path carried the refusals only in ``--json``, so the readable
  output was clean over a fleet it could not migrate;
* the success line never named the root it had written into — and the
  default root is the untracked live copy, which inside a container is not
  even the host's.
"""

from __future__ import annotations

from rich.markup import escape

from ._helpers import console

__all__ = [
    "plan_payload",
    "render_diffs",
    "preflight_payload",
    "render_apply",
    "render_plan",
    "render_preflight",
]


def _lit(text: str) -> str:
    """Escape before printing through rich.

    Not habit — MEASURED on the sibling sweep: a value rendering as
    ``[user-shared]`` is parsed by rich as a style tag and SWALLOWED, so a
    report printed a correct histogram with every row blank. Engine keys and
    model ids here include ``opus[1m]``, which is exactly that shape.
    """
    return escape(str(text))


def preflight_payload() -> dict:
    """Probe the gateway the migration writes into every spec.

    ``/v1/models``, NOT the base. The base answers 404 — measured — and a
    preflight that dials it reports "something is listening" from a path the
    gateway does not serve. Both addresses are in the payload so a reader can
    see which one was dialled rather than inferring it.
    """
    from ..config._engine_reach import reach_verdict
    from ..config._qwen_gateway import (
        QWEN_GATEWAY_PROBE_PATH,
        QWEN_GATEWAY_PROVIDER,
        qwen_gateway_probe_url,
        qwen_gateway_url,
    )

    url = qwen_gateway_probe_url()
    verdict = reach_verdict(url)
    return {
        "provider": QWEN_GATEWAY_PROVIDER,
        "url": url,
        "base_url": qwen_gateway_url(),
        "probe_path": QWEN_GATEWAY_PROBE_PATH,
        "state": verdict.state,
        "detail": verdict.detail,
        "http_status": verdict.http_status,
        "proves_listening": verdict.proves_listening,
        # The load-bearing one: is the INFERENCE API served at that address?
        # ``proves_listening`` is true of a 404 too, and a 404 is what the
        # base returns, so a report keying on it goes green on no evidence.
        "serves_endpoint": verdict.serves_endpoint,
        "proves_absent": verdict.proves_absent,
        "undetermined": verdict.undetermined,
    }


def render_preflight(payload: dict) -> None:
    if payload["serves_endpoint"]:
        colour = "green"
    elif payload["proves_absent"]:
        colour = "red"
    else:
        # Undetermined AND listening-wrong-path land here. Neither is a
        # negative and neither is evidence the API is there.
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


def plan_payload(plan, root) -> dict:
    """``root`` is one root or several — the report names every one of them.

    The default sweep searches EVERY user-scope root, so a payload carrying
    the first of them would be a claim about a directory the sweep did not
    confine itself to. ``roots`` is every root RESOLVED, in order;
    ``roots_absent`` is the subset that is not a directory, named rather than
    quietly dropped — an operator whose tree was resolved and found missing
    has to be able to see that, and it is the same distinction
    :mod:`..._maintenance._roster_state` draws between "empty" and "absent".
    ``root`` stays the human line and names them all.
    """
    from pathlib import Path

    given = list(root) if isinstance(root, (list, tuple)) else [root]
    roots = [str(r) for r in given]
    absent = [str(r) for r in given if not Path(r).is_dir()]
    return {
        "root": ", ".join(roots),
        "roots": roots,
        "roots_absent": absent,
        "roster": plan.roster.state if plan.roster else None,
        "specs": len(plan.outcomes),
        "would_migrate": len(plan.migrated),
        "already_migrated": [o.agent for o in plan.already],
        # Migratable, and past --limit. Named so a batch is never mistaken
        # for a completed sweep — see ``migration_complete`` below.
        "held_back": [o.agent for o in plan.held_back],
        "refused": [
            {"agent": o.agent, "reason": o.reason, "detail": o.detail}
            for o in plan.refused
        ],
        "unreadable": [{"agent": o.agent, "detail": o.detail} for o in plan.unreadable],
        "skipped_templates": list(plan.skipped_templates),
        "engine_sets": _engine_histogram(plan),
        "safe_to_apply": plan.safe_to_apply,
        # THE QUESTION A SCHEDULED RUNNER IS ACTUALLY ASKING, and neither
        # ``exit_code`` nor ``applied`` answers it: both are 0/true for a run
        # that wrote nothing because every spec was refused or held back.
        "migration_complete": plan.is_complete,
        "summary": plan.summary(),
    }


def _engine_histogram(plan) -> "dict[str, int]":
    hist: dict[str, int] = {}
    for outcome in plan.migrated:
        key = ", ".join(outcome.engine_keys)
        hist[key] = hist.get(key, 0) + 1
    return dict(sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))


def render_plan(plan, payload: dict, *, diff: bool) -> None:
    if plan.roster is not None and not plan.roster.is_populated:
        console.print(f"[red]NO ROSTER SEARCHED[/red] — {_lit(plan.roster.describe())}")
        return
    console.print(
        f"[bold]{payload['specs']} spec(s)[/bold] under {_lit(payload['root'])} — "
        f"{payload['would_migrate']} would gain a spec.engines block\n"
    )
    for keys, count in payload["engine_sets"].items():
        console.print(f"  [green]{count:4d}[/green]  engines: {_lit(keys)}")
    if payload.get("roots_absent"):
        # A resolved root that is not there was NOT searched. Dropping it from
        # the report would let an operator whose tree is missing read the
        # count as covering it.
        console.print(
            f"[yellow]NOT SEARCHED[/yellow] "
            f"{len(payload['roots_absent'])} resolved root(s) do not exist "
            f"({_lit(', '.join(payload['roots_absent']))}).\n",
            soft_wrap=True,
        )
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
    if payload["held_back"]:
        console.print(
            f"\n[cyan]HELD BACK[/cyan] {len(payload['held_back'])} spec(s) past "
            f"--limit ({_lit(', '.join(payload['held_back']))}). Run the same "
            f"command again to take the next batch.",
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


def _render_unfinished(payload: dict) -> None:
    """Name everything a further run still has to do. Never a bare count."""
    for entry in payload["refused"]:
        console.print(
            f"  [yellow]REFUSED[/yellow] {_lit(entry['agent'])}: "
            f"{_lit(entry['reason'])}",
            soft_wrap=True,
        )
    if payload["held_back"]:
        console.print(
            f"  [cyan]HELD BACK[/cyan] {len(payload['held_back'])} past --limit "
            f"({_lit(', '.join(payload['held_back']))})",
            soft_wrap=True,
        )


def render_apply(result, payload: dict) -> None:
    if result.applied and not result.written:
        # THE CLAIM IS ABOUT THE MIGRATION, so it is made only when the
        # migration is finished. "Nothing was written" is true of a completed
        # sweep AND of a run whose every spec was refused, and of a --limit
        # batch that already took its N — and the earlier form of this
        # sentence counted the SELECTED specs, so it called both of those
        # completed. A scheduled runner reading exit 0 believed it.
        if not payload["migration_complete"]:
            console.print(
                f"[yellow]Nothing was written[/yellow] — "
                f"{len(payload['already_migrated'])} of {payload['specs']} "
                f"spec(s) under {_lit(payload['root'])} declare spec.engines. "
                f"The sweep is NOT complete:",
                soft_wrap=True,
            )
            _render_unfinished(payload)
            return
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
            f"verified under {_lit(payload['root'])} — every one still resolves "
            f"the SAME backend.\n"
            f"  [dim]originals archived at {_lit(result.archive_dir)}[/dim]"
        )
        if not payload["migration_complete"]:
            console.print("\n[bold]Still outstanding[/bold] — run again:")
            _render_unfinished(payload)
        return
    if result.rolled_back:
        console.print(
            f"[red]ROLLED BACK[/red] — {_lit(result.rolled_back)}", soft_wrap=True
        )
        for entry in (*result.drift, *result.errors):
            console.print(f"    [magenta]{_lit(entry)}[/magenta]", soft_wrap=True)
        return
    console.print(
        f"[red]REFUSED[/red] — nothing was written.\n  {_lit(result.refused)}",
        soft_wrap=True,
    )


def render_diffs(plan) -> None:
    """One unified diff per spec about to be written.

    Used by BOTH the dry-run and the apply. ``--diff`` is on by default and
    its own help calls it "the whole point"; the apply path used to accept
    the flag and print nothing, so an operator who believed they were
    reviewing the rewrite saw a one-line summary and no diff at all.
    """
    for outcome in plan.migrated:
        console.print(f"\n[bold]{_lit(outcome.agent)}[/bold]")
        console.print(_lit(outcome.diff), soft_wrap=True, highlight=False)
