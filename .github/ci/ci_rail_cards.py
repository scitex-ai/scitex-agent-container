"""The card half of the CI feedback rail: the card contract, and safe
access to the one store that is actually the fleet's board.

Companion to :mod:`ci_card_rail` (orchestration + CLI) and
:mod:`ci_rail_listen` (delivery). This module owns the shape of the card
a push and a verdict share, and the guard that decides whether the store
in front of us is the real one.

THE CARD IS THE RAIL'S ROUTING TABLE, not merely its output. ``pre-push``
records WHO pushed; the verdict half reads that back and delivers to that
agent instead of inferring an owner from the repo name. On this repo the
inference is genuinely wrong: two agent specs declare
``project: scitex-agent-container`` and sac's own ``resolve_owner`` takes
the one that sorts first, which is the one with no inbox subscriber.

WHY THE STORE GUARD EXISTS — measured, not hypothetical. See
:func:`cards`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

# CI outcome -> card status. ``failed``, deliberately NOT ``blocked``, and
# the difference is not cosmetic. A ``blocked`` card must name the gate
# holding it, and this store's blockers are ('compute', 'dependency',
# 'dep', 'operator-decision', 'agent-wait', 'none') — none of which
# describes a red test suite. A red gate waits on nobody; it is a finished
# run with a bad result that its owner can act on immediately. Filing it
# as ``blocked`` with no gate would drop it into exactly the state this
# fleet spent 2026-08-11 paying for: cards that nudge nobody and are
# excluded from the runnable count, invisible until someone goes looking.
# A fix arrives as a NEW push with a NEW sha and therefore a NEW card,
# which is what makes ``failed`` both terminal and honest here.
STATUS_FOR_CONCLUSION = {"success": "done", "failure": "failed"}

__all__ = [
    "STATUS_FOR_CONCLUSION",
    "card_id_for",
    "card_title",
    "cards",
    "get_card",
    "now_stamp",
    "record_push",
    "repo_basename",
    "upsert_card",
]


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def repo_basename(repo: str) -> str:
    return repo.rstrip("/").split("/")[-1].strip()


def card_id_for(repo: str, sha: str) -> str:
    """The card id both halves of the rail derive independently.

    Twelve hex characters of the head sha: long enough that a collision
    is not a practical concern, short enough to stay readable. Keyed on
    the HEAD SHA rather than the branch because a branch is reused across
    pushes while a verdict belongs to the commit it judged. Deriving it
    on both sides is what removes the need for any channel between a git
    hook and a runner job that starts minutes later.
    """
    return f"ci-{repo_basename(repo)}-{sha[:12]}"


def card_title(repo: str, branch: str, sha: str, verdict: str = "") -> str:
    """A title legible on a phone at 4am, which is where this lands.

    The verdict leads, because that is the one word a woken reader needs.
    Repo and branch follow, because a bare short sha identifies nothing
    to a human. The id is for the two halves of the rail to agree on; the
    title is for the person.
    """
    tag = f"ci {verdict}".strip().upper() if verdict else "ci"
    return f"[{tag}] {repo_basename(repo)} {branch} {sha[:8]}"


def cards() -> Any:
    """The card package, with the store it resolved to CHECKED, not assumed.

    This guard is not defensive padding; every clause is a measured
    failure on this host.

    * ``127.0.0.1:5442`` and ``127.0.0.1:55432`` BOTH answer, BOTH report
      ``store_uuid`` ``1d55dd6e-…``, and hold 3496 vs 3793 cards. Two
      divergent databases wearing one identity — so the field designed to
      detect a split is precisely the field that cannot detect this one.
      The cause is now traced: an MCP entry pinned the DSN literally to
      ``:5442`` (an SSH tunnel to another box's postgres) while every
      shell reads ``:55432``, so a process's card writes and its own
      read-back can land in different databases with nothing saying so.
    * An UNSET ``$SCITEX_CARDS_DB`` does not fail. It silently resolves to
      a local ``cards.db`` FILE that no board reads. A runner's ``run:``
      step gets a non-interactive shell sourcing no profile, so unset is
      exactly what it sees unless the workflow passes the DSN explicitly.

    Writing a verdict into a store nobody reads is the same silent-nobody
    failure as delivering to a deaf agent. So: resolve, check, and refuse
    — and RETURN the resolved target so the caller can print it, because
    a rail that cannot say which database it wrote to cannot be audited.
    """
    import scitex_cards

    resolved = scitex_cards.resolve_store() or {}
    backend = str(resolved.get("backend") or "").lower()
    if backend not in ("postgresql", "postgres"):
        raise RuntimeError(
            f"card store resolved to {resolved.get('resolved')!r} "
            f"(backend={backend!r}), not the fleet's postgres store. Set "
            "$SCITEX_CARDS_DB explicitly — an unset DSN falls back to a "
            "local file that no board reads."
        )
    return scitex_cards


def resolved_store_dsn() -> str:
    """The DSN actually in force, for logging. Never raises."""
    try:
        import scitex_cards

        return str((scitex_cards.resolve_store() or {}).get("resolved") or "?")
    except Exception:  # noqa: BLE001 — a logging aid must not break the rail
        return "?"


def get_card(pkg: Any, card_id: str) -> dict[str, Any] | None:
    """The card, or ``None`` if it genuinely does not exist yet.

    ONLY ``TaskNotFoundError`` means "not there"; everything else is
    re-raised, and that narrowness is a scar. This began as a bare
    ``except Exception: return None``, which turned a keyword-argument
    mistake (``get_task`` takes ``task_id=``, not a positional) into a
    phantom "no such card" — so the caller went on to ``add_task`` and
    hit a duplicate-id error one layer away from the cause. A read fault
    dressed as absence is exactly the silent failure this rail exists to
    delete; it must not live inside the rail itself.

    A caution for whoever debugs this next: the not-found error text
    names ``~/.scitex/cards/tasks.yaml``, which is neither the resolved
    DSN nor the ``user_store`` path. Do not read that string as evidence
    of which store was queried — it names one that was not.
    """
    try:
        return pkg.get_task(task_id=card_id)
    except pkg.TaskNotFoundError:
        return None


def upsert_card(pkg: Any, card_id: str, *, title: str, **fields: Any) -> Any:
    """Create the card, or update it when this rail already made one.

    ``add_task`` raises on an existing id, so the read decides. No lock
    spans the two halves of the rail and none is needed: the push half
    runs minutes ahead of the verdict half, and even a genuine race
    converges, because both sides write the same derived id.
    """
    if get_card(pkg, card_id) is None:
        return pkg.add_task(id=card_id, title=title, **fields)
    return pkg.update_task(task_id=card_id, title=title, **fields)


def record_push(
    *, repo: str, branch: str, sha: str, agent: str | None, subject: str = ""
) -> Any:
    """Register a push on its card. Driven by the ``pre-push`` hook.

    ``agent`` is whoever actually pushed, and recording it is the whole
    reason the verdict half can address the right agent later. Status is
    ``in_progress``: a pushed commit awaiting CI is work in flight, and
    the owner is the person who can act on it.
    """
    pkg = cards()
    card_id = card_id_for(repo, sha)
    fields: dict[str, Any] = {
        "status": "in_progress",
        "kind": "task",
        "repo": repo_basename(repo),
        "project": repo_basename(repo),
        "note": f"pushed {sha[:8]} on {branch} at {now_stamp()}; CI verdict pending.",
        "last_activity": now_stamp(),
    }
    if agent:
        fields.update(agent=agent, assignee=agent, created_by=agent)
    card = upsert_card(
        pkg, card_id, title=card_title(repo, branch, sha, "pending"), **fields
    )
    line = f"pushed `{sha[:8]}` to `{branch}`"
    if subject.strip():
        line += f" — {subject.strip().splitlines()[0][:120]}"
    pkg.comment_task(task_id=card_id, text=line, by=agent or "ci")
    return card


# EOF
