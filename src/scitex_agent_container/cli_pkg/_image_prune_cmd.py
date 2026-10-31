"""``sac image prune`` — SIF retention (keep-N newest + current symlink target).

Card ``infra-disk-hygiene-mechanize-20260929`` gap 1: ``sac image`` could
build/distribute/list/rollback but never delete, so hash-named dated
builds accumulated until a host root filled. The retention policy is the
already-decided one: keep 3 (rollback safety) with the current symlink
target always kept even when it is not among the newest.

Safety shape (mirrors ``sac image build``'s Refusing convention): the
command deletes NOTHING unless ``--yes`` is given; ``--dry-run`` prints
the plan; bare ``prune`` refuses. Only dated ``*.sif`` regular files
directly under a layer dir are ever candidates — symlinks, sidecar logs,
locks, build-contexts, sandboxes and hand-placed undated SIFs are never
touched. Each deleted SIF takes its ``.sha256`` sidecar with it.
"""

from __future__ import annotations

import re
from pathlib import Path

import click

from .._logging import render_rich

# Dated build artefacts look like ``sac-base-2026-1015-180450.sif``.
# A hand-placed custom SIF (``my-test.sif``) never matches and is never
# a prune candidate, no matter how old it is.
DATED_SIF = re.compile(r"-20\d\d-\d+.*\.sif$")

DEFAULT_KEEP = 3


def plan_image_prune(
    files: list[tuple[str, float]], *, current: str | None, keep: int = 3
) -> tuple[list[str], list[str]]:
    """Split dated SIF names into ``(keep, prune)`` (pure, no IO).

    ``files`` is ``(name, mtime)`` for the layer's dated regular files.
    ``current`` (the symlink target's name, or ``None`` when the link is
    absent/dangling) is always kept. The remaining ``keep - 1`` slots go
    to the newest by mtime.
    """
    if keep < 1:
        raise ValueError("--keep must be >= 1")
    names = [name for name, _ in sorted(files, key=lambda item: item[1])]
    kept: list[str] = []
    if current is not None and current in names:
        kept.append(current)
    for name in reversed(names):
        if len(kept) >= keep:
            break
        if name not in kept:
            kept.append(name)
    return kept, [name for name in names if name not in kept]


def _scan_layer(layer_dir: Path) -> tuple[str | None, list[tuple[str, float]]]:
    """Read one layer dir: current target name + dated ``(name, mtime)``."""
    current: str | None = None
    link = layer_dir.parent / (layer_dir.name + ".sif")
    try:
        if link.is_symlink():
            target = link.resolve()
            if target.parent == layer_dir:
                current = target.name
    except OSError:
        current = None
    files: list[tuple[str, float]] = []
    try:
        entries = list(layer_dir.iterdir())
    except OSError:
        return current, files
    for entry in entries:
        try:
            if not entry.is_file() or entry.is_symlink():
                continue
            if not DATED_SIF.search(entry.name):
                continue
            files.append((entry.name, entry.stat().st_mtime))
        except OSError:
            continue
    return current, files


@click.command("prune")
@click.option(
    "--keep",
    type=int,
    default=DEFAULT_KEEP,
    show_default=True,
    help="Dated SIFs retained per layer (current target always kept).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the retention plan without deleting anything.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Actually delete prune candidates (+ their .sha256 sidecars).",
)
def image_prune(keep: int, dry_run: bool, yes: bool) -> None:
    """Delete dated SIFs beyond keep-N + the current target, per layer.

    Example:
      $ sac image prune --dry-run
      $ sac image prune --keep 5 --yes
    """
    from . import image_group as ig

    if keep < 1:
        raise click.UsageError("--keep must be >= 1")
    if not dry_run and not yes:
        raise click.UsageError(
            "Refusing to delete SIFs without --dry-run or --yes. "
            "Run `sac image prune --dry-run` to review the plan first."
        )
    ig._ensure_containers_dir()
    root = ig._SCITEX_USER_STATE_ROOT
    try:
        layer_dirs = sorted(
            p for p in root.glob("*/containers/*") if p.is_dir()
        )
    except OSError as exc:
        raise click.ClickException(f"Cannot scan image store: {exc}") from exc
    total_bytes = 0
    total_files = 0
    for layer_dir in layer_dirs:
        current, files = _scan_layer(layer_dir)
        if not files:
            continue
        kept, candidates = plan_image_prune(
            files, current=current, keep=keep
        )
        render_rich(
            f"[dim]{layer_dir.parent.parent.name}/{layer_dir.name}/ "
            f"(keep {len(kept)}, prune {len(candidates)})[/dim]",
            __name__,
        )
        for name in kept:
            marker = "  (current)" if name == current else ""
            render_rich(f"  KEEP   {name}{marker}", __name__)
        for name in candidates:
            candidate = layer_dir / name
            try:
                size = candidate.stat().st_size
            except OSError:
                size = 0
            render_rich(
                f"  PRUNE  {name}  ({size / (1024 * 1024):.1f} MB)", __name__
            )
            if not dry_run:
                try:
                    candidate.unlink()
                except OSError as exc:
                    raise click.ClickException(
                        f"Cannot delete {candidate}: {exc}"
                    ) from exc
                sidecar = layer_dir / (name + ".sha256")
                try:
                    if sidecar.is_file() and not sidecar.is_symlink():
                        sidecar.unlink()
                except OSError as exc:
                    raise click.ClickException(
                        f"Cannot delete {sidecar}: {exc}"
                    ) from exc
            total_bytes += size
            total_files += 1
    if dry_run:
        render_rich(
            f"[dim]dry-run: {total_files} file(s), "
            f"{total_bytes / (1024 * 1024):.1f} MB reclaimable "
            f"(nothing deleted; re-run with --yes)[/dim]",
            __name__,
        )
        return
    render_rich(
        f"pruned {total_files} file(s), "
        f"{total_bytes / (1024 * 1024):.1f} MB reclaimed",
        __name__,
    )
