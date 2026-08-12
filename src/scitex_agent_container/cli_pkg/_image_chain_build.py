"""Pure helpers for the staged ``sac image build`` — no click, no side seams.

``image_group.image_build`` keeps the click surface AND the swap-and-restore
build seams (``_build_layer_from_source`` / ``_run_reproducible_build``) that
tests reassign; everything here is decision logic and filesystem bookkeeping
that can be unit-tested without a CLI runner or a fake backend.

The one piece with real consequences is :func:`publish_compat_aliases`. See its
docstring — without it, renaming ``sac-base.sif`` is a fleet outage.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

from ._image_layers import Layer


def existing_artifact_notice(
    out_dir: Path, layer: Layer, *, sandbox: bool
) -> str | None:
    """Warn that a build will replace an artifact that is already there.

    Returns the message, or ``None`` when nothing exists yet.

    A SIF rebuild is ATOMIC (delegated to scitex-container's ``build``): it
    lands a fresh timestamped SIF and swaps the stable ``<image>.sif`` boot
    symlink all-at-once, so on success the live image is replaced but on
    failure the prior one is left intact (no in-place clobber). A sandbox
    rebuild is still in place, and the wording says so.
    """
    artifact_dir = out_dir / layer.image
    existing = artifact_dir / (
        f"{layer.image}.sandbox" if sandbox else f"{layer.image}.sif"
    )
    if not existing.exists():
        return None
    size_mb = existing.stat().st_size / (1024 * 1024) if existing.is_file() else 0
    mtime = _dt.datetime.fromtimestamp(existing.stat().st_mtime).isoformat(
        timespec="seconds"
    )
    kind = "sandbox dir" if sandbox else "SIF"
    verb = "overwritten" if sandbox else "replaced (atomic swap)"
    return (
        f"⚠  Existing {kind} at {existing} "
        f"({size_mb:.0f} MB, built {mtime}) will be {verb}."
    )


def publish_compat_aliases(out_dir: Path, layer: Layer) -> Path | None:
    """Publish ``<legacy_image>.sif`` beside the stage's real artifact.

    THE RENAME IS ONLY SAFE BECAUSE OF THIS. The four-stage split renames the
    published artifacts (``sac-base.sif`` → ``sac-03-base.sif``,
    ``sac-scitex.sif`` → ``sac-04-scitex.sif``), and agent specs hold the
    ABSOLUTE PATH of the old name — 65+ specs on this host, 157 references to
    the base image across a 114-spec census. A spec pointing at a path that
    stopped existing is a dead agent, not a warning, so each renamed stage
    keeps its old name as a symlink to the new artifact.

    RELATIVE symlink target on purpose: the containers dir is rsync'd between
    hosts (the Spartan bake → master pull), and an absolute target would point
    at the BUILD host's filesystem after the copy.

    Returns the alias path, or ``None`` when the stage has no legacy name.
    Never raises on a pre-existing alias — it is replaced atomically, so a
    concurrent reader never sees a missing symlink.
    """
    if not layer.legacy_image:
        return None
    alias = out_dir / f"{layer.legacy_image}.sif"
    target = f"{layer.image}.sif"
    tmp = out_dir / f".{layer.legacy_image}.sif.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    tmp.symlink_to(target)
    tmp.replace(alias)
    return alias


def missing_parent_message(layer: Layer, parent_sif: Path) -> str:
    """Remediation text for a layered build whose parent SIF is absent.

    Names the missing path AND the exact command that produces it — a build
    that dies saying only "no such file" makes the reader go find the chain.
    """
    return (
        f"{layer.name} bootstraps from {parent_sif.name}, which is not built "
        f"(looked at {parent_sif}).\n"
        f"  Build the parent:  sac image build {layer.parent} -y\n"
        f"  Or the whole chain: sac image build {layer.name} --chain -y"
    )


__all__ = [
    "existing_artifact_notice",
    "missing_parent_message",
    "publish_compat_aliases",
]
