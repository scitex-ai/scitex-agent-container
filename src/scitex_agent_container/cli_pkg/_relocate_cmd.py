"""``sac agents relocate <name> --to <host>`` — the dry run, and nothing else yet.

Relocation moves an agent to a different host. The AGENT relocates; the host
does not move, and the agent's identity and count are unchanged (1 -> 1). It is
not a kind of fork/twin, which changes WHAT an agent does and takes the count to
two — the two verbs share one implementation detail (seeding a session from an
existing transcript) and nothing else.

WHY THE DRY RUN SHIPS FIRST, ALONE. On 2026-08-07 this move was done by hand.
Rewriting `host:` alone produced an agent that STARTED, reported HEALTHY, and
did nothing — the worst failure shape there is, because it looks like success
and nobody goes looking. Every check in :mod:`.._lifecycle._relocate_preflight`
exists because of something that went wrong that day. Shipping the check before
the move means the next hand-move is already safer, even with no executing path.

THE EXECUTING PATH REFUSES rather than half-working. `--dry-run` is currently
required, and omitting it exits non-zero naming what is missing (the cross-host
transcript transport). A verb that silently does part of a relocation is exactly
the "looks like success" failure this command was written to prevent.

WHAT COMES FROM WHERE — the operator's requirement, 2026-08-08:
「定義されているのと、今動いているのって違うんで」

    DECLARED   read from the spec. Claims, printed as claims.
    OBSERVED   probed on the target. Evidence, printed separately.

Collapsing those into one column is how `sac agents list` reports a running
agent as `defined`, and this command must not repeat it.

UNPROBED IS UNKNOWN, AND UNKNOWN REFUSES. Any fact a probe could not answer
stays ``None``, which preflight reports as UNKNOWN and refuses on, exactly as it
would for a probe that ran and failed. That is deliberate: from the decision's
point of view "nobody asked" and "asked and could not tell" are the same thing,
and both differ from an answer. A dry run that cannot measure something is not a
green light — it is a list of what still has to be measured, each entry carrying
the reason it could not be.

THE PROBES ARE ONE ssh ROUND TRIP, NOT ELEVEN. See
:mod:`.._lifecycle._relocate_probe_adapter`: the batch is deliberately built so
that a partial answer degrades PER FACT — eight measured and three unknown,
never eleven of either because one section failed.
"""

from __future__ import annotations

import time

import click

from .._lifecycle._relocate_preflight import preflight
from .._lifecycle._relocate_probe import gather_target_facts
from .._lifecycle._relocate_probe_adapter import (
    build_target_probes,
    card_store_url_from_spec,
)
from .._lifecycle._relocate_render import render_dry_run
from ._helpers import console

__all__ = ["declared_from_spec", "register", "relocate"]

#: Exit code when the dry run refuses (a failed or undetermined check). Distinct
#: from click's own 2 (usage error) so a caller can tell "you asked wrongly"
#: from "the target is not ready".
EXIT_REFUSED = 3
#: Exit code for the not-yet-built executing path. Distinct again, so a script
#: cannot mistake "unimplemented" for "the target failed preflight".
EXIT_UNIMPLEMENTED = 4


def _dig(body: dict, *path: str) -> object:
    """Follow ``path`` through nested dicts, yielding ``None`` at any break."""
    cur: object = body
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def declared_from_spec(spec: dict) -> dict[str, object]:
    """Pull the spec's own claims out for the DECLARED section.

    ``host`` IS DELIBERATELY NOT HERE. It is an observation, not a declaration
    (operator, 2026-08-11: 「設定ファイル、人が書くものはファイル、状態は db」), so
    it comes from the state db and is printed under OBSERVED. Leaving it in this
    dict would put a machine-owned fact under a heading that reads "from the
    spec — not verified by this run", which is the same collapse that makes
    `sac agents list` report a running agent as `defined`. The legacy field
    still present in every spec on disk is reported by
    :func:`.._lifecycle._relocate_host_record.legacy_spec_host_notice`, which
    says out loud that it is ignored.

    Reads defensively: a missing key yields ``None``, rendered as ``(unset)``. A
    relocation must be able to report on a half-written spec — refusing to print
    because a field is absent would hide the very thing the operator needs.

    Binds and image live under ``spec.apptainer``, not at the top of ``spec``.
    Reading them from the wrong level is the mistake this function exists to
    make once, here, instead of at every call site: measured 2026-08-08, a
    top-level ``binds`` lookup reported "(none)" for a spec carrying nineteen of
    them — and "no binds" is exactly the answer that makes the `/mnt/c` check
    look satisfied when it was never asked.
    """
    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    binds = _dig(body, "apptainer", "binds") or []
    bind_sources = tuple(
        b.split(":", 1)[0] if isinstance(b, str) else str(b)
        for b in (binds if isinstance(binds, list) else [])
    )
    # Read through the SAME resolver the probe uses, so DECLARED and OBSERVED
    # cannot disagree about which store this agent has. An ``apptainer.env``-only
    # lookup printed "(unset)" for this repo's own spec — which carries the URL
    # in a ``--env`` raw arg — while the probe went and successfully dialled it:
    # two sections of one report describing the same agent differently.
    card_store = card_store_url_from_spec(spec) or None
    return {
        "runtime": body.get("runtime"),
        "image": _dig(body, "apptainer", "image"),
        "a2a port": _dig(body, "a2a", "port"),
        "bind sources": bind_sources,
        "card store": card_store,
    }


def _residency_history(name: str):
    """The agent's stays, read from the STATE DB — the only authority on host.

    THERE IS NO RESIDENCY TABLE YET, and pretending otherwise would be the worse
    of the two available lies. What exists is ``instances.host``, which
    ``record_instance_start`` canonicalises and writes when a process starts —
    an observation, and the right kind of one. So an active instance row becomes
    a single OPEN stay and that is the whole history.

    The cost is stated rather than hidden: with one row there is no audit trail,
    so ``host_at(history, t)`` can answer "where does it live now" and cannot
    answer "which host wrote this row in March". Closing that needs a residency
    table; until then this returns the shortest history that is TRUE rather than
    a longer one that is invented.

    No row yields ``()`` — genuinely "the db knows nothing", which is what lets
    a legacy spec ``host:`` seed it once.
    """
    from .._lifecycle._residency import Residency
    from .._state.state_db_instances import list_active_instances

    rows = [r for r in list_active_instances() if r.get("name") == name]
    if not rows:
        return ()
    row = rows[0]
    host = (row.get("host") or "").strip()
    if not host:
        return ()
    return (Residency(host=host, from_ts=_epoch(row.get("started_at"))),)


def _epoch(started_at: object) -> float:
    """``instances.started_at`` is an ISO TEXT column; residency wants seconds.

    An unparseable stamp yields ``0.0`` rather than raising. That is safe HERE
    and only here: the stay is open, so ``current_host`` reads the host and
    never consults the start time, and a relocation must not be blocked by a
    malformed timestamp on a row whose host is perfectly legible. It would NOT
    be safe for the attribution lookup, which is one more reason that needs a
    real residency table rather than this row.
    """
    if not isinstance(started_at, str) or not started_at.strip():
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(started_at.strip()).timestamp()
    except ValueError:  # stx-allow: fallback (reason: an open stay is read for its HOST; a malformed start time must not block a relocation whose host is legible)
        return 0.0


def _required_ports(declared: dict[str, object]) -> tuple[int, ...]:
    """The ports preflight must find free — only when the spec PINS one.

    ``a2a.port: auto`` is not a requirement, it is a deferral: sac picks a free
    port at boot. Coercing it into a number here would invent a requirement the
    spec never made, and then fail the relocation on a port clash that cannot
    happen.
    """
    port = declared.get("a2a port")
    if isinstance(port, bool):
        return ()
    if isinstance(port, int):
        return (port,)
    if isinstance(port, str) and port.isdigit():
        return (int(port),)
    return ()


@click.command("relocate")
@click.argument("name", required=True)
@click.option("--to", "to_host", required=True, help="Target host to relocate ONTO.")
@click.option(
    "--dry-run/--no-dry-run",
    default=True,
    show_default=True,
    help="Check the target and touch nothing. Currently the only supported mode.",
)
def relocate(name: str, to_host: str, dry_run: bool) -> None:
    """Relocate agent NAME onto --to HOST. The AGENT moves, not the host.

    \b
    Example:
      $ sac agents relocate scitex-dev --to scitex-compute-03 --dry-run

    The dry run reports EVERY problem in one pass rather than stopping at the
    first, so you do not have to run it N times to find N problems. It answers
    in three values, never two: PASS, FAIL, and UNKNOWN. An UNKNOWN refuses just
    as firmly as a FAIL, but calls for a different action — go and measure it,
    rather than go and fix it.
    """
    # Resolve the spec from DISK, not from the instance registry. Measured
    # 2026-08-08 while building this command: `Registry().get("scitex-agent-
    # container")` returned None from inside a container — for the very agent
    # running the query — because the registry it reads is the container's own
    # private one. The spec files are bind-visible and the registry is not, so
    # the DECLARED half of this report must come from the files.
    import yaml

    from ._helpers._agent_list_discover import _discover_defined_agents

    spec_path = dict(_discover_defined_agents()).get(name)
    if spec_path is None:
        console.print(
            f"[red]no spec found for agent {name!r}[/red]\n"
            "Looked for <agents>/<name>/spec.yaml under the user (and project) scope."
        )
        raise SystemExit(2)
    # The RAW yaml, not the parsed AgentConfig. DECLARED must show what the spec
    # literally says: `AgentConfig` fills in defaults (runtime defaults to "tui",
    # for one), and a default printed under a heading marked DECLARED would be a
    # claim the operator never made.
    spec = yaml.safe_load(spec_path.read_text()) or {}

    declared = declared_from_spec(spec)

    # WHERE IT RUNS NOW comes from the STATE DB, never from the spec. The spec's
    # `host:` is a legacy field: it is read at most ONCE, to seed a db that knows
    # nothing, and is ignored from then on. Reading it here instead would make
    # this command answer "where does it run" from a hand-written file that
    # exists in one copy per machine — the confusion the 2026-08-11 ruling
    # removed (「設定ファイル、人が書くものはファイル、状態は db」).
    from .._lifecycle._relocate_host_record import (
        legacy_spec_host_notice,
        resolve_host,
    )

    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    legacy_host = body.get("host") if isinstance(body, dict) else None
    where = resolve_host(
        _residency_history(name),
        legacy_spec_host=legacy_host if isinstance(legacy_host, str) else None,
        now=time.time(),
    )
    notice = legacy_spec_host_notice(
        spec_host=legacy_host if isinstance(legacy_host, str) else None,
        db_host=where.host,
    )
    if notice:
        console.print(f"[yellow]note:[/yellow] {notice}", soft_wrap=True)
    if where.host == to_host:
        console.print(
            f"[yellow]{name} is already recorded on {to_host!r} — nothing to relocate.[/yellow]\n"
            f"Source of that answer: {where.reason}"
        )
        raise SystemExit(EXIT_REFUSED)

    # ONE batched ssh round trip answers all thirteen facts; each is parsed on
    # its own marker line, so a section that fails costs only its own fact. See
    # `.._lifecycle._relocate_probe_adapter` for how per-fact degradation
    # survives the batching.
    probes, _batch = build_target_probes(
        to_host, spec, required_ports=_required_ports(declared)
    )
    gathered = gather_target_facts(probes)
    report = preflight(
        agent=name,
        to_host=to_host,
        facts=gathered.facts,
        runtime=str(declared.get("runtime") or ""),
        required_ports=_required_ports(declared),
    )

    for line in render_dry_run(report, declared=declared, errors=gathered.errors):
        console.print(line, soft_wrap=True)

    if not dry_run:
        console.print(
            "\n[red]refusing to execute:[/red] the cross-host transcript transport "
            "is not built, so a relocation would arrive with no memory of the "
            "conversation that moved it. See docs/relocate.md §5."
        )
        raise SystemExit(EXIT_UNIMPLEMENTED)
    if report.ok is not True:
        raise SystemExit(EXIT_REFUSED)


def register(agent_group) -> None:
    """Attach ``relocate`` to the parent ``agents`` Click group."""
    agent_group.add_command(relocate)
