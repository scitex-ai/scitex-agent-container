"""Move host-only ``spec.uuid`` stamps from spec files into runtime state.

SAC < 0.40 stamped a ``uuid:`` line into a spec on START. On hosts whose
specs live in a git checkout those lines are usually WORKING-TREE-ONLY: the
committed file has no uuid, the host copy has one, and the live agent's
``instances`` rows are keyed on it. Since #1777 the start path keeps the
identity of a uuid-less spec in ``<state_dir>/spec_uuid`` instead. Deleting
the stray line by hand would make the next start mint a NEW runtime uuid and
fork the agent's identity.

:func:`migrate_spec_uuids` closes that gap per spec:

1. find ``spec.uuid`` in the working tree; none -> nothing to do;
2. compare with the file's git ``HEAD`` version. A uuid that HEAD also
   carries is AUTHORED and left untouched. Only a uuid HEAD lacks is a
   runtime stamp;
3. write that uuid to the agent's runtime state file (the same file
   :func:`._spec_uuid.runtime_spec_uuid` reads). A state file that already
   holds a DIFFERENT uuid is refused loudly: two identities exist and a
   human must pick one;
4. remove just that one line (line-level edit, no YAML round-trip).

Outside git (or for a spec not committed at HEAD) there is no committed
version to compare with, so the uuid is persisted to state and the line is
KEPT. Nothing here touches a running agent: it writes the state file and
the spec file only. Idempotent: a re-run after success finds no stamp.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from ._spec_uuid import RUNTIME_UUID_FILE, _write_state_uuid, parse_spec_uuid
from ._yaml_line_edit import find_block, find_key, parse_key_line, split_ending

__all__ = [
    "SpecUuidMigration",
    "default_state_dir_for_spec",
    "migrate_spec_uuids",
    "plan_spec_uuid_migration",
]

_GIT_TIMEOUT_S = 10

#: Per-spec outcomes. ``migrated`` / ``persisted`` are the only ones that
#: write (outside dry-run); ``refused`` and ``error`` make the run fail.
ACTIONS = (
    "no-uuid",  # spec carries no uuid line
    "authored",  # uuid is committed at HEAD — left untouched
    "migrated",  # working-tree-only stamp -> state file, line removed
    "persisted",  # no git HEAD to compare with -> state file, line kept
    "refused",  # state file holds a different uuid, or HEAD's uuid differs
    "error",  # unreadable / malformed spec
)


@dataclass
class SpecUuidMigration:
    """What happened (or, in dry-run, would happen) to one spec."""

    agent: str
    spec_path: str
    action: str
    uuid: str = ""
    state_path: str = ""
    state_before: str = ""
    line_removed: bool = False
    reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _locate_uuid(text: str) -> "tuple[list[str], list[str], int | None, str]":
    """``(bodies, endings, line_index, raw_value)`` of ``spec.uuid`` in ``text``."""
    lines = text.splitlines(keepends=True)
    bodies = [split_ending(line)[0] for line in lines]
    endings = [split_ending(line)[1] for line in lines]
    block = find_block(bodies, ("spec",))
    if block is None or block.child_indent is None:
        return bodies, endings, None, ""
    idx = find_key(bodies, block.start, block.stop, block.child_indent, "uuid")
    if idx is None:
        return bodies, endings, None, ""
    parsed = parse_key_line(bodies[idx])
    return bodies, endings, idx, (parsed.value.strip() if parsed else "")


def _git(cwd: Path, *args: str) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
        env={**os.environ, "GIT_DISCOVERY_ACROSS_FILESYSTEM": "1"},
    )


def committed_text(spec_path: Path) -> "tuple[str | None, str]":
    """The spec's content at git ``HEAD`` of its own repo, or ``(None, why)``."""
    real = spec_path.resolve()
    try:
        top = _git(real.parent, "rev-parse", "--show-toplevel")
    except (
        OSError,
        subprocess.SubprocessError,
    ) as exc:  # stx-allow: fallback (reason: no git means no committed version; the caller REPORTS this and keeps the line)
        return None, f"git unavailable: {exc}"
    if top.returncode != 0:
        return None, "not in a git repository"
    shown = _git(real.parent, "show", f"HEAD:./{real.name}")
    if shown.returncode != 0:
        return None, "spec is not committed at HEAD"
    return shown.stdout, ""


def default_state_dir_for_spec(spec_path: Path) -> Path:
    """The runtime state dir the START path uses for this spec."""
    from ..runtimes.tui_session import state_dir_for_config
    from . import load_config

    return state_dir_for_config(load_config(spec_path))


def _remove_line(spec_path: Path, bodies, endings, idx: int) -> None:
    """Drop line ``idx`` and rewrite the spec atomically, keeping its mode."""
    kept = [b + e for i, (b, e) in enumerate(zip(bodies, endings)) if i != idx]
    mode = spec_path.stat().st_mode & 0o7777
    tmp = spec_path.with_name(f".{spec_path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text("".join(kept))
        os.chmod(tmp, mode)
        os.replace(tmp, spec_path)
    finally:
        tmp.unlink(missing_ok=True)


def _migrate_one(
    spec_path: Path,
    *,
    dry_run: bool,
    state_dir_for: Callable[[Path], Path],
) -> SpecUuidMigration:
    agent = spec_path.parent.name
    out = SpecUuidMigration(agent=agent, spec_path=str(spec_path), action="error")
    try:
        text = spec_path.read_text()
        bodies, endings, idx, raw = _locate_uuid(text)
        if idx is None:
            out.action = "no-uuid"
            return out
        out.uuid = parse_spec_uuid(raw, path=str(spec_path))
        head_text, why = committed_text(spec_path)
        if head_text is not None:
            _, _, head_idx, head_raw = _locate_uuid(head_text)
            if head_idx is not None:
                head_uuid = parse_spec_uuid(head_raw, path=f"HEAD:{spec_path}")
                if head_uuid == out.uuid:
                    out.action = "authored"
                    out.reason = "uuid is committed at HEAD"
                    return out
                out.action = "refused"
                out.reason = (
                    f"working tree uuid {out.uuid} differs from the committed "
                    f"uuid {head_uuid}; resolve the spec by hand"
                )
                return out
        state_path = Path(state_dir_for(spec_path)) / RUNTIME_UUID_FILE
        out.state_path = str(state_path)
    except Exception as exc:  # stx-allow: fallback (reason: one unreadable spec becomes a SpecUuidMigration(action=error) row printed to stdout by `sac agents migrate-spec-uuids` and fails the run with exit 1; it must not hide the plan for every other spec)
        out.action = "error"
        out.reason = f"{type(exc).__name__}: {exc}"
        return out

    if state_path.is_file():
        out.state_before = state_path.read_text().strip()
        try:
            same = parse_spec_uuid(out.state_before, path=str(state_path)) == out.uuid
        except ValueError:  # stx-allow: fallback (reason: a malformed state file is a conflict and is REFUSED loudly just below)
            same = False
        if not same:
            out.action = "refused"
            out.reason = (
                f"runtime state {state_path} already holds {out.state_before!r}, "
                f"not the spec's {out.uuid}; two identities exist — pick one "
                f"by hand (nothing was written)"
            )
            return out

    remove = head_text is not None
    out.action = "migrated" if remove else "persisted"
    out.line_removed = remove
    if not remove:
        out.reason = f"{why}: uuid persisted to state, line kept"
    if dry_run:
        return out
    if not out.state_before:
        _write_state_uuid(state_path, out.uuid)
    if remove:
        _remove_line(spec_path, bodies, endings, idx)
    return out


def _spec_paths(agents_dir: Path, name: "str | None") -> "list[Path]":
    if name:
        spec = agents_dir / name / "spec.yaml"
        if not spec.is_file():
            raise FileNotFoundError(f"no spec for agent {name!r}: {spec}")
        return [spec]
    from .._reconcile._pass import fleet_spec_paths

    return fleet_spec_paths(agents_dir)


def migrate_spec_uuids(
    agents_dir: "str | Path",
    *,
    name: "str | None" = None,
    dry_run: bool = False,
    state_dir_for: "Callable[[Path], Path] | None" = None,
) -> "list[SpecUuidMigration]":
    """Move working-tree-only ``spec.uuid`` stamps into runtime state.

    ``name`` limits the run to one agent dir under ``agents_dir``; otherwise
    every fleet spec (``_``-prefixed scaffolding skipped). ``dry_run``
    computes the same plan and writes nothing. ``state_dir_for`` maps a spec
    path to its runtime state dir; the default is the one the START path
    uses (:func:`default_state_dir_for_spec`).
    """
    resolver = state_dir_for or default_state_dir_for_spec
    return [
        _migrate_one(spec, dry_run=dry_run, state_dir_for=resolver)
        for spec in _spec_paths(Path(agents_dir).expanduser(), name)
    ]


def plan_spec_uuid_migration(agents_dir, **kwargs) -> "list[SpecUuidMigration]":
    """:func:`migrate_spec_uuids` with ``dry_run=True``: the plan, no writes."""
    return migrate_spec_uuids(agents_dir, dry_run=True, **kwargs)
