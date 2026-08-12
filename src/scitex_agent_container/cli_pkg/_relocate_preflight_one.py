"""Preflight ONE agent: read its spec, probe its future host, decide. No printing.

Split out of :mod:`_relocate_cmd` when the command learned to take several agent
names. That is the whole reason this is a separate module and it is worth stating
plainly: nine agents are queued for relocation, and learning the shape of the work
one agent at a time costs a round trip to another machine per agent. A sweep needs
the per-agent answer as a VALUE it can collect — not as something printed to a
console halfway down a click command.

So nothing here prints, and nothing here exits. A spec that cannot be found is a
:class:`Prepared` carrying an ``error``, not a ``SystemExit``: in a sweep, one
unreadable spec must not abort the other eight. The caller decides what an error
means, which for a single agent is still exit 2.

READ-ONLY, AND THAT IS A CONSTRAINT NOT AN ACCIDENT. Preflight touches nothing on
either host — it reads a spec file, reads the state db, and runs one batched
read-only probe over ssh. Nothing here may create, move, or modify anything; the
moment it does, "just check whether this would work" stops being safe to run
against nine live agents.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .._lifecycle._relocate_preflight import PreflightReport, preflight
from .._lifecycle._relocate_probe import gather_target_facts
from .._lifecycle._relocate_probe_adapter import (
    build_target_probes,
    card_store_url_from_spec,
)
from .._lifecycle._relocate_spec_reads import declared_groups_from_spec

__all__ = [
    "Prepared",
    "declared_from_spec",
    "prepare_one",
    "required_ports",
]


@dataclass
class Prepared:
    """One agent's preflight, as data. ``error`` and ``report`` are exclusive."""

    name: str
    to_host: str
    #: Non-empty when this agent could not be preflighted at all (no spec, or it
    #: is already recorded on the target). The sweep prints it and moves on.
    error: str = ""
    #: True when ``error`` means "there is nothing to do", not "something broke".
    already_there: bool = False
    spec: dict = field(default_factory=dict)
    spec_path: str = ""
    declared: dict = field(default_factory=dict)
    from_host: str = ""
    workdir: str = ""
    notices: tuple[str, ...] = ()
    report: PreflightReport | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def blocks(self) -> bool:
        """Refused? An error or a missing report blocks as firmly as a failed check."""
        if self.already_there:
            return False
        if self.error or self.report is None:
            return True
        return self.report.blocks


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
    dict would put a machine-owned fact under a heading that reads "from the spec
    — not verified by this run", which is the same collapse that makes `sac agents
    list` report a running agent as `defined`.

    Reads defensively: a missing key yields ``None``, rendered as ``(unset)``. A
    relocation must be able to report on a half-written spec — refusing to print
    because a field is absent would hide the very thing the operator needs.

    Binds and image live under ``spec.apptainer``, not at the top of ``spec``.
    Measured 2026-08-08, a top-level ``binds`` lookup reported "(none)" for a spec
    carrying nineteen of them — and "no binds" is exactly the answer that makes
    the bind check look satisfied when it was never asked.
    """
    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    binds = _dig(body, "apptainer", "binds") or []
    bind_sources = tuple(
        b.split(":", 1)[0] if isinstance(b, str) else str(b)
        for b in (binds if isinstance(binds, list) else [])
    )
    # Read through the SAME resolver the probe uses, so DECLARED and OBSERVED
    # cannot disagree about which store this agent has.
    return {
        "runtime": body.get("runtime"),
        "image": _dig(body, "apptainer", "image"),
        "a2a port": _dig(body, "a2a", "port"),
        "workdir": body.get("workdir"),
        "groups": declared_groups_from_spec(spec),
        "bind sources": bind_sources,
        "card store": card_store_url_from_spec(spec) or None,
    }


def required_ports(declared: dict[str, object]) -> tuple[int, ...]:
    """The ports preflight must find free — only when the spec PINS one.

    ``a2a.port: auto`` is not a requirement, it is a deferral: sac picks a free
    port at boot. Coercing it into a number here would invent a requirement the
    spec never made, and then fail the relocation on a clash that cannot happen.
    """
    port = declared.get("a2a port")
    if isinstance(port, bool):
        return ()
    if isinstance(port, int):
        return (port,)
    if isinstance(port, str) and port.isdigit():
        return (int(port),)
    return ()


def _source_repo_facts(spec_body: dict):
    """Scan the agent's workdir for un-saved work, or report that nobody looked.

    ONLY the workdir, and only when it is a git repo. The alternative — walk and
    guess at every repo an agent might touch — would produce a check whose PASS
    means "the repos I happened to think of are clean", which is worse than the
    UNKNOWN it replaces because it reads as an answer.

    A workdir that is not a repo yields an OBSERVED empty scan: there is nothing
    to strand, which is a real answer and passes. That is deliberately different
    from passing no facts at all, which is "nobody looked" and refuses.
    """
    from .._lifecycle._relocate_source_scan import scan_source

    workdir = str(spec_body.get("workdir") or "").strip()
    if not workdir or not (Path(workdir) / ".git").exists():
        return scan_source(())
    return scan_source((workdir,))


def source_facts(spec: dict, spec_body: dict, agent: str):
    """The repo scan PLUS which conversation would travel, both read locally.

    ``session_resolvable`` is a preflight check for one reason: the phase that
    needs the answer runs after the agent has been stopped. Ten agents on
    ywata-note-win passed every check on 2026-08-12 and could not complete,
    because each held more than one transcript and the transport only named a
    session when there was exactly one.

    The workdir is resolved against THIS filesystem, which is right precisely
    because these are the SOURCE's facts and the coordinator is standing on the
    source. When it is not, the directory is simply not there and ``scan_session``
    reports NOT OBSERVED rather than inventing an empty one.
    """
    from .._lifecycle._relocate_source_scan import scan_session
    from .._lifecycle._relocate_transcript_home import transcript_home_from_spec
    from .._lifecycle._relocate_transport_paths import derive_target_dir

    facts = _source_repo_facts(spec_body)
    home = transcript_home_from_spec(spec)
    workdir = str(spec_body.get("workdir") or "").strip()
    transcript_dir = ""
    if home.path and workdir:
        derived = derive_target_dir(
            target_home=home.path,
            target_resolved_workdir=str(Path(workdir).resolve()),
        )
        transcript_dir = derived.path or ""
    state_dir = str(Path.home() / ".scitex" / "agent-container" / "runtime" / agent)
    return scan_session(facts, transcript_dir=transcript_dir, state_dir=state_dir)


def _residency_history(name: str):
    """The agent's stays, read from the STATE DB — the only authority on host.

    THERE IS NO RESIDENCY TABLE YET, and pretending otherwise would be the worse
    of the two available lies. What exists is ``instances.host``, which
    ``record_instance_start`` canonicalises and writes when a process starts — an
    observation, and the right kind of one. So an active instance row becomes a
    single OPEN stay and that is the whole history.
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

    An unparseable stamp yields ``0.0`` rather than raising. Safe HERE and only
    here: the stay is open, so ``current_host`` reads the host and never consults
    the start time, and a relocation must not be blocked by a malformed timestamp
    on a row whose host is perfectly legible.
    """
    if not isinstance(started_at, str) or not started_at.strip():
        return 0.0
    from datetime import datetime

    try:
        return datetime.fromisoformat(started_at.strip()).timestamp()
    except ValueError:  # stx-allow: fallback (reason: an open stay is read for its HOST; a malformed start time must not block a relocation whose host is legible)
        return 0.0


def prepare_one(name: str, to_host: str) -> Prepared:
    """Read the spec, probe ``to_host`` once, and return the whole answer.

    ONE batched ssh round trip answers every target fact; each is parsed on its
    own marker line, so a section that fails costs only its own fact.
    """
    import yaml

    from .._lifecycle._relocate_host_record import (
        legacy_spec_host_notice,
        resolve_host,
    )
    from ._helpers._agent_list_discover import _discover_defined_agents

    out = Prepared(name=name, to_host=to_host)
    spec_path = dict(_discover_defined_agents()).get(name)
    if spec_path is None:
        out.error = (
            f"no spec found for agent {name!r}. Looked for <agents>/<name>/spec.yaml "
            "under the user (and project) scope."
        )
        return out

    # The RAW yaml, not the parsed AgentConfig. DECLARED must show what the spec
    # literally says: `AgentConfig` fills in defaults (runtime defaults to "tui",
    # for one), and a default printed under a heading marked DECLARED would be a
    # claim the operator never made.
    spec = yaml.safe_load(spec_path.read_text()) or {}
    body = spec.get("spec") if isinstance(spec.get("spec"), dict) else spec
    body = body if isinstance(body, dict) else {}
    out.spec = spec
    out.spec_path = str(spec_path)
    out.declared = declared_from_spec(spec)
    out.workdir = str(body.get("workdir") or "").strip()

    # WHERE IT RUNS NOW comes from the STATE DB, never from the spec. The spec's
    # `host:` is a legacy field: read at most ONCE, to seed a db that knows
    # nothing, and ignored from then on.
    legacy_host = body.get("host")
    where = resolve_host(
        _residency_history(name),
        legacy_spec_host=legacy_host if isinstance(legacy_host, str) else None,
        now=time.time(),
    )
    out.from_host = where.host or ""
    notice = legacy_spec_host_notice(
        spec_host=legacy_host if isinstance(legacy_host, str) else None,
        db_host=where.host,
    )
    if notice:
        out.notices = (notice,)
    if where.host == to_host:
        out.already_there = True
        out.error = (
            f"{name} is already recorded on {to_host!r} — nothing to relocate. "
            f"Source of that answer: {where.reason}"
        )
        return out

    ports = required_ports(out.declared)
    probes, _batch = build_target_probes(to_host, spec, required_ports=ports)
    gathered = gather_target_facts(probes)
    out.errors = gathered.errors
    out.report = preflight(
        agent=name,
        to_host=to_host,
        facts=gathered.facts,
        runtime=str(out.declared.get("runtime") or ""),
        required_ports=ports,
        source_facts=source_facts(spec, body, name),
        from_host=out.from_host,
        workdir=out.workdir,
        declared_groups=declared_groups_from_spec(spec),
    )
    return out
