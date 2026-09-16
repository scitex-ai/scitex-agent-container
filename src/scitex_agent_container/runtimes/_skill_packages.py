"""Materialize only the canonical skill packages a spec declares."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

_MANIFEST = ".sac-skill-packages.json"


class SkillPackageError(RuntimeError):
    """A declared package cannot be exposed exactly as requested."""


def canonical_skills_root() -> Path:
    return Path.home() / ".scitex" / "dev" / "skills"


def _resolve_packages(package_ids: list[str], root: Path) -> dict[str, Path]:
    available = sorted(path.name for path in root.iterdir()) if root.is_dir() else []
    resolved: dict[str, Path] = {}
    for package_id in package_ids:
        entry = root / package_id
        if entry.is_symlink() and not entry.exists():
            raise SkillPackageError(
                f"skill package {package_id!r} is broken: {entry} is a dangling "
                "symlink. No fallback was materialized."
            )
        if not entry.exists():
            choices = ", ".join(available) or "(none)"
            raise SkillPackageError(
                f"skill package {package_id!r} is not installed at {entry}; "
                f"available packages: {choices}. No fallback was materialized."
            )
        target = entry.resolve()
        if not target.is_dir():
            raise SkillPackageError(
                f"skill package {package_id!r} is broken: {entry} does not resolve "
                "to a directory. No fallback was materialized."
            )
        dangling = next(
            (
                path
                for path in target.rglob("*")
                if path.is_symlink() and not path.exists()
            ),
            None,
        )
        if dangling is not None:
            raise SkillPackageError(
                f"skill package {package_id!r} is broken: {dangling} is a "
                "dangling symlink. No fallback was materialized."
            )
        if not any(path.is_file() for path in target.rglob("*.md")):
            raise SkillPackageError(
                f"skill package {package_id!r} is broken: {target} contains no "
                "Markdown skill files. No fallback was materialized."
            )
        resolved[package_id] = target
    return resolved


def materialize_skill_packages(
    config: Any,
    workspace_home: str | Path,
    *,
    source_root: Path | None = None,
    protected_names: set[str] | None = None,
) -> None:
    """Copy exactly ``config.skill_packages`` into the runtime skill directory.

    Package directories are the selection unit. File-level filtering is not
    supported because packages may contain relative references and auxiliary
    files; partially copying one would no longer expose the authored package.
    """
    package_ids = list(getattr(config, "skill_packages", []) or [])
    root = (source_root or canonical_skills_root()).expanduser()
    resolved = _resolve_packages(package_ids, root)
    collisions = sorted(set(resolved) & set(protected_names or ()))
    if collisions:
        raise SkillPackageError(
            "declared skill packages collide with spec.to_home skill entries: "
            f"{', '.join(collisions)}. Remove one declaration; sac will not "
            "choose one source silently."
        )
    skills_dir = Path(workspace_home) / ".claude" / "skills"
    manifest = skills_dir / _MANIFEST

    previous: list[str] = []
    if manifest.is_file():
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
            if isinstance(value, list):
                previous = [str(item) for item in value]
        except (OSError, ValueError):
            previous = []

    for package_id in previous:
        if Path(package_id).name != package_id or package_id in {".", ".."}:
            continue
        destination = skills_dir / package_id
        if destination.is_symlink() or destination.is_file():
            destination.unlink()
        elif destination.is_dir():
            shutil.rmtree(destination)

    if not package_ids:
        if manifest.exists():
            manifest.unlink()
        return

    skills_dir.mkdir(parents=True, exist_ok=True)
    for package_id in resolved:
        destination = skills_dir / package_id
        if destination.exists() or destination.is_symlink():
            raise SkillPackageError(
                f"cannot materialize declared skill package {package_id!r}: "
                f"{destination} already exists from spec.to_home or another "
                "layer. Remove the collision; sac will not overwrite it."
            )
    for package_id, target in resolved.items():
        destination = skills_dir / package_id
        shutil.copytree(target, destination)
    manifest.write_text(json.dumps(package_ids) + "\n", encoding="utf-8")


def retain_adjacent_to_home_skills(
    workspace_home: str | Path, adjacent_to_home: Path | None
) -> None:
    """Remove skill entries inherited from non-adjacent/shared layers.

    The portable definition may carry skill trees in its own adjacent
    ``to_home``. Shared baselines and old implicit host links are not part of
    that definition and must not survive a re-materialization.
    """
    skills_dir = Path(workspace_home) / ".claude" / "skills"
    if not skills_dir.is_dir():
        return
    source = (
        adjacent_to_home / ".claude" / "skills"
        if adjacent_to_home is not None
        else None
    )
    allowed = (
        {path.name for path in source.iterdir()}
        if source is not None and source.is_dir()
        else set()
    )
    allowed.add(_MANIFEST)
    for entry in skills_dir.iterdir():
        if entry.name in allowed:
            continue
        if entry.is_symlink() or entry.is_file():
            entry.unlink()
        elif entry.is_dir():
            shutil.rmtree(entry)


__all__ = [
    "SkillPackageError",
    "canonical_skills_root",
    "materialize_skill_packages",
    "retain_adjacent_to_home_skills",
]
