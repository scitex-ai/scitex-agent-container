"""Post-build retention for timestamped SIF artefacts.

Every successful ``sac image build`` lands a fresh timestamped SIF and
swaps the stable ``sac-<layer>.sif`` boot symlink to it — but nothing
ever removed the superseded timestamped files, so daily bakes
accumulated ~7 GB each until the host root filled (measured 2026-10-23
on scitex-compute-03: 4 superseded SIFs = 18 GB, host at 100%, while the
hourly disk guard prunes docker only and reclaimed 0 B).

After a build succeeds, :func:`retire_superseded_builds` keeps the
newest ``keep`` timestamped SIFs per layer in place and moves the rest
into ``<artifact_dir>/archive/``. Same-filesystem rename (instant, no
copy); the stable boot symlink is never touched; an archive name
collision is left in place rather than clobbered. Archive purging stays
manual — this function never deletes bytes, so a wrong call is
recoverable by moving files back.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["retire_superseded_builds"]


def retire_superseded_builds(
    artifact_dir: Path,
    layer: str,
    *,
    keep: int = 2,
    archive_name: str = "archive",
) -> list[Path]:
    """Archive superseded timestamped SIFs, newest ``keep`` stay live.

    Returns the archived paths (empty when there is nothing to retire).
    """
    if keep < 1:
        raise ValueError(f"keep must be >= 1, got {keep!r}")
    if not artifact_dir.is_dir():
        return []
    candidates = sorted(
        (
            p
            for p in artifact_dir.glob(f"sac-{layer}-*.sif")
            if p.is_file() and not p.is_symlink()
        ),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    doomed = candidates[:-keep] if len(candidates) > keep else []
    if not doomed:
        return []
    archive = artifact_dir / archive_name
    archive.mkdir(parents=True, exist_ok=True)
    moved: list[Path] = []
    for path in doomed:
        dest = archive / path.name
        if dest.exists():
            continue
        path.rename(dest)
        moved.append(dest)
    return moved
