"""Safe activation and rollback for SAC's layered image store.

The SAC store names an image ``sac-<layer>-<version>.sif`` and publishes it
through two stable links::

    sac-<layer>/sac-<layer>.sif -> sac-<layer>-<version>.sif
    sac-<layer>.sif             -> sac-<layer>/sac-<layer>-<version>.sif

These helpers own that layout.  The generic ``scitex-container`` version
helpers use ``current.sif`` / ``scitex-v<version>.sif`` and therefore cannot
represent a SAC layer.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ._image_build_lock import image_build_lock

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
LAYERS = ("base", "scitex", "proxy")


def _link_paths(containers_dir: Path, layer: str) -> tuple[Path, Path]:
    image_name = f"sac-{layer}"
    layer_dir = containers_dir / image_name
    return layer_dir / f"{image_name}.sif", containers_dir / f"{image_name}.sif"


def _link_state(link: Path) -> str | None:
    """Return a link's literal target; refuse to treat a file as a link."""
    if link.is_symlink():
        return os.readlink(link)
    if link.exists():
        raise RuntimeError(f"refusing to replace non-symlink live path: {link}")
    return None


def _atomic_symlink(link: Path, target: str) -> None:
    """Replace one symlink atomically on its filesystem."""
    tmp = link.parent / f".{link.name}.switch-tmp.{os.getpid()}"
    if tmp.is_symlink() or tmp.is_file():
        tmp.unlink()
    elif tmp.exists():
        raise RuntimeError(f"refusing to remove non-file temporary path: {tmp}")
    tmp.symlink_to(target)
    os.replace(tmp, link)


def _restore_link(link: Path, target: str | None) -> None:
    if target is None:
        if link.is_symlink():
            link.unlink()
        elif link.exists():
            raise RuntimeError(f"refusing to remove non-symlink live path: {link}")
        return
    _atomic_symlink(link, target)


def _resolved_live_target(inner: Path, top: Path) -> Path | None:
    """Return the shared live artifact, or fail on a split/missing pair."""
    inner_state = _link_state(inner)
    top_state = _link_state(top)
    if inner_state is None and top_state is None:
        return None
    if inner_state is None or top_state is None:
        raise RuntimeError(
            "live SAC image links are incomplete; refusing to guess: "
            f"inner={inner_state!r}, top={top_state!r}"
        )
    try:
        inner_target = inner.resolve(strict=True)
        top_target = top.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"live SAC image link is dangling: {exc.filename}") from exc
    if inner_target != top_target:
        raise RuntimeError(
            "live SAC image links disagree; refusing activation: "
            f"inner={inner_target}, top={top_target}"
        )
    return inner_target


def _version_from_name(layer: str, name: str) -> str:
    prefix = f"sac-{layer}-"
    if not name.startswith(prefix) or not name.endswith(".sif"):
        raise RuntimeError(f"non-canonical SAC {layer} artifact: {name}")
    return name[len(prefix) : -len(".sif")]


def _activate_unlocked(containers_dir: Path, layer: str, target: Path) -> None:
    inner, top = _link_paths(containers_dir, layer)
    current = _resolved_live_target(inner, top)
    before_inner = _link_state(inner)
    before_top = _link_state(top)
    resolved_target = target.resolve(strict=True)
    if target.is_symlink() or not target.is_file():
        raise FileNotFoundError(f"SAC image artifact is not a regular SIF: {target}")
    if current == resolved_target:
        return

    try:
        _atomic_symlink(inner, target.name)
        _atomic_symlink(top, f"sac-{layer}/{target.name}")
        if inner.resolve(strict=True) != resolved_target:
            raise RuntimeError(f"inner live link did not activate {target}")
        if top.resolve(strict=True) != resolved_target:
            raise RuntimeError(f"top-level live link did not activate {target}")
    except Exception:
        _restore_link(inner, before_inner)
        _restore_link(top, before_top)
        raise


def switch_layer_version(containers_dir: Path, layer: str, version: str) -> str:
    """Atomically repoint both stable links to one explicit layer version."""
    if layer not in LAYERS:
        raise ValueError(f"invalid SAC image layer: {layer!r}")
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"invalid SAC image version: {version!r}")
    containers_dir = Path(containers_dir)
    image_name = f"sac-{layer}"
    layer_dir = containers_dir / image_name
    target = layer_dir / f"{image_name}-{version}.sif"
    with image_build_lock(layer_dir, layer=layer):
        if target.is_symlink() or not target.is_file():
            raise FileNotFoundError(f"SAC {layer} version {version} not found: {target}")
        _activate_unlocked(containers_dir, layer, target)
    return version


def rollback_layer(containers_dir: Path, layer: str) -> str:
    """Activate the artifact immediately older than the current layer image."""
    if layer not in LAYERS:
        raise ValueError(f"invalid SAC image layer: {layer!r}")
    containers_dir = Path(containers_dir)
    image_name = f"sac-{layer}"
    layer_dir = containers_dir / image_name
    inner, top = _link_paths(containers_dir, layer)
    with image_build_lock(layer_dir, layer=layer):
        current = _resolved_live_target(inner, top)
        if current is None:
            raise RuntimeError(f"no active SAC {layer} image; cannot roll back")
        artifacts = sorted(
            (
                path
                for path in layer_dir.glob(f"{image_name}-*.sif")
                if path.is_file() and not path.is_symlink()
            ),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        resolved = [path.resolve() for path in artifacts]
        try:
            current_index = resolved.index(current)
        except ValueError as exc:
            raise RuntimeError(
                f"active SAC {layer} artifact is not in the layer store: {current}"
            ) from exc
        if current_index + 1 >= len(artifacts):
            version = _version_from_name(layer, current.name)
            raise RuntimeError(
                f"no older SAC {layer} image available (current: {version})"
            )
        previous = artifacts[current_index + 1]
        _activate_unlocked(containers_dir, layer, previous)
    return _version_from_name(layer, previous.name)


__all__ = ["LAYERS", "rollback_layer", "switch_layer_version"]
