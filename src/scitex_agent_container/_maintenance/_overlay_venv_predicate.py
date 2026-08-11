"""Facts in, three-valued checks out. Nothing here touches a disk.

WHY THERE IS NO I/O IN THIS MODULE. It evaluates FACTS someone else gathered —
:class:`._overlay_venv_model.OverlayVenvFacts` in,
:class:`._overlay_venv_model.InvalidationPlan` out. That means every refusal
is unit-testable against the exact broken state we hit in production, without
root, without apptainer, and without fabricating a live overlay mount — which
is the only way to know a guard would actually have refused.

THE GUARD THAT MATTERS MOST is :func:`_check_not_inside_container`. Moving the
venv slice aside from INSIDE a running container does not move it: overlayfs
turns the rename into a name-specific WHITEOUT in the upper layer, which then
masks the SIF's clean files too. That converts a recoverable shadow into a
permanently broken tree — the failure this whole rail exists to prevent, caused
by the repair for it. Refuse rather than proceed.

ORDER OF THE CHECKS IS THE ORDER OF THE HAZARD, not of the question: the three
safety checks come first so a report read top-down leads with "may I act",
never with "should I act".
"""

from __future__ import annotations

from ._overlay_venv_model import (
    CHECK_AGENT_NOT_RUNNING,
    CHECK_NOT_INSIDE_CONTAINER,
    CHECK_OVERLAY_READABLE,
    CHECK_SIF_IDENTITY,
    CHECK_UPPER_NOT_MOUNTED_HERE,
    InvalidationPlan,
    OverlayVenvFacts,
    VenvCheck,
)

__all__ = ["plan_invalidation"]

_UNOBSERVED_HINT = (
    "gather this fact before deciding; an unobserved check is not a passing "
    "one, and acting on it would move files on a guess"
)


def _unobserved(name: str, what: str) -> VenvCheck:
    return VenvCheck(
        name=name,
        ok=None,
        detail=f"{what} was not observed",
        hint=_UNOBSERVED_HINT,
    )


def _check_not_inside_container(facts: OverlayVenvFacts) -> VenvCheck:
    """The whiteout guard. See the module docstring — this one is the reason."""
    if facts.inside_container is None:
        return _unobserved(CHECK_NOT_INSIDE_CONTAINER, "container membership")
    if facts.inside_container:
        return VenvCheck(
            name=CHECK_NOT_INSIDE_CONTAINER,
            ok=False,
            detail="this process is running INSIDE a container",
            hint=(
                "run the invalidation from the HOST. From inside, the overlay "
                "is mounted live and a rename/unlink writes an overlayfs "
                "WHITEOUT that also masks the SIF's clean files — it turns a "
                "recoverable shadow into a permanently broken tree"
            ),
        )
    return VenvCheck(
        name=CHECK_NOT_INSIDE_CONTAINER,
        ok=True,
        detail="running on the host, not inside a container",
    )


def _check_agent_not_running(facts: OverlayVenvFacts) -> VenvCheck:
    if facts.agent_running is None:
        return _unobserved(CHECK_AGENT_NOT_RUNNING, "agent liveness")
    if facts.agent_running:
        return VenvCheck(
            name=CHECK_AGENT_NOT_RUNNING,
            ok=False,
            detail="the agent is running, so its container has this overlay mounted",
            hint=(
                "stop the agent first (`sac agents stop <name>`); renaming an "
                "upperdir out from under a live overlayfs is undefined and can "
                "corrupt the running container"
            ),
        )
    return VenvCheck(
        name=CHECK_AGENT_NOT_RUNNING,
        ok=True,
        detail="no live agent process claims this overlay",
    )


def _check_upper_not_mounted_here(facts: OverlayVenvFacts) -> VenvCheck:
    """Corroborating witness, deliberately narrow.

    A mount made by ANOTHER process lives in that process's mount namespace and
    is invisible here, so a pass proves only that WE have not mounted it. It is
    kept because it is free and independently decisive when it fires; the
    liveness check above is what covers the other namespaces.
    """
    if facts.upper_mounted_here is None:
        return _unobserved(CHECK_UPPER_NOT_MOUNTED_HERE, "this process's mount table")
    if facts.upper_mounted_here:
        return VenvCheck(
            name=CHECK_UPPER_NOT_MOUNTED_HERE,
            ok=False,
            detail="an overlayfs in this mount namespace is using this upper layer",
            hint=(
                "unmount it before touching the upper layer; writing through a "
                "live overlayfs produces whiteouts, not deletions"
            ),
        )
    return VenvCheck(
        name=CHECK_UPPER_NOT_MOUNTED_HERE,
        ok=True,
        detail="no overlayfs in this mount namespace uses this upper layer",
    )


def _check_sif_identity(facts: OverlayVenvFacts) -> VenvCheck:
    if facts.sif_identity is None:
        return _unobserved(CHECK_SIF_IDENTITY, "the SIF identity")
    if not facts.sif_identity:
        return VenvCheck(
            name=CHECK_SIF_IDENTITY,
            ok=None,
            detail="the image path did not resolve to a stat-able SIF",
            hint=(
                "check that spec.apptainer.image exists and that the "
                "sac-base.sif symlink resolves; with no identity there is "
                "nothing to compare the overlay's stamp against, and "
                "invalidating on an unknown image would be a coin flip"
            ),
        )
    return VenvCheck(
        name=CHECK_SIF_IDENTITY,
        ok=True,
        detail=f"image resolves to {facts.sif_identity}",
    )


def _check_overlay_readable(facts: OverlayVenvFacts) -> VenvCheck:
    if facts.recorded_identity is None:
        return VenvCheck(
            name=CHECK_OVERLAY_READABLE,
            ok=None,
            detail="the overlay's SIF stamp could not be read",
            hint=(
                "check permissions on the overlay root; an unreadable stamp is "
                "NOT an unstamped overlay, and treating it as one would move a "
                "tree we never inspected"
            ),
        )
    if facts.venv_slice_present is None:
        return VenvCheck(
            name=CHECK_OVERLAY_READABLE,
            ok=None,
            detail="could not tell whether the overlay upper carries a venv slice",
            hint=(
                "check permissions on <overlay>/upper; 'could not read' must "
                "never be recorded as 'nothing there'"
            ),
        )
    return VenvCheck(
        name=CHECK_OVERLAY_READABLE,
        ok=True,
        detail=(
            "overlay stamp and upper layer both readable "
            f"(venv slice {'present' if facts.venv_slice_present else 'absent'})"
        ),
    )


def plan_invalidation(
    *,
    agent: str,
    overlay_root: str,
    facts: OverlayVenvFacts,
) -> InvalidationPlan:
    """Evaluate every check against observed facts. Touches nothing.

    Returns the FULL plan rather than the first refusal: an operator asking
    "why did this not invalidate?" needs every reason at once, not N runs to
    find N reasons.

    Staleness is computed only from OBSERVED identities. When either side is
    unobservable the corresponding check is already UNKNOWN, so ``action``
    resolves to ``refuse`` regardless of what ``stale`` says — but ``stale`` is
    still reported honestly rather than defaulted to ``False``, because a
    silent ``False`` here is what a future reader would mistake for "checked,
    and it was fresh".
    """
    checks = (
        _check_not_inside_container(facts),
        _check_agent_not_running(facts),
        _check_upper_not_mounted_here(facts),
        _check_sif_identity(facts),
        _check_overlay_readable(facts),
    )
    current = facts.sif_identity or ""
    recorded = facts.recorded_identity or ""
    # An UNSTAMPED overlay (recorded == "") is STALE by construction: it is the
    # pre-contract state, which is precisely the state the 2026-08-11 sweep
    # measured, so it must invalidate rather than be adopted as "matches".
    stale = current != recorded
    return InvalidationPlan(
        agent=agent,
        overlay_root=overlay_root,
        sif_identity=current,
        recorded_identity=recorded,
        checks=checks,
        stale=stale,
        venv_slice_present=bool(facts.venv_slice_present),
    )
