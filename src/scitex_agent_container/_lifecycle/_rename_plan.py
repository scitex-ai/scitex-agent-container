#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""What a rename WOULD touch — the layout, the preflight, and the plan.

Read-only half of ``sac agents rename``. Nothing in this module mutates
anything, which is what makes ``--dry-run`` trustworthy: the dry run and
the real run compute the SAME plan from the SAME code, then one of them
stops.

:mod:`._rename` owns the other half — executing a plan, reversibly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ._rename_cards import find_foreign_scoped_cards, find_owned_cards
from ._rename_db import count_rows
from ._rename_spec import SpecChange, plan_spec_changes

# Same rule ``sac agents create`` enforces: the name is a directory name.
_VALID_NAME_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-_")

# Operator/test port: point the whole rename at a different sac root.
# Read at CALL time (see Layout.default) — never captured at import.
ROOT_ENV = "SCITEX_AGENT_CONTAINER_ROOT"


class RenameError(RuntimeError):
    """The rename was refused, or failed and was rolled back."""


@dataclass(frozen=True)
class Layout:
    """Where sac keeps an agent's things, rooted at an injectable base.

    Every path derives from ``root``, so a caller — a test, or an operator
    with a non-standard install — can point the whole rename somewhere
    else. Deliberately NOT read from the module-level constants elsewhere
    in sac (``Registry.REGISTRY_DIR``, ``_session_state.DEFAULT_STATE_ROOT``,
    ``state_db.DEFAULT_DB_PATH``): those are computed from ``$HOME`` at
    IMPORT time, so a fixture that sets ``$HOME`` afterwards CANNOT redirect
    them. A test that trusted that would look isolated while reading — and
    writing — the live fleet.
    """

    root: Path

    @classmethod
    def default(cls) -> "Layout":
        """The production root, or ``$SCITEX_AGENT_CONTAINER_ROOT`` if set.

        Resolved on every call, so the override actually takes effect (an
        import-time constant would not).
        """
        override = os.environ.get(ROOT_ENV, "").strip()
        if override:
            return cls(root=Path(override).expanduser())
        return cls(root=Path.home() / ".scitex" / "agent-container")

    @property
    def state_db(self) -> Path:
        return self.root / "runtime" / "state.db"

    def spec_dir(self, name: str) -> Path:
        return self.root / "agents" / name

    def spec_file(self, name: str) -> Path:
        return self.spec_dir(name) / "spec.yaml"

    def overlay_dir(self, name: str) -> Path:
        return self.root / "containers" / "overlays" / name

    def runtime_dir(self, name: str) -> Path:
        return self.root / "runtime" / name

    def registry_json(self, name: str) -> Path:
        return self.root / "runtime" / "registry" / f"{name}.json"


@dataclass(frozen=True)
class Move:
    """A directory or file the rename would move."""

    src: Path
    dst: Path


@dataclass
class RenamePlan:
    """Everything the rename would touch. Built without mutating anything."""

    old: str
    new: str
    layout: Layout
    spec_move: Move
    spec_changes: list[SpecChange] = field(default_factory=list)
    overlay_move: Move | None = None
    runtime_move: Move | None = None
    registry_move: Move | None = None
    db_counts: dict[str, int] = field(default_factory=dict)
    card_ids: list[str] = field(default_factory=list)
    cards_enabled: bool = True
    warnings: list[str] = field(default_factory=list)

    @property
    def db_total(self) -> int:
        return sum(self.db_counts.values())


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """True when a process with ``pid`` currently exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # It exists; we merely may not signal it.
        return True
    return True


def _open_instance_pid(db_path: Path, name: str) -> int | None:
    """Return the pid of an open ``instances`` row for ``name``, if any."""
    if not db_path.is_file():
        return None
    # Through the OWNING module, not through its table. The raw SELECT this
    # replaces would keep reading a SQLite ``instances`` table after that
    # table moves backend, and would report "not running" for every agent
    # rather than failing — the same silent-stranding shape found in
    # ``_authheal/_specimen`` during the sqlite->PostgreSQL migration.
    #
    # ``list_active_instances`` already applies ``ended_at IS NULL`` and
    # orders by ``started_at DESC``, so only this function's two extra
    # conditions remain here: the name, and a pid that is actually recorded.
    from .._state.state_db_instances import list_active_instances

    try:
        rows = list_active_instances()
    except Exception:  # stx-allow: fallback (reason: a fresh DB has no instances table — absence of the table is absence of evidence, not evidence of running. Kept deliberately broad: the raw version caught sqlite3.Error, and the accessor may raise a different type per backend, so narrowing it here would turn a fresh database into a crash mid-rename.)
        return None
    for row in rows:
        if row.get("name") == name and row.get("pid") is not None:
            return int(row["pid"])
    return None


def probe_running(name: str, layout: Layout) -> tuple[str, str]:
    """Return ``(verdict, reason)`` — ``running`` / ``stopped`` / ``unknown``.

    Renaming a LIVE agent's workdir, overlay and state dir out from under
    it is unsafe, so only a definitive ``stopped`` may proceed; ``unknown``
    refuses too.

    The evidence is PHYSICAL — a live PID, never a row that merely CLAIMS
    to be open. That matters in both directions:

      * a stale ``instances`` row (the reaper closes rows lazily) must not
        block a legitimate rename forever, so an open row whose pid is DEAD
        is not evidence of running;
      * a wedged-but-alive agent still holds its overlay open, so a live
        pid IS evidence of running even when the agent makes no progress.
    """
    pid_file = layout.runtime_dir(name) / "pid"
    if pid_file.is_file():
        raw = pid_file.read_text(encoding="utf-8").strip()
        try:
            pid = int(raw)
        except ValueError:
            return (
                "unknown",
                f"{pid_file} exists but holds no pid ({raw!r}); cannot prove "
                "the agent is stopped",
            )
        if _pid_alive(pid):
            return "running", f"pid {pid} (from {pid_file}) is alive"

    open_pid = _open_instance_pid(layout.state_db, name)
    if open_pid is not None and _pid_alive(open_pid):
        return (
            "running",
            f"state.db has an open instances row for {name!r} whose pid "
            f"{open_pid} is alive",
        )

    return "stopped", ""


def preflight(old: str, new: str, layout: Layout) -> None:
    """Refuse the rename, loudly, if any precondition is unmet."""
    if not new or not all(ch in _VALID_NAME_CHARS for ch in new):
        raise RenameError(
            f"invalid agent name {new!r}: use lowercase letters, digits, '-' "
            "and '_' only (the name is a directory name)"
        )
    if old == new:
        raise RenameError(f"{old!r} and {new!r} are the same name — nothing to do")

    if not layout.spec_dir(old).is_dir():
        raise RenameError(
            f"agent {old!r} not found: {layout.spec_dir(old)} does not exist"
        )
    if not layout.spec_file(old).is_file():
        raise RenameError(
            f"agent {old!r} has no spec at {layout.spec_file(old)} — refusing "
            "to rename a directory sac cannot load"
        )

    for label, path in (
        ("spec dir", layout.spec_dir(new)),
        ("overlay dir", layout.overlay_dir(new)),
        ("runtime dir", layout.runtime_dir(new)),
        ("registry entry", layout.registry_json(new)),
    ):
        if path.exists():
            raise RenameError(
                f"{new!r} already exists ({label}: {path}). Pick another name, "
                f"or remove it first:  sac agents delete {new}"
            )

    verdict, reason = probe_running(old, layout)
    if verdict != "stopped":
        raise RenameError(
            f"agent {old!r} is {verdict} ({reason}). Renaming a live agent's "
            "workdir/overlay/state dir out from under it is unsafe.\n"
            f"    Stop it first:  sac agents stop {old}"
        )


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def build_plan(
    old: str,
    new: str,
    *,
    layout: Layout | None = None,
    store: str | Path | None = None,
    cards: bool = True,
) -> RenamePlan:
    """Compute everything the rename would touch. Mutates NOTHING.

    Runs :func:`preflight` first: ``--dry-run`` must tell the operator the
    rename would be REFUSED before they commit to it, not after.
    """
    layout = layout or Layout.default()
    preflight(old, new, layout)

    plan = RenamePlan(
        old=old,
        new=new,
        layout=layout,
        spec_move=Move(layout.spec_dir(old), layout.spec_dir(new)),
        cards_enabled=cards,
    )

    spec_text = layout.spec_file(old).read_text(encoding="utf-8")
    plan.spec_changes = plan_spec_changes(spec_text, old, new)
    if not plan.spec_changes:
        plan.warnings.append(
            f"the spec never names {old!r} — nothing to rewrite inside it "
            "(only the directory moves)"
        )

    for label, src, dst, slot in (
        ("overlay", layout.overlay_dir(old), layout.overlay_dir(new), "overlay_move"),
        ("runtime", layout.runtime_dir(old), layout.runtime_dir(new), "runtime_move"),
        (
            "registry",
            layout.registry_json(old),
            layout.registry_json(new),
            "registry_move",
        ),
    ):
        if src.exists():
            setattr(plan, slot, Move(src, dst))
        else:
            plan.warnings.append(f"no {label} at {src} — skipping that step")

    plan.db_counts = count_rows(layout.state_db, old)

    if cards:
        plan.card_ids = find_owned_cards(old, store=store)
        _warn_about_foreign_scoped_cards(plan, store=store)
    else:
        plan.warnings.append(
            "--no-cards: the board will NOT be migrated. Every card owned by "
            f"{old!r} is ORPHANED — the agent under its new name cannot see "
            "its own work, and nothing will tell you."
        )

    _warn_about_workdir(plan)
    return plan


def _warn_about_foreign_scoped_cards(
    plan: RenamePlan, *, store: str | Path | None
) -> None:
    """Report cards scoped ``agent:<old>`` that ``old`` does not own.

    sac will NOT reassign these: their owner is someone else, and taking a
    card from a working agent to tidy up a scope string is not a trade this
    verb gets to make. Say what is being left behind instead of silently
    picking one of the two wrong answers.
    """
    foreign = find_foreign_scoped_cards(plan.old, store=store)
    if not foreign:
        return
    plan.warnings.append(
        f"{len(foreign)} card(s) are scoped 'agent:{plan.old}' but OWNED BY "
        f"SOMEONE ELSE ({', '.join(foreign[:5])}). sac will not take another "
        f"agent's card, so their scope will still say 'agent:{plan.old}' "
        "afterwards. Fix them on the board if that is wrong."
    )


def _warn_about_workdir(plan: RenamePlan) -> None:
    """Flag a rewritten workdir whose new target is not on disk.

    sac renames the AGENT, not the repo. When the spec's workdir carried
    the agent's name (the fleet's project-maintainer convention,
    ``~/proj/<name>``) the rewrite follows it — but if the repo has not
    been renamed too, the agent would start with a workdir that is not
    there. Say so now, not at the next start.
    """
    for change in plan.spec_changes:
        if change.path != "spec.workdir":
            continue
        target = Path(os.path.expanduser(change.after))
        if not target.exists():
            plan.warnings.append(
                f"spec.workdir will point at {change.after}, which does not "
                "exist yet. sac renames the AGENT, not the repo — rename the "
                "repo too, or edit spec.workdir afterwards."
            )


__all__ = [
    "ROOT_ENV",
    "Layout",
    "Move",
    "RenameError",
    "RenamePlan",
    "build_plan",
    "preflight",
    "probe_running",
]
