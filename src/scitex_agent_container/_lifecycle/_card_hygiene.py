"""Deterministic card-hygiene validator (blocker-validity pillar).

Incident 2026-07-01: an agent looked at its OWN in_progress cards and labelled
them "gated / not on my side" — including a card blocked only on its own
mergeable PR. A valid blocker is a CARD owned by a DIFFERENT appropriate agent;
your own work is never a blocker. This validator makes that rule deterministic:
it reads the shared tasks store (no scitex_todo import — honours the standalone
mandate) and flags cards whose declared block is invalid.

Rules (per ACTIVE card):
- SELF_BLOCK        — a blocker card is assigned to the SAME agent (it's your job).
- VOID_BLOCKER      — a blocker card is in a terminal state (done/cancelled/…).
- BLOCKED_NO_BLOCKER — status ``blocked`` but no blocker/depends_on registered.

Callers: the per-agent Stop-hook (fail loud before turn end) and the
listen-server liveness-tick reconciler. Kept a pure function of the task list
so it is trivially testable with real fixtures (no mocks).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

SELF_BLOCK = "self-block"
VOID_BLOCKER = "void-blocker"
BLOCKED_NO_BLOCKER = "blocked-no-blocker"

# Cards an owner is accountable for right now.
_ACTIVE_STATUSES = frozenset({"in_progress", "blocked"})
# A blocker pointing at one of these is stale — it can never unblock anything.
_TERMINAL_STATUSES = frozenset({"done", "cancelled", "canceled", "deferred", "closed"})


@dataclass(frozen=True)
class CardViolation:
    card_id: str
    rule: str
    detail: str


def _assignee(card: dict) -> str:
    owner = card.get("assignee") or card.get("agent") or ""
    return str(owner).strip()


def _blocker_card_refs(card: dict, index: dict[str, dict]) -> list[str]:
    """Blocker references that resolve to real cards. ``depends_on`` is a list
    of card ids; ``blocker`` is a card id ONLY when it names an existing card
    (a free-form value like ``operator-decision`` is a declared reason, not a
    card ref, so it is left alone).
    """
    refs: list[str] = []
    for dep in card.get("depends_on", None) or []:
        dep = str(dep).strip()
        if dep in index:
            refs.append(dep)
    blocker = str(card.get("blocker", "") or "").strip()
    if blocker and blocker in index:
        refs.append(blocker)
    return refs


def _has_declared_blocker(card: dict) -> bool:
    blocker = str(card.get("blocker", "") or "").strip()
    deps = card.get("depends_on", None) or []
    return bool(blocker) or bool(deps)


def audit_tasks(
    tasks: list[dict], *, agent: str | None = None
) -> list[CardViolation]:
    """Return blocker-validity violations across ``tasks``.

    ``agent`` restricts to cards that agent owns (assignee/agent); ``None``
    audits the whole board.
    """
    index = {
        str(t.get("id", "")).strip(): t
        for t in tasks
        if isinstance(t, dict) and str(t.get("id", "")).strip()
    }
    violations: list[CardViolation] = []
    for card in tasks:
        if not isinstance(card, dict):
            continue
        cid = str(card.get("id", "")).strip()
        if not cid:
            continue
        if str(card.get("status", "")).strip() not in _ACTIVE_STATUSES:
            continue
        owner = _assignee(card)
        if agent is not None and owner != agent:
            continue

        if str(card.get("status", "")).strip() == "blocked" and not _has_declared_blocker(card):
            violations.append(
                CardViolation(cid, BLOCKED_NO_BLOCKER, "status=blocked with no blocker/depends_on registered")
            )

        for ref in _blocker_card_refs(card, index):
            blocker_card = index[ref]
            if _assignee(blocker_card) == owner and owner:
                violations.append(
                    CardViolation(cid, SELF_BLOCK, f"blocker '{ref}' is assigned to the same agent ({owner}) — it's your job, not a block")
                )
            if str(blocker_card.get("status", "")).strip() in _TERMINAL_STATUSES:
                violations.append(
                    CardViolation(cid, VOID_BLOCKER, f"blocker '{ref}' is {blocker_card.get('status')} — stale block; re-home or escalate")
                )
    return violations


def _default_tasks_path() -> Path:
    env = os.environ.get("SCITEX_TODO_TASKS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".scitex" / "todo" / "tasks.yaml"


def audit_tasks_file(
    path: Path | None = None, *, agent: str | None = None
) -> list[CardViolation]:
    import yaml

    tasks_path = path if path is not None else _default_tasks_path()
    doc = yaml.safe_load(tasks_path.read_text()) or {}
    return audit_tasks(list(doc.get("tasks", []) or []), agent=agent)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="sac-card-hygiene")
    parser.add_argument("--agent", default=os.environ.get("SCITEX_AGENT_CONTAINER_NAME"))
    parser.add_argument("--tasks-path", default=None)
    args = parser.parse_args(argv)
    path = Path(args.tasks_path).expanduser() if args.tasks_path else None
    violations = audit_tasks_file(path, agent=args.agent)
    if not violations:
        return 0
    for v in violations:
        print(f"ERROR[sac-card-hygiene] {v.rule}: {v.card_id} — {v.detail}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
