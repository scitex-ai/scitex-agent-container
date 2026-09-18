"""Deterministic Hermes session freshness and failure handoff policy."""

from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

DEFAULT_MAX_SESSION_AGE_MINUTES = 3 * 24 * 60
HANDOFF_DIRNAME = "hermes-context-handoffs"
ACTIVE_CARD_FILE = "hermes-active-card.json"
FRESH_NEXT_TASK_FILE = "hermes-fresh-next-task.json"
LIFECYCLE_LOCK_FILE = "hermes-context-lifecycle.lock"
CONSUMED_COMPLETIONS_FILE = "hermes-consumed-completions.json"
OWNED_SESSION_FILE = "hermes-owned-session.json"
_EXPLICIT_CARD_RE = re.compile(
    r"\b(?:card|part\s+of)\s+([a-z0-9]+(?:-[a-z0-9]+)+)", re.IGNORECASE
)


class HermesContextGcRefused(RuntimeError):
    """Lifecycle mutation was refused because durable continuity was unproven."""


@dataclass(frozen=True)
class WorktreeFact:
    path: Path
    sha: str
    branch: str
    dirty: tuple[str, ...]
    accounted: bool = True


@dataclass(frozen=True)
class SessionTransition:
    session_id: str
    handoff_path: Path


def continuation_is_fresh_enough(
    started_at: float | None,
    *,
    now: float,
    max_age_minutes: int = DEFAULT_MAX_SESSION_AGE_MINUTES,
) -> bool:
    """Return whether a stored session is strictly younger than the age cap."""
    if not isinstance(started_at, (int, float)) or started_at <= 0 or started_at > now:
        return False
    return now - started_at < max_age_minutes * 60


def _write_handoff(path: Path, payload: dict) -> None:
    parent_existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not parent_existed:
        parent_directory = os.open(path.parent.parent, os.O_RDONLY)
        try:
            os.fsync(parent_directory)
        finally:
            os.close(parent_directory)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        remaining = memoryview(json.dumps(payload, sort_keys=True).encode("utf-8"))
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("short write while persisting Hermes context handoff")
            remaining = remaining[written:]
        os.fsync(descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        try:
            os.close(descriptor)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _unlink_durable(path: Path) -> None:
    path.unlink(missing_ok=True)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


@contextmanager
def _lifecycle_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(state_dir / LIFECYCLE_LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def record_inbound_task_event(
    state_dir: Path, agent_name: str, event: dict
) -> None:
    """Persist the current card identity or a completion-triggered fresh boundary."""
    extra = event.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    card_id = str(extra.get("card_id") or "").strip()
    if not card_id:
        match = _EXPLICIT_CARD_RE.search(str(event.get("content") or ""))
        card_id = match.group(1) if match else ""
    if not card_id:
        return
    with _lifecycle_lock(state_dir):
        _record_inbound_task_event(state_dir, agent_name, event, card_id=card_id)


def _record_inbound_task_event(
    state_dir: Path, agent_name: str, event: dict, *, card_id: str
) -> None:
    extra = event.get("extra")
    extra = extra if isinstance(extra, dict) else {}
    kind = str(extra.get("card_event_kind") or "").strip()
    owner = str(extra.get("card_event_owner") or "").strip()
    delivery_id = str(event.get("msg_id") or "").strip()
    delivery_session_id = str(event.get("_hermes_delivery_session_id") or "").strip()
    active_path = state_dir / ACTIVE_CARD_FILE
    fresh_path = state_dir / FRESH_NEXT_TASK_FILE
    try:
        active = json.loads(active_path.read_text(encoding="utf-8"))
        was_active = active.get("card_id") == card_id
    except (OSError, ValueError, TypeError):
        was_active = False
    is_card_event = str(event.get("kind") or "") == "card-event"
    trusted_card_event = (
        is_card_event
        and str(event.get("from_agent") or "").strip() == "scitex-cards"
    )
    if kind and not trusted_card_event:
        return
    if kind == "completed":
        # The delivery is only a wake-up hint. Require it to match the active
        # card already selected locally; reconciliation independently reads
        # the exact authenticated Cards row before any session mutation.
        consumed = _consumed_completion_ids(state_dir)
        if (
            was_active
            and delivery_id
            and delivery_session_id
            and delivery_id not in consumed
        ):
            _write_handoff(
                fresh_path,
                {
                    "card_id": card_id,
                    "delivery_id": delivery_id,
                    "owner": owner,
                    "phase": "task-completed",
                    "reason": "task-completed",
                    "session_id": delivery_session_id,
                    "was_active": was_active,
                },
            )
            active_path.unlink(missing_ok=True)
        return
    if is_card_event and (not trusted_card_event or owner != agent_name):
        return
    # A new assignment supersedes any unconsumed completion boundary. Removing
    # it before publishing the active card makes stale completion fail closed.
    stale_marker: dict = {}
    try:
        stale_marker = json.loads(fresh_path.read_text(encoding="utf-8"))
        stale_delivery = str(stale_marker.get("delivery_id") or "").strip()
    except (OSError, ValueError, TypeError):
        stale_delivery = ""
    stale_phase = str(stale_marker.get("phase") or "") if stale_delivery else ""
    if stale_delivery and stale_phase not in {"closing", "closed-awaiting-fresh"}:
        _mark_completion_consumed(state_dir, stale_delivery)
        fresh_path.unlink(missing_ok=True)
    _write_handoff(active_path, {"card_id": card_id})


def _consumed_completion_ids(state_dir: Path) -> set[str]:
    try:
        payload = json.loads(
            (state_dir / CONSUMED_COMPLETIONS_FILE).read_text(encoding="utf-8")
        )
    except (OSError, ValueError, TypeError):
        return set()
    rows = payload.get("delivery_ids") if isinstance(payload, dict) else None
    return {str(row) for row in rows if str(row).strip()} if isinstance(rows, list) else set()


def _mark_completion_consumed(state_dir: Path, delivery_id: str) -> None:
    consumed = _consumed_completion_ids(state_dir)
    consumed.add(delivery_id)
    _write_handoff(
        state_dir / CONSUMED_COMPLETIONS_FILE,
        {"delivery_ids": sorted(consumed)[-100:]},
    )


def _validate_worktrees(
    worktrees: list[WorktreeFact], workdir: Path
) -> tuple[WorktreeFact, tuple[tuple[str, str, str], ...]]:
    dirty = [fact for fact in worktrees if fact.dirty]
    if dirty:
        detail = "; ".join(
            f"{fact.path}: {', '.join(fact.dirty)}" for fact in dirty
        )
        raise HermesContextGcRefused(
            f"dirty worktree content is not committed/accounted: {detail}"
        )
    unaccounted = [fact for fact in worktrees if not fact.accounted]
    if unaccounted:
        raise HermesContextGcRefused(
            "subagent commits are not preserved on a remote ref: "
            + ", ".join(f"{fact.path}@{fact.sha}" for fact in unaccounted)
        )
    resolved = workdir.resolve()
    matches = [fact for fact in worktrees if fact.path.resolve() == resolved]
    if len(matches) != 1 or not matches[0].sha:
        raise HermesContextGcRefused(
            f"cannot bind handoff to one exact worktree SHA for {workdir}"
        )
    signature = tuple(
        sorted(
            (str(fact.path.resolve()), fact.sha, fact.branch) for fact in worktrees
        )
    )
    return matches[0], signature


def handle_compression_failure(
    *,
    state_dir: Path,
    agent_name: str,
    workdir: Path,
    session_record: dict,
    card: dict,
    worktrees: list[WorktreeFact],
    verification: dict,
    next_nonce: Callable[[], str],
    rotate: Callable[[Path, str, str], str],
) -> SessionTransition | None:
    """Persist minimal continuity, prove a fresh session, then retire the old one."""
    del agent_name
    failure = str(session_record.get("compression_failure_error") or "").strip()
    if not failure:
        return None
    current, _signature = _validate_worktrees(worktrees, workdir)
    task_id = str(card.get("id") or "").strip()
    next_action = str(
        card.get("task") or card.get("title") or card.get("note") or ""
    ).strip()
    if not task_id or not next_action:
        raise HermesContextGcRefused("active card id and next action are required")
    old_session_id = str(session_record.get("id") or "").strip()
    if not old_session_id:
        raise HermesContextGcRefused("compression failure record has no session id")
    handoff_path = state_dir / HANDOFF_DIRNAME / f"{old_session_id}.json"
    payload = {
        "task_id": task_id,
        "repo_worktree": str(workdir),
        "exact_sha": current.sha,
        "tests": str(verification.get("tests") or "unknown"),
        "next_action": next_action,
        "blocker": str(card.get("blocker") or "none"),
    }
    _write_handoff(handoff_path, payload)
    nonce = str(next_nonce() or "").strip()
    if not nonce:
        raise HermesContextGcRefused("fresh-session proof nonce is empty")
    fresh_session = str(rotate(handoff_path, nonce, old_session_id) or "").strip()
    if not fresh_session:
        raise HermesContextGcRefused("fresh-session nonce proof was not returned")
    return SessionTransition(fresh_session, handoff_path)


def _git(workdir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(workdir), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise HermesContextGcRefused(detail or f"git {' '.join(args)} failed")
    return completed.stdout


def _commit_is_on_live_remote(workdir: Path, sha: str) -> bool:
    """Prove a commit is contained by a currently advertised remote head."""
    candidates = _git(workdir, "branch", "-r", "--contains", sha).splitlines()
    for candidate in candidates:
        remote_ref = candidate.strip().lstrip("* ")
        remote, separator, branch = remote_ref.partition("/")
        if not separator or not branch or " -> " in remote_ref:
            continue
        advertised = _git(
            workdir, "ls-remote", "--heads", remote, f"refs/heads/{branch}"
        ).splitlines()
        for row in advertised:
            tip = row.split(maxsplit=1)[0] if row else ""
            if not tip:
                continue
            if tip == sha:
                return True
            ancestor = subprocess.run(
                ["git", "-C", str(workdir), "merge-base", "--is-ancestor", sha, tip],
                check=False,
                capture_output=True,
                text=True,
            )
            if ancestor.returncode == 0:
                return True
    return False


def collect_worktree_facts(
    workdir: Path, *, session_started_at: float
) -> list[WorktreeFact]:
    """Measure the current tree plus session-era Hermes subagent worktrees."""
    del session_started_at
    listing = _git(workdir, "worktree", "list", "--porcelain")
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*listing.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    resolved = workdir.resolve()
    if not any(Path(record.get("worktree", "")).resolve() == resolved for record in records):
        raise HermesContextGcRefused(f"current worktree is absent from git: {workdir}")
    facts = []
    for record in records:
        path = Path(record["worktree"])
        branch_ref = record.get("branch", "")
        sha = record.get("HEAD", "")
        is_current_named_tree = (
            path.resolve() == resolved
            and bool(branch_ref)
            and not branch_ref.startswith("refs/heads/hermes-subagent/")
            and not any(part.startswith("subagent-") for part in path.parts)
        )
        dirty = tuple(
            line
            for line in _git(path, "status", "--porcelain", "--untracked-files=all").splitlines()
            if line
        )
        facts.append(
            WorktreeFact(
                path=path,
                sha=sha,
                branch=branch_ref.removeprefix("refs/heads/"),
                dirty=dirty,
                accounted=(
                    is_current_named_tree or _commit_is_on_live_remote(path, sha)
                ),
            )
        )
    return facts


def load_card(card_id: str) -> dict:
    """Fetch one exact durable Cards row by id."""
    card_id = str(card_id or "").strip()
    if not card_id:
        raise HermesContextGcRefused("card lookup requires an exact id")
    env = os.environ.copy()
    env["SCITEX_DEV_CURRENCY_SEVERITY"] = "silent"
    completed = subprocess.run(
        [
            "scitex-cards",
            "list-tasks",
            "--scope",
            "",
            "--id-prefix",
            card_id,
            "--json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode:
        raise HermesContextGcRefused(
            completed.stderr.strip() or "scitex-cards lookup failed"
        )
    try:
        rows = json.loads(completed.stdout, strict=False)
    except (ValueError, TypeError) as exc:
        raise HermesContextGcRefused(f"active card lookup returned invalid JSON: {exc}") from exc
    exact = [row for row in rows if isinstance(row, dict) and row.get("id") == card_id]
    if len(exact) != 1:
        raise HermesContextGcRefused(
            f"active card {card_id!r} resolved to {len(exact)} exact rows"
        )
    return exact[0]


def load_active_card(state_dir: Path) -> dict:
    """Read the tracked card id, then fetch that exact durable Cards row."""
    try:
        marker = json.loads((state_dir / ACTIVE_CARD_FILE).read_text(encoding="utf-8"))
        card_id = str(marker.get("card_id") or "").strip()
    except (OSError, ValueError, TypeError) as exc:
        raise HermesContextGcRefused(f"active card identity is unavailable: {exc}") from exc
    return load_card(card_id)


def _consume_completion_boundary(
    *,
    state_dir: Path,
    agent_name: str,
    workdir: Path,
    live_id: str,
    card_by_id_reader: Callable[[str], dict],
    worktree_reader: Callable[..., list[WorktreeFact]],
    transition_guard: Callable[[Path, str], None],
    close_live: Callable[[Path, str], None],
) -> str | None:
    """Validate and consume one completion boundary under the event-writer lock."""
    fresh_marker = state_dir / FRESH_NEXT_TASK_FILE
    with _lifecycle_lock(state_dir):
        if not fresh_marker.exists():
            return None
        try:
            marker = json.loads(fresh_marker.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise HermesContextGcRefused(
                f"fresh-next-task marker is unreadable: {exc}"
            ) from exc
        if (
            not isinstance(marker, dict)
            or marker.get("reason") != "task-completed"
            or marker.get("phase") != "task-completed"
            or not str(marker.get("card_id") or "").strip()
            or not str(marker.get("delivery_id") or "").strip()
            or not str(marker.get("session_id") or "").strip()
            or marker.get("was_active") is not True
        ):
            raise HermesContextGcRefused("fresh-next-task marker is malformed")
        if (state_dir / ACTIVE_CARD_FILE).exists():
            raise HermesContextGcRefused(
                "a newer active card exists; refusing stale completion boundary"
            )
        bound_session = str(marker.get("session_id") or "").strip()
        if bound_session != live_id:
            _mark_completion_consumed(state_dir, str(marker["delivery_id"]))
            fresh_marker.unlink(missing_ok=True)
            return None
        completed_card = card_by_id_reader(str(marker["card_id"]))
        current_owner = str(
            completed_card.get("owner")
            or completed_card.get("assignee")
            or completed_card.get("assigned_to")
            or completed_card.get("agent")
            or ""
        ).strip()
        if (
            str(completed_card.get("id") or "") != marker["card_id"]
            or str(completed_card.get("status") or "").lower() != "done"
            or current_owner != agent_name
        ):
            raise HermesContextGcRefused(
                "Cards no longer proves this exact completed ownership boundary"
            )
        transition_guard(state_dir, live_id)
        completion_facts = worktree_reader(workdir, session_started_at=0)
        _validate_worktrees(completion_facts, workdir)
        marker_now = json.loads(fresh_marker.read_text(encoding="utf-8"))
        if marker_now != marker or (state_dir / ACTIVE_CARD_FILE).exists():
            raise HermesContextGcRefused(
                "completion boundary was superseded before close"
            )
        transition_guard(state_dir, live_id)
        _write_handoff(fresh_marker, {**marker, "phase": "closing"})
        close_live(state_dir, live_id)
        _write_handoff(
            fresh_marker,
            {**marker, "phase": "closed-awaiting-fresh"},
        )
        return ""


def complete_pending_completion(state_dir: Path, session: dict) -> None:
    """Consume a completion cut only after a different fresh session attaches."""
    marker_path = state_dir / FRESH_NEXT_TASK_FILE
    with _lifecycle_lock(state_dir):
        if not marker_path.exists():
            return
        try:
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise HermesContextGcRefused(
                f"completion boundary is unreadable during attachment: {exc}"
            ) from exc
        fresh_live_id = str(session.get("id") or "").strip()
        old_live_id = str(marker.get("session_id") or "").strip()
        if (
            marker.get("phase") != "closed-awaiting-fresh"
            or not fresh_live_id
            or fresh_live_id == old_live_id
        ):
            raise HermesContextGcRefused(
                "fresh session attachment does not complete the pending task boundary"
            )
        _mark_completion_consumed(state_dir, str(marker.get("delivery_id") or ""))
        _unlink_durable(marker_path)


def reconcile_context_lifecycle(
    *,
    state_dir: Path,
    agent_name: str,
    workdir: Path,
    observed_session: dict,
    engine_model: str = "",
    engine_provider: str = "",
    stored_record_reader: Callable[[Path, str], dict] | None = None,
    handoff_facts_reader: Callable[..., tuple[dict, list[dict]]] | None = None,
    card_reader: Callable[[Path], dict] | None = None,
    card_by_id_reader: Callable[[str], dict] | None = None,
    worktree_reader: Callable[..., list[WorktreeFact]] | None = None,
    rotate_session: Callable[..., str] | None = None,
    close_live: Callable[[Path, str], None] | None = None,
    transition_guard: Callable[[Path, str], None] | None = None,
    nonce_factory: Callable[[], str] = lambda: secrets.token_hex(16),
) -> str | None:
    """Return a replacement stored id, ``""`` for fresh, or ``None`` to continue."""
    from ._hermes_context_rpc import (
        close_session as default_close_session,
    )
    from ._hermes_context_rpc import (
        replace_session_from_handoff as default_replace_session,
    )
    from ._hermes_context_rpc import (
        session_handoff_facts as default_handoff_facts,
    )
    from ._hermes_context_rpc import (
        session_transition_guard as default_transition_guard,
    )
    from ._hermes_context_rpc import (
        stored_session_record as default_stored_record,
    )

    stored_record_reader = stored_record_reader or default_stored_record
    handoff_facts_reader = handoff_facts_reader or default_handoff_facts
    card_reader = card_reader or load_active_card
    card_by_id_reader = card_by_id_reader or load_card
    worktree_reader = worktree_reader or collect_worktree_facts
    rotate_session = rotate_session or default_replace_session
    close_live = close_live or default_close_session
    transition_guard = transition_guard or default_transition_guard

    live_id = str(observed_session.get("id") or "").strip()
    stored_id = str(
        observed_session.get("session_key") or observed_session.get("id") or ""
    ).strip()
    if not live_id or not stored_id:
        raise HermesContextGcRefused("observed Hermes session has no live/stored identity")
    if str(observed_session.get("status") or "").strip().lower() != "idle":
        raise HermesContextGcRefused(
            f"Hermes session {live_id!r} is not idle; refusing lifecycle mutation"
        )
    if (state_dir / FRESH_NEXT_TASK_FILE).exists():
        return _consume_completion_boundary(
            state_dir=state_dir,
            agent_name=agent_name,
            workdir=workdir,
            live_id=live_id,
            card_by_id_reader=card_by_id_reader,
            worktree_reader=worktree_reader,
            transition_guard=transition_guard,
            close_live=close_live,
        )
    record = stored_record_reader(state_dir, stored_id)
    if not str(record.get("compression_failure_error") or "").strip():
        return None
    evidence, active_subagents = handoff_facts_reader(
        state_dir, live_id, workdir=workdir
    )
    if active_subagents:
        names = ", ".join(
            str(row.get("subagent_id") or row.get("id") or "unknown")
            for row in active_subagents
        )
        raise HermesContextGcRefused(
            f"active subagents must finish and commit before handoff: {names}"
        )
    card = card_reader(state_dir)
    worktrees = worktree_reader(
        workdir, session_started_at=float(record.get("started_at") or time.time())
    )
    _current, initial_signature = _validate_worktrees(worktrees, workdir)
    task_id = str(card.get("id") or "").strip()

    def pre_close_check() -> None:
        transition_guard(state_dir, live_id)
        _evidence, still_active = handoff_facts_reader(
            state_dir, live_id, workdir=workdir
        )
        if still_active:
            raise HermesContextGcRefused(
                "a subagent became active during fresh-session proof"
            )
        refreshed = worktree_reader(
            workdir, session_started_at=float(record.get("started_at") or 0)
        )
        _refreshed_current, refreshed_signature = _validate_worktrees(
            refreshed, workdir
        )
        if refreshed_signature != initial_signature:
            raise HermesContextGcRefused(
                "worktree path/SHA set changed during fresh-session proof"
            )
        try:
            active_marker = json.loads(
                (state_dir / ACTIVE_CARD_FILE).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, TypeError) as exc:
            raise HermesContextGcRefused(
                f"active card marker changed during fresh-session proof: {exc}"
            ) from exc
        if active_marker.get("card_id") != task_id:
            raise HermesContextGcRefused(
                "active card changed during fresh-session proof"
            )
        transition_guard(state_dir, live_id)

    transition = handle_compression_failure(
        state_dir=state_dir,
        agent_name=agent_name,
        workdir=workdir,
        session_record={**record, "id": live_id},
        card=card,
        worktrees=worktrees,
        verification={
            "tests": str(evidence.get("status") or evidence.get("result") or "unknown")
        },
        next_nonce=nonce_factory,
        rotate=lambda handoff, nonce, old: rotate_session(
            state_dir,
            agent_name=agent_name,
            workdir=workdir,
            handoff_path=handoff,
            nonce=nonce,
            old_session_id=old,
            model=engine_model,
            provider=engine_provider,
            pre_close_check=pre_close_check,
        ),
    )
    return transition.session_id if transition is not None else None


__all__ = [
    "ACTIVE_CARD_FILE",
    "DEFAULT_MAX_SESSION_AGE_MINUTES",
    "FRESH_NEXT_TASK_FILE",
    "HANDOFF_DIRNAME",
    "HermesContextGcRefused",
    "SessionTransition",
    "WorktreeFact",
    "continuation_is_fresh_enough",
    "collect_worktree_facts",
    "handle_compression_failure",
    "load_active_card",
    "reconcile_context_lifecycle",
    "record_inbound_task_event",
]
