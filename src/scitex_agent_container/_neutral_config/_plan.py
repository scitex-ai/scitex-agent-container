"""Deployment actions: plan, check and apply (ADR-0033 §Deploy).

Adapters return a list of actions; this module compares each against the
target and applies it. Nothing is deleted: a real file or directory in the
way is MOVED into ``<target>/.sac-backup/<stamp>/`` first, and a symlink in
the way is replaced (its old target is reported). A manifest
(``<target>/.sac-agents-config.json``) records what deploy manages, so an
entry later removed from the source is unlinked on the next deploy, and
unmanaged entries (e.g. a ``skills/scitex`` link owned by scitex-dev) are
never touched.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

MANIFEST = ".sac-agents-config.json"
BACKUP_DIR = ".sac-backup"

OK = "ok"
CREATE = "create"
UPDATE = "update"
STALE = "stale"


@dataclass(frozen=True)
class Link:
    """``dest`` must be a symlink pointing at ``source``."""

    dest: Path
    source: Path

    def state(self) -> str:
        if os.path.islink(self.dest):
            return OK if os.readlink(self.dest) == str(self.source) else UPDATE
        return UPDATE if os.path.lexists(self.dest) else CREATE

    def describe(self) -> str:
        return f"link {self.dest} -> {self.source}"


@dataclass(frozen=True)
class WriteFile:
    """``dest`` must be a regular file with exactly ``content``."""

    dest: Path
    content: str

    def state(self) -> str:
        if os.path.islink(self.dest) or not self.dest.is_file():
            return UPDATE if os.path.lexists(self.dest) else CREATE
        return OK if self.dest.read_text(encoding="utf-8") == self.content else UPDATE

    def describe(self) -> str:
        return f"write {self.dest}"


Action = Link | WriteFile


@dataclass(frozen=True)
class Result:
    action: Action | None
    state: str
    note: str = ""

    def line(self) -> str:
        what = self.action.describe() if self.action else self.note
        extra = f"  ({self.note})" if self.action and self.note else ""
        return f"[{self.state}] {what}{extra}"


def _manifest_path(target: Path) -> Path:
    return target / MANIFEST


def read_manifest(target: Path) -> list[str]:
    path = _manifest_path(target)
    if not path.is_file():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("managed", []))


def _stale(target: Path, actions: list[Action]) -> list[Path]:
    wanted = {str(a.dest) for a in actions}
    return [
        Path(p) for p in read_manifest(target) if p not in wanted and os.path.islink(p)
    ]


def check(target: Path, actions: list[Action]) -> list[Result]:
    """Compare without changing anything."""
    results = [Result(a, a.state()) for a in actions]
    results += [
        Result(None, STALE, f"unlink {p} (no longer in source)")
        for p in _stale(target, actions)
    ]
    return results


def _move_aside(path: Path, backup_root: Path) -> Path:
    dest = backup_root / path.name
    n = 1
    while os.path.lexists(dest):
        dest = backup_root / f"{path.name}.{n}"
        n += 1
    backup_root.mkdir(parents=True, exist_ok=True)
    shutil.move(str(path), str(dest))
    return dest


def _clear(dest: Path, backup_root: Path) -> str:
    if os.path.islink(dest):
        old = os.readlink(dest)
        dest.unlink()
        return f"replaced link -> {old}"
    if os.path.lexists(dest):
        return f"moved aside to {_move_aside(dest, backup_root)}"
    return ""


def apply(target: Path, actions: list[Action]) -> list[Result]:
    """Make the target match; returns one result per action (+ stale unlinks)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = target / BACKUP_DIR / stamp
    results: list[Result] = []
    for action in actions:
        state = action.state()
        if state == OK:
            results.append(Result(action, OK))
            continue
        action.dest.parent.mkdir(parents=True, exist_ok=True)
        note = _clear(action.dest, backup_root)
        if isinstance(action, Link):
            os.symlink(action.source, action.dest)
        else:
            action.dest.write_text(action.content, encoding="utf-8")
        results.append(Result(action, state, note))
    for path in _stale(target, actions):
        path.unlink()
        results.append(Result(None, STALE, f"unlinked {path}"))
    target.mkdir(parents=True, exist_ok=True)
    _manifest_path(target).write_text(
        json.dumps({"managed": sorted(str(a.dest) for a in actions)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return results


__all__ = [
    "CREATE",
    "OK",
    "STALE",
    "UPDATE",
    "Action",
    "Link",
    "Result",
    "WriteFile",
    "apply",
    "check",
    "read_manifest",
]
