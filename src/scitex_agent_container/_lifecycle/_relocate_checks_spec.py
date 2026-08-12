"""Three more promises the spec makes, asked of the machine that must keep them.

:mod:`_relocate_checks` holds the twelve predicates learned by moving an agent by
hand. These three were learned on 2026-08-11 from the opposite direction — the
operator's question 「スペックが相手方でバリデーションが通るか」, does this spec
VALIDATE on the other side — and each covers a promise the original twelve never
asked about:

    workdir     the directory the container runs in (``--pwd``). There is no
                ``spec.repo``: ``spec.workdir`` IS the checkout, and it is a HOST
                path that must be mounted by a bind. A missing one fails at boot,
                which under relocation means after the source has been stopped.
    card store  the DSN itself, not merely whether something answers on it. 5432
                is wrong on every host in this fleet with no exceptions, and a
                DSN carrying it can still PASS a reachability probe — some other
                postgres answers, and the agent quietly records its work into a
                database nobody reads.
    groups      whether the TARGET's own sac can resolve the group labels this
                spec declares. Measured 2026-08-11: three hosts run a listen
                daemon whose group resolution returns ``[]`` no matter what
                ``spec.yaml`` says, and nine relocation probes were refused 403
                because of it. An agent moved onto such a host holds its groups
                on paper and is refused by every group-gated call.

THE 5432 RULE IS ABSOLUTE AND THE ABSENT PORT IS THE SAME BUG. libpq defaults a
port-less DSN to 5432, so ``postgresql://scitex_cards@127.0.0.1/scitex_cards``
means the identical wrong endpoint while looking like it names nothing. Both fail
here.

A PORT THAT IS NEITHER PASSES, AND SAYS SO. Thirty fleet specs name 55432 and
that is the convention, but a host may legitimately map its own; this preflight
states the deviation in the detail rather than inventing a failure it cannot
justify. The hard rule is enforced where the evidence is unambiguous, and nowhere
else.

Pure predicates over observed facts, like their twelve neighbours. No I/O.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlparse

from ._relocate_preflight_facts import Check, TargetFacts

__all__ = [
    "CHECK_CARD_STORE_DSN",
    "CHECK_GROUPS",
    "CHECK_WORKDIR",
    "FLEET_CARD_STORE_PORT",
    "WRONG_CARD_STORE_PORT",
    "check_card_store_dsn",
    "check_target_groups",
    "check_workdir",
]

CHECK_WORKDIR: Final = "workdir_exists_on_target"
CHECK_CARD_STORE_DSN: Final = "card_store_dsn_correct"
CHECK_GROUPS: Final = "groups_resolvable_on_target"

#: postgres's own default, and the one port that is never right here.
WRONG_CARD_STORE_PORT: Final = 5432
#: What every spec in this fleet names (thirty of thirty, measured 2026-08-11).
FLEET_CARD_STORE_PORT: Final = 55432


def check_workdir(facts: TargetFacts, to_host: str) -> Check:
    """The container's ``--pwd`` must exist THERE, not merely here.

    ``missing_workdir_paths`` carries the same three-valued shape as the binds
    fact: ``None`` is nobody-looked, ``()`` is looked-and-nothing-missing (which
    a spec declaring no workdir satisfies by construction), and a non-empty tuple
    names what is absent.
    """
    if facts.missing_workdir_paths is None:
        return Check(
            name=CHECK_WORKDIR,
            ok=None,
            detail=f"whether the agent's workdir exists on {to_host} was not observed",
            hint=(
                f"test it before deciding: ssh {to_host} '[ -d <workdir> ]'. The workdir "
                "is the container's --pwd, so an absent one fails at boot — which under "
                "relocation is after the source has already been stopped"
            ),
        )
    if facts.missing_workdir_paths:
        missing = ", ".join(facts.missing_workdir_paths)
        return Check(
            name=CHECK_WORKDIR,
            ok=False,
            detail=f"the agent's workdir does not exist on {to_host}: {missing}",
            hint=(
                f"create it on {to_host} before relocating — if it is a git checkout, "
                "clone it there and check out the branch the agent works on. sac passes "
                "spec.workdir to apptainer as --pwd and also needs a bind that mounts "
                "it; a workdir that exists but is unbound fails the same way"
            ),
        )
    return Check(
        name=CHECK_WORKDIR,
        ok=True,
        detail=f"the agent's workdir exists on {to_host}",
    )


def _port_of(url: str) -> int | None:
    """The DSN's explicit port, or ``None`` when it names none."""
    try:
        return urlparse(url).port
    except ValueError:
        return None


def check_card_store_dsn(facts: TargetFacts) -> Check:
    """The DSN the agent WOULD dial — judged as a string, before anything answers.

    Separate from ``card_store_reachable`` because they are different questions
    with different fixes, and because reachability can PASS on a wrong endpoint:
    something is listening on 5432 on most machines, so a spec that names it gets
    a green light and then writes the fleet's cards into a database nobody reads.
    """
    url = facts.card_store_url
    if url is None:
        return Check(
            name=CHECK_CARD_STORE_DSN,
            ok=None,
            detail="the card-store DSN this agent would use was not established",
            hint=(
                "declare SCITEX_CARDS_DB for this agent (spec.apptainer.env, or an "
                "--env pair in spec.apptainer.raw_args) — an agent with no board "
                f"records nothing. The fleet's endpoint is port {FLEET_CARD_STORE_PORT}"
            ),
        )
    port = _port_of(url)
    if port is None:
        return Check(
            name=CHECK_CARD_STORE_DSN,
            ok=False,
            detail=f"the card-store DSN names no port: {url}",
            hint=(
                f"give it :{FLEET_CARD_STORE_PORT} explicitly. A port-less DSN is not "
                f"neutral — libpq defaults it to {WRONG_CARD_STORE_PORT}, which is the "
                "wrong database on every host in this fleet, and it fails silently "
                "because something usually answers there"
            ),
        )
    if port == WRONG_CARD_STORE_PORT:
        return Check(
            name=CHECK_CARD_STORE_DSN,
            ok=False,
            detail=f"the card-store DSN names port {WRONG_CARD_STORE_PORT}: {url}",
            hint=(
                f"change it to :{FLEET_CARD_STORE_PORT}. {WRONG_CARD_STORE_PORT} is "
                "postgres's default and is wrong on every host here with no exceptions; "
                "a reachability probe still passes on it, so this must be caught by "
                "reading the DSN rather than by dialling it"
            ),
        )
    if port != FLEET_CARD_STORE_PORT:
        return Check(
            name=CHECK_CARD_STORE_DSN,
            ok=True,
            detail=(
                f"the card-store DSN names port {port}, not the fleet's "
                f"{FLEET_CARD_STORE_PORT} — passing because {port} is not the forbidden "
                f"{WRONG_CARD_STORE_PORT}; confirm the deviation is deliberate"
            ),
        )
    return Check(
        name=CHECK_CARD_STORE_DSN,
        ok=True,
        detail=f"the card-store DSN names the fleet's port {FLEET_CARD_STORE_PORT}",
    )


def check_target_groups(
    facts: TargetFacts, declared_groups: tuple[str, ...], to_host: str
) -> Check:
    """Would the TARGET's own sac recognise the groups this spec declares?

    The distinction this check exists to preserve is the operator's: "the target
    refused me" is not "the target says no". A resolver that answers with a
    NON-EMPTY set and omits a declared group has made a statement — that is a
    FAIL. A resolver that answers with nothing, or that could not be asked at
    all, has made none, and on 2026-08-11 exactly that shape (three hosts whose
    group resolution returns ``[]`` regardless of spec.yaml) refused nine
    relocation probes with a 403. Reporting it as a FAIL would send the operator
    to edit a spec that is already correct.
    """
    if not declared_groups:
        return Check(
            name=CHECK_GROUPS,
            ok=True,
            detail="the spec declares no groups, so none has to hold on the target",
        )
    if facts.target_resolved_groups is None:
        return Check(
            name=CHECK_GROUPS,
            ok=None,
            detail=(
                f"whether {to_host}'s sac resolves this spec's groups "
                f"({', '.join(declared_groups)}) was not observed"
            ),
            hint=(
                "ask the target's own sac to resolve them before relocating. If it "
                "cannot — an older listen daemon resolves every caller to [] no matter "
                "what spec.yaml declares — the agent arrives holding its groups on paper "
                "and is refused 403 by every group-gated call it makes"
            ),
        )
    resolved = facts.target_resolved_groups
    if not resolved:
        return Check(
            name=CHECK_GROUPS,
            ok=None,
            detail=(
                f"{to_host}'s sac resolved NO groups from this spec's labels "
                f"({', '.join(declared_groups)}) — which is what a daemon too old to "
                "read spec labels answers for every agent, so it is not a verdict about "
                "this spec"
            ),
            hint=(
                f"upgrade sac on {to_host} and re-run. An empty resolution cannot be "
                "told apart from 'this agent genuinely holds no groups', and the two "
                "have opposite fixes; on 2026-08-11 this exact shape refused nine "
                "relocation probes with a 403 while every spec involved was correct"
            ),
        )
    absent = tuple(g for g in declared_groups if g not in resolved)
    if absent:
        return Check(
            name=CHECK_GROUPS,
            ok=False,
            detail=(
                f"{to_host}'s sac resolves {', '.join(sorted(resolved))} for this spec "
                f"and does not recognise: {', '.join(absent)}"
            ),
            hint=(
                "the target answered, and the answer is no — so this is a spec/target "
                f"disagreement, not an outage. Either drop those labels or teach {to_host} "
                "about them; an unrecognised group is silently no group at the ACL gate"
            ),
        )
    return Check(
        name=CHECK_GROUPS,
        ok=True,
        detail=f"{to_host}'s sac resolves every declared group: {', '.join(declared_groups)}",
    )
