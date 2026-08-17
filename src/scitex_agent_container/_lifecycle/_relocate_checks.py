"""The predicates, one per thing that went wrong when this was done by hand.

Every check here was learned by doing the move manually on 2026-08-07. None of
them is hypothetical: rewriting `host:` alone produced an agent that STARTED,
reported HEALTHY, and did nothing, which is the worst failure shape available
because it looks exactly like success.

    binds        the spec bound /mnt/c — a Windows drive absent on the nas
    card store   SCITEX_CARDS_DB is 5432 here and 5442 there
    credentials  the nas had a stale file (expired 2026-05-23, empty
                 refreshToken) that sac loaded IN PREFERENCE to the good one;
                 every turn 401'd while `sac agents health` still said healthy
    runtime      `tui` is rejected by the nas's older sac
    schema       a top-level `provider:` key is rejected by that same validator
    source work  uncommitted or unpushed work on the machine being LEFT — the
                 one check here that is about the source rather than the target
  + reachability, image presence, free ports, and whether the hub is reachable
    FROM the target (the nas's services bind 127.0.0.1, so "I can reach it" from
    here proves nothing about there)

CREDENTIALS IS THE ONE TO READ TWICE. Checking PRESENCE passes on an expired
file. The check has to be about VALIDITY, and the failure it prevents is silent.

TWO SIBLINGS HOLD THE REST, and they are separate files because each is one
story rather than one predicate: :mod:`_relocate_checks_sac` owns whether sac on
the target can be REACHED (three different PATHs, and which one the answer is
about), and :mod:`_relocate_checks_late` owns the two questions the PHASES used
to ask after the agent had already been stopped.

NO I/O. These evaluate FACTS someone else gathered — facts in, :class:`Check`s
out — so sac does not learn how to probe a host here, and every check is
unit-testable against the exact broken state we hit in production. That is the
only way to know a check would actually have caught it.
"""

from __future__ import annotations

from typing import Final

from ._relocate_bind_kind import classify_binds, group_by_action
from ._relocate_preflight_facts import Check, SourceFacts, TargetFacts
from ._relocate_session_choice import CODE_UNKNOWN as SESSION_UNKNOWN
from ._relocate_session_choice import choose_session

__all__ = [
    "CHECK_BINDS",
    "CHECK_CARD_STORE",
    "CHECK_CREDENTIALS",
    "CHECK_HUB_FROM_TARGET",
    "CHECK_IMAGE",
    "CHECK_PORTS",
    "CHECK_REACHABLE",
    "CHECK_RUNTIME",
    "CHECK_SCHEMA",
    "CHECK_SESSION",
    "CHECK_SOURCE_WORK",
    "check_binds",
    "check_card_store",
    "check_credentials",
    "check_hub_from_target",
    "check_image",
    "check_ports",
    "check_reachable",
    "check_runtime",
    "check_schema",
    "check_session_resolvable",
    "check_source_work",
]

CHECK_REACHABLE: Final = "target_reachable"
CHECK_IMAGE: Final = "image_present"
CHECK_BINDS: Final = "binds_exist_on_target"
CHECK_CARD_STORE: Final = "card_store_reachable"
CHECK_CREDENTIALS: Final = "credentials_valid"
CHECK_RUNTIME: Final = "runtime_supported"
CHECK_SCHEMA: Final = "spec_schema_accepted"
CHECK_PORTS: Final = "ports_free"
CHECK_HUB_FROM_TARGET: Final = "hub_reachable_from_target"
CHECK_SOURCE_WORK: Final = "source_work_committed"
CHECK_SESSION: Final = "session_resolvable"


def _unobserved(name: str, what: str) -> Check:
    return Check(
        name=name,
        ok=None,
        detail=f"{what} was not observed on the target",
        hint=(
            "run the probe that supplies this fact before deciding; an unobserved "
            "check is not a passing one, and proceeding on it is how a relocation "
            "reports healthy while doing nothing"
        ),
    )


def check_reachable(facts: TargetFacts, to_host: str) -> Check:
    if facts.reachable is None:
        return _unobserved(CHECK_REACHABLE, "reachability")
    if not facts.reachable:
        return Check(
            name=CHECK_REACHABLE,
            ok=False,
            detail=f"{to_host} did not answer",
            hint=f"check ssh/network to {to_host}; nothing else in this report is meaningful until it answers",
        )
    return Check(name=CHECK_REACHABLE, ok=True, detail=f"{to_host} answered")


def check_image(facts: TargetFacts, to_host: str) -> Check:
    if facts.image_present is None:
        return _unobserved(CHECK_IMAGE, "the agent image")
    if not facts.image_present:
        return Check(
            name=CHECK_IMAGE,
            ok=False,
            detail=f"the agent's image is absent on {to_host}",
            hint=f"build or copy the SIF to {to_host} before relocating; a missing image fails at boot, after the lease has moved",
        )
    return Check(name=CHECK_IMAGE, ok=True, detail="image present")


def check_binds(
    facts: TargetFacts, to_host: str, *, workdir: str = "", from_host: str = ""
) -> Check:
    """Missing binds are never ONE problem, and the hint must not pretend they are.

    The old hint said "remove or re-point these binds in the spec", which is the
    right instruction for a Windows drive on a NAS and the wrong one for the
    fifteen fleet specs measured on 2026-08-11: nine bind Spartan cluster storage
    that a workstation cannot provide at all, and six bind a dataset and a
    checkout that exist only on the laptop that made them. Printed as one list of
    absent paths those look identical; the actions are provision, carry, and
    (for anything under an account or key directory) provision-and-never-copy.

    ``workdir`` and ``from_host`` are what make the split possible — see
    :mod:`_relocate_bind_kind`. Without them every path falls to "unclassified",
    which states both possibilities rather than guessing.
    """
    if facts.missing_bind_sources is None:
        return _unobserved(CHECK_BINDS, "bind sources")
    if facts.missing_bind_sources:
        missing = ", ".join(facts.missing_bind_sources)
        classified = classify_binds(
            facts.missing_bind_sources, workdir=workdir, from_host=from_host
        )
        parts = [
            f"{action}: " + ", ".join(b.path for b in members)
            for action, members in group_by_action(classified)
        ]
        return Check(
            name=CHECK_BINDS,
            ok=False,
            detail=f"bind sources absent on {to_host}: {missing}",
            hint=(
                "these are not one problem — "
                + "; ".join(parts)
                + ". A path the host provides is provisioned on the target; a path the "
                "agent made exists only where it ran and must travel with it (a "
                "relocation carries the spec and the transcript and nothing else); a "
                "credential path is provisioned there and NEVER copied between hosts"
            ),
        )
    return Check(
        name=CHECK_BINDS, ok=True, detail="every bind source exists on the target"
    )


def check_card_store(facts: TargetFacts, to_host: str) -> Check:
    if facts.card_store_reachable is None:
        return _unobserved(CHECK_CARD_STORE, "the card store")
    if not facts.card_store_reachable:
        url = facts.card_store_url or "(no url recorded)"
        return Check(
            name=CHECK_CARD_STORE,
            ok=False,
            detail=f"card store {url} not reachable from {to_host}",
            hint=(
                "set SCITEX_CARDS_DB to the target's own store before relocating "
                "(2026-08-07: 5432 here, 5442 there) — an agent that cannot reach its "
                "board runs and records nothing"
            ),
        )
    return Check(
        name=CHECK_CARD_STORE, ok=True, detail=f"card store reachable from {to_host}"
    )


def check_credentials(facts: TargetFacts) -> Check:
    """VALIDITY, not presence. Presence passes on an expired file."""
    if (
        facts.credential_expires_in_s is None
        or facts.credential_refresh_token_present is None
    ):
        return _unobserved(CHECK_CREDENTIALS, "credential validity")
    if facts.credential_expires_in_s <= 0:
        return Check(
            name=CHECK_CREDENTIALS,
            ok=False,
            detail=f"target credential expired {abs(facts.credential_expires_in_s):.0f}s ago",
            hint=(
                "refresh or replace the target-local credential; sac loads it IN PREFERENCE "
                "to a good one, so every turn 401s while `sac agents health` still says healthy"
            ),
        )
    if not facts.credential_refresh_token_present:
        return Check(
            name=CHECK_CREDENTIALS,
            ok=False,
            detail="target credential has an empty refreshToken",
            hint=(
                "replace it — it is valid now and unrenewable, so the agent dies at the "
                "first refresh with no warning beforehand"
            ),
        )
    return Check(
        name=CHECK_CREDENTIALS,
        ok=True,
        detail=f"credential valid for {facts.credential_expires_in_s:.0f}s with a refresh token",
    )


def check_runtime(facts: TargetFacts, runtime: str) -> Check:
    if facts.supported_runtimes is None:
        return _unobserved(CHECK_RUNTIME, "supported runtimes")
    if runtime not in facts.supported_runtimes:
        supported = ", ".join(facts.supported_runtimes) or "(none reported)"
        return Check(
            name=CHECK_RUNTIME,
            ok=False,
            detail=f"target does not support runtime {runtime!r}; it supports: {supported}",
            hint=(
                "either upgrade sac on the target or set the spec's runtime to one it accepts "
                "(2026-08-07: the nas's sac 0.21.9 rejected 'tui')"
            ),
        )
    return Check(name=CHECK_RUNTIME, ok=True, detail=f"runtime {runtime!r} supported")


def check_schema(facts: TargetFacts) -> Check:
    if facts.rejected_spec_keys is None:
        return _unobserved(CHECK_SCHEMA, "spec-schema acceptance")
    if facts.rejected_spec_keys:
        keys = ", ".join(facts.rejected_spec_keys)
        return Check(
            name=CHECK_SCHEMA,
            ok=False,
            detail=f"target's validator rejects spec key(s): {keys}",
            hint=(
                "remove those keys for the target, or upgrade its sac "
                "(2026-08-07: a top-level 'provider:' key was rejected by the older validator)"
            ),
        )
    return Check(
        name=CHECK_SCHEMA, ok=True, detail="target's validator accepts the spec"
    )


def check_ports(facts: TargetFacts, required_ports: tuple[int, ...]) -> Check:
    if facts.ports_in_use is None:
        return _unobserved(CHECK_PORTS, "port availability")
    clashes = tuple(p for p in required_ports if p in facts.ports_in_use)
    if clashes:
        return Check(
            name=CHECK_PORTS,
            ok=False,
            detail=f"port(s) already in use on the target: {', '.join(str(p) for p in clashes)}",
            hint="free them or reassign the agent's ports in the spec before relocating",
        )
    return Check(
        name=CHECK_PORTS, ok=True, detail="required ports are free on the target"
    )


def check_hub_from_target(facts: TargetFacts, to_host: str) -> Check:
    if facts.hub_reachable_from_target is None:
        return _unobserved(CHECK_HUB_FROM_TARGET, "hub reachability FROM the target")
    if not facts.hub_reachable_from_target:
        return Check(
            name=CHECK_HUB_FROM_TARGET,
            ok=False,
            detail=f"the hub is not reachable from {to_host}",
            hint=(
                "check what the hub's services bind to — reaching them from HERE proves "
                "nothing about THERE (the nas binds 127.0.0.1, so nothing cross-host reaches it)"
            ),
        )
    return Check(
        name=CHECK_HUB_FROM_TARGET, ok=True, detail=f"hub reachable from {to_host}"
    )


def check_session_resolvable(source: SourceFacts, agent: str) -> Check:
    """Can the conversation to resume be NAMED, before anything is stopped?

    This check exists because the answer used to be discovered three phases too
    late. TARGET_STANDBY refuses without a session id, and TARGET_STANDBY runs
    after SOURCE_STOP — so an agent whose session could not be identified was
    taken down, had its transcript copied and verified, and then aborted with the
    marker unwritten and nothing running anywhere.

    Measured 2026-08-12 on ywata-note-win: ten agents, every one holding between
    two and five transcripts, every one reporting ``GO — every check passed``, and
    not one of them able to complete. The guard was ``len(files) == 1``. A check
    that passes on ten agents that cannot proceed is not a check; the gap is as
    much the bug as the guard was.
    """
    if source.transcripts is None:
        return Check(
            name=CHECK_SESSION,
            ok=None,
            detail=f"the transcripts for {agent} on the source were not listed",
            hint=(
                "list the source's project directory before deciding. Which session "
                "travels is settled here or it is settled after the agent has been "
                "stopped, and only one of those is recoverable without an operator"
            ),
        )
    choice = choose_session(
        agent=agent,
        carried=[name for name, _ in source.transcripts],
        marker=source.session_marker,
        mtimes=dict(source.transcripts),
    )
    if choice.session is not None:
        return Check(
            name=CHECK_SESSION,
            ok=True,
            detail=(
                f"session {choice.session} would travel, chosen by {choice.chosen_by} "
                f"from {len(choice.candidates)} candidate(s)"
            ),
        )
    return Check(
        name=CHECK_SESSION,
        ok=None if choice.code == SESSION_UNKNOWN else False,
        detail=f"{choice.reason} ({choice.code})",
        hint=choice.hint,
    )


def check_source_work(source: SourceFacts, from_host: str) -> Check:
    """Relocating away from unsaved work strands it on a host nobody watches.

    The agent arrives on the target intact and its half-finished branch does
    not. Nothing fails, nothing is logged, and the work is found missing later
    by someone who assumed a relocation moved everything.
    """
    where = from_host or "the source"
    if source.repos is None:
        return Check(
            name=CHECK_SOURCE_WORK,
            ok=None,
            detail=f"un-saved work on {where} was not looked for",
            hint=(
                f"scan the agent's repos on {where} with `git status --porcelain` and a "
                "`@{u}..` count, and record the numbers; a relocation carries the spec "
                "and the transcript, so anything unsaved stays behind"
            ),
        )
    unmeasured = tuple(r for r in source.repos if r.has_work is None)
    if unmeasured:
        listed = ", ".join(r.path for r in unmeasured)
        return Check(
            name=CHECK_SOURCE_WORK,
            ok=None,
            detail=f"un-saved work on {where} was not measured for: {listed}",
            hint=(
                "run `git status --porcelain` and a `@{u}..` count in each and record "
                "the numbers; an unscanned repo is not a clean one"
            ),
        )
    dirty = tuple(r for r in source.repos if r.has_work is True)
    if dirty:
        parts = []
        for repo in dirty:
            counts = []
            if repo.uncommitted:
                counts.append(f"{repo.uncommitted} uncommitted file(s)")
            if repo.unpushed:
                counts.append(f"{repo.unpushed} unpushed commit(s)")
            branch = f" on {repo.branch}" if repo.branch else ""
            parts.append(f"{repo.path}{branch}: {', '.join(counts)}")
        return Check(
            name=CHECK_SOURCE_WORK,
            ok=False,
            detail=f"un-saved work on {where} — " + "; ".join(parts),
            hint=(
                "commit and push it, or stash it deliberately, before moving. A "
                "relocation carries the spec and the transcript and nothing else, so "
                "this work stays on a machine the agent will no longer be looking at"
            ),
        )
    return Check(
        name=CHECK_SOURCE_WORK,
        ok=True,
        detail=f"every repo scanned on {where} is committed and pushed ({len(source.repos)} scanned)",
    )
