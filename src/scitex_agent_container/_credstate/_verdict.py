"""The answer a host must be able to give mechanically.

    "A host must be able to answer, mechanically: do I have what I need
     to act, and if not, what exactly is missing and where does it come
     from? Today the answer requires a human to look at files."

This module is that answer. It takes a DECLARATION (what the fleet says
should be here) and a MEASUREMENT (what this node's disk actually shows)
and returns findings that name the fault and the remedy.

The declaration/measurement split is load-bearing. Nothing here can go
green because a schedule ran: every verdict is a comparison against a
measurement of the artifact itself. The one failure this domain exists to
end is a green board over a dead credential.

The verdict set is small on purpose, and two of the entries are the ones
that did not exist before:

``EXTRA_REFRESHER``
    A node holds refresh material that the fleet did not declare it to
    hold. This is the CR-001 violation, and today it is INVISIBLE:
    ``holds_refresh_material`` reports whatever the disk says, so a
    second holder appearing looks exactly like the first one. Two
    refreshers mutually invalidate — an OAuth refresh rotates the refresh
    token and whichever host refreshes first silently revokes the other.

``UNDECLARED``
    Credential-shaped material exists here that no row describes. This is
    the shape of the token found in a ``~/.bashrc`` and of the Telegram
    token folded into ``$HOME/.env`` — present, working, and recorded
    nowhere, so the next host change loses it without a sound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Sequence

from ._model import ROLE_PRIMARY, TIER_DISTRIBUTABLE, TIER_PRIMARY_SECRET

OK = "OK"
ABSENT = "ABSENT"
UNRESOLVABLE = "UNRESOLVABLE"
EXPIRED = "EXPIRED"
EXPIRING = "EXPIRING"
EXTRA_REFRESHER = "EXTRA_REFRESHER"
NO_REFRESHER = "NO_REFRESHER"
WORLD_READABLE = "WORLD_READABLE"
UNDECLARED = "UNDECLARED"
UNKNOWN_EXPIRY = "UNKNOWN_EXPIRY"

SEVERITY_OK = "ok"
SEVERITY_WARN = "warn"
SEVERITY_FAULT = "fault"

_SEVERITY: dict[str, str] = {
    OK: SEVERITY_OK,
    EXPIRING: SEVERITY_WARN,
    UNKNOWN_EXPIRY: SEVERITY_WARN,
    WORLD_READABLE: SEVERITY_WARN,
    UNDECLARED: SEVERITY_WARN,
    ABSENT: SEVERITY_FAULT,
    UNRESOLVABLE: SEVERITY_FAULT,
    EXPIRED: SEVERITY_FAULT,
    EXTRA_REFRESHER: SEVERITY_FAULT,
    NO_REFRESHER: SEVERITY_FAULT,
}

#: Default warning horizon. Matches the spirit of
#: ``_keepalive_guards.MIN_VALIDITY_S`` (a token that dies in flight is
#: worse than no token, because it looks like a fix) but is longer,
#: because this is a report to a human, not a go/no-go on a push.
DEFAULT_EXPIRING_HORIZON_S = 1800


@dataclass(frozen=True)
class Finding:
    """One credential's answer on one node, with the remedy attached.

    ``remedy`` is never optional for a fault. A report that names a
    problem without naming where the fix comes from leaves the operator
    exactly where the file-hunting started.
    """

    cred_key: str
    node: str
    verdict: str
    severity: str
    summary: str
    remedy: str | None = None

    @property
    def is_fault(self) -> bool:
        return self.severity == SEVERITY_FAULT


def _remedy_for_absent(descriptor, node: str) -> str:
    """Where a missing credential comes from — the materialize answer.

    For ``primary_secret`` the honest answer is that it must NOT be
    materialized here. That is not a gap in the implementation; it is the
    two-tier model holding. Copying refresh material to a second host is
    the exact defect ``assert_access_only`` exists to prevent, so a
    "materialize" verb that offered to do it would quietly repeal the
    invariant this design is required to preserve.
    """
    if descriptor is None:
        return (
            f"no descriptor declares this credential, so its source is "
            f"unknown. Declare it (primary node, tier, locator) before "
            f"{node} can be told where to get it."
        )
    if descriptor.tier == TIER_PRIMARY_SECRET and descriptor.primary_node != node:
        return (
            f"do NOT copy this here. It is tier '{TIER_PRIMARY_SECRET}': the "
            f"material lives only on primary '{descriptor.primary_node}', "
            f"because cloning refresh material onto a second host makes both "
            f"hosts able to rotate it and silently revoke each other. If "
            f"{node} must act, it needs a '{TIER_DISTRIBUTABLE}' artifact "
            f"minted on {descriptor.primary_node} "
            f"(sac accounts keepalive --to {node}), not this credential."
        )
    if descriptor.obtain_command:
        return (
            f"materialize from primary '{descriptor.primary_node}': "
            f"{descriptor.obtain_command}"
        )
    return (
        f"comes from primary '{descriptor.primary_node}'; no obtain_command "
        f"is recorded on the descriptor, so the path to get it is still "
        f"undocumented — record one."
    )


def assess(
    *,
    descriptor,
    placement,
    observation,
    node: str,
    now: datetime | None = None,
    expiring_horizon_s: int = DEFAULT_EXPIRING_HORIZON_S,
) -> list[Finding]:
    """Compare one declaration against one measurement. Returns every finding.

    Returns a LIST, not a single verdict, because a credential can be
    wrong in more than one way at once and reporting only the worst hides
    the others — a present-but-world-readable token that is also about to
    expire is two problems with two different fixes.
    """
    _now = now or datetime.now(timezone.utc)
    key = placement.cred_key
    findings: list[Finding] = []

    def add(verdict: str, summary: str, remedy: str | None = None) -> None:
        findings.append(
            Finding(
                cred_key=key,
                node=node,
                verdict=verdict,
                severity=_SEVERITY[verdict],
                summary=summary,
                remedy=remedy,
            )
        )

    if observation.scheme is None:
        add(
            UNRESOLVABLE,
            f"locator {placement.locator!r} cannot be resolved on {node}",
            "record a locator of the form 'file:<abs path>' or 'env:<VARNAME>'",
        )
        return findings

    if not observation.present:
        if placement.required:
            add(
                ABSENT,
                f"required here but {observation.detail or 'not present'}",
                _remedy_for_absent(descriptor, node),
            )
        else:
            add(OK, "not required on this node and not present")
        return findings

    # ---- present: now everything else is checkable -------------------
    expires = observation.artifact_expires_at
    if expires is not None:
        if expires <= _now:
            add(
                EXPIRED,
                f"present but the artifact's own expiry passed at "
                f"{expires.isoformat()}",
                _refresh_remedy(descriptor, node),
            )
        elif expires <= _now + timedelta(seconds=expiring_horizon_s):
            add(
                EXPIRING,
                f"expires at {expires.isoformat()} "
                f"(within {expiring_horizon_s}s)",
                _refresh_remedy(descriptor, node),
            )
    elif observation.scheme == "file":
        add(
            UNKNOWN_EXPIRY,
            "present, but the artifact declares no expiry this reader "
            "recognises — so 'is it still usable' cannot be answered here",
            "if this credential does expire, record the field name it uses; "
            "an unknown expiry reads as 'never expires' and that is how a "
            "dead token looks healthy",
        )

    if observation.world_readable:
        add(
            WORLD_READABLE,
            f"mode {observation.file_mode} — readable beyond its owner",
            f"chmod 600 on the file named by {placement.locator!r}",
        )

    findings.extend(
        _refresher_findings(
            descriptor=descriptor,
            placement=placement,
            observation=observation,
            node=node,
            add_key=key,
        )
    )

    if not findings:
        add(OK, f"present at {placement.locator}")
    return findings


def _refresh_remedy(descriptor, node: str) -> str:
    if descriptor is None:
        return "no descriptor declares who renews this credential — declare one"
    if descriptor.primary_node == node and descriptor.refresh_command:
        return f"this node is primary; renew with: {descriptor.refresh_command}"
    if descriptor.primary_node == node:
        return (
            "this node is primary but no refresh_command is recorded, so how "
            "it gets renewed is still undocumented — record one"
        )
    return (
        f"renewal happens on primary '{descriptor.primary_node}', not here; "
        f"this node receives a refreshed artifact from it"
    )


def _refresher_findings(
    *, descriptor, placement, observation, node: str, add_key: str
) -> list[Finding]:
    """The CR-001 disk half: declared role vs. what the disk actually holds."""
    holds = observation.holds_refresh_material
    if descriptor is None or holds is None:
        return []
    declared_primary = (
        placement.role == ROLE_PRIMARY or descriptor.primary_node == node
    )
    out: list[Finding] = []
    if holds and not declared_primary:
        out.append(
            Finding(
                cred_key=add_key,
                node=node,
                verdict=EXTRA_REFRESHER,
                severity=_SEVERITY[EXTRA_REFRESHER],
                summary=(
                    f"{node} is declared a replica but holds REFRESH material. "
                    f"Primary is '{descriptor.primary_node}'. Two hosts able "
                    f"to refresh the same credential mutually invalidate: the "
                    f"refresh token rotates on use, so whichever refreshes "
                    f"first silently revokes the other."
                ),
                remedy=(
                    f"strip this host to access-only (it should hold a "
                    f"minted artifact, not refresh material), or hand primacy "
                    f"over from '{descriptor.primary_node}' deliberately — "
                    f"but exactly one node may hold it."
                ),
            )
        )
    if declared_primary and not holds:
        out.append(
            Finding(
                cred_key=add_key,
                node=node,
                verdict=NO_REFRESHER,
                severity=_SEVERITY[NO_REFRESHER],
                summary=(
                    f"{node} is declared PRIMARY for this credential but holds "
                    f"no refresh material, so nothing here can renew it. If no "
                    f"other node holds it either, the credential is on a "
                    f"one-way trip to expiry with no alarm attached."
                ),
                remedy=(
                    "re-authenticate on this node, or move the declared "
                    "primary to the node that actually holds the material"
                ),
            )
        )
    return out


def check_single_refresher(
    *, holders: Sequence[str], cred_key: str, declared_primary: str
) -> list[Finding]:
    """CR-001 across nodes: exactly one holder, and it is the declared one.

    ``holders`` is the set of nodes observed to hold refresh material.
    Kept as a separate entry point from :func:`assess` because this is a
    FLEET question — one node's observation cannot answer it, and a check
    that silently answers a fleet question from local data is how "the
    timer is running" came to be mistaken for "the token works".
    """
    findings: list[Finding] = []
    unique = sorted(set(holders))
    if len(unique) > 1:
        findings.append(
            Finding(
                cred_key=cred_key,
                node=", ".join(unique),
                verdict=EXTRA_REFRESHER,
                severity=SEVERITY_FAULT,
                summary=(
                    f"{len(unique)} nodes hold refresh material for "
                    f"{cred_key!r}: {', '.join(unique)}. CR-001 allows exactly "
                    f"one. These hosts are revoking each other's tokens every "
                    f"time either one refreshes."
                ),
                remedy=(
                    f"leave the material only on the declared primary "
                    f"'{declared_primary}' and strip the others to "
                    f"access-only artifacts"
                ),
            )
        )
    elif unique and unique[0] != declared_primary:
        findings.append(
            Finding(
                cred_key=cred_key,
                node=unique[0],
                verdict=EXTRA_REFRESHER,
                severity=SEVERITY_FAULT,
                summary=(
                    f"refresh material for {cred_key!r} is held by "
                    f"{unique[0]!r}, but the declared primary is "
                    f"{declared_primary!r}. The declaration and the disk "
                    f"disagree about who owns this credential."
                ),
                remedy=(
                    "reconcile deliberately: either move the material or "
                    "update the declaration — but do not leave them disagreeing"
                ),
            )
        )
    elif not unique:
        findings.append(
            Finding(
                cred_key=cred_key,
                node="(none)",
                verdict=NO_REFRESHER,
                severity=SEVERITY_FAULT,
                summary=(
                    f"NO node holds refresh material for {cred_key!r}. Nothing "
                    f"in the fleet can renew it; it expires and stays expired."
                ),
                remedy=f"re-authenticate on the declared primary {declared_primary!r}",
            )
        )
    return findings


def undeclared_findings(
    *, observed_locators: Iterable[str], declared_locators: Iterable[str], node: str
) -> list[Finding]:
    """Credential material present on this node that no row describes.

    The inverse question, and the one nobody asks: not "is what I
    declared present" but "is anything present that I never declared".
    Both tokens found by looking at filesystems this week were of this
    shape — working, load-bearing, and recorded nowhere.
    """
    declared = set(declared_locators)
    return [
        Finding(
            cred_key="(undeclared)",
            node=node,
            verdict=UNDECLARED,
            severity=_SEVERITY[UNDECLARED],
            summary=(
                f"credential material at {locator!r} on {node} is described by "
                f"no descriptor. It works today and vanishes on the next host "
                f"change, with nothing recording that it was the working path."
            ),
            remedy=f"declare it: sac creds declare --locator {locator!r}",
        )
        for locator in sorted(set(observed_locators) - declared)
    ]


def worst_severity(findings: Sequence[Finding]) -> str:
    if any(f.severity == SEVERITY_FAULT for f in findings):
        return SEVERITY_FAULT
    if any(f.severity == SEVERITY_WARN for f in findings):
        return SEVERITY_WARN
    return SEVERITY_OK
