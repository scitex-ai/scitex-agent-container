"""Portable SAC image references and immutable launch identities."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

MANAGED_IMAGES = frozenset({"sac-base", "sac-scitex", "sac-proxy"})
_MANAGED_ARTIFACT_RE = re.compile(
    r"^(sac-(?:base|scitex|proxy))(?:-\d{4}-\d{4}-\d{6})?\.sif$"
)
_MANAGED_LAYOUT_MARKERS = (
    "/.scitex/agent-container/containers/",
    "/sac-images/",
    "/scitex-containers/agent-container/",
)


def managed_image_path(reference: str) -> Path | None:
    """Resolve a logical SAC image name to this host's live image link."""
    name = str(reference or "").strip()
    if name not in MANAGED_IMAGES:
        return None
    return Path.home() / ".scitex" / "agent-container" / "containers" / f"{name}.sif"


def legacy_managed_image_name(reference: str) -> str | None:
    """Logical name for an old managed filesystem reference, else ``None``.

    A basename alone is insufficient: CI and operators may use a custom image
    named ``sac-base.sif`` under an unrelated directory. Only paths inside a
    known SAC build/install layout are managed declarations.
    """
    raw = str(reference or "").strip()
    match = _MANAGED_ARTIFACT_RE.fullmatch(Path(raw).name)
    if match is None:
        return None
    normalized = raw.replace("\\", "/")
    if normalized.startswith("~/.scitex/agent-container/containers/"):
        return match.group(1)
    if any(marker in normalized for marker in _MANAGED_LAYOUT_MARKERS):
        return match.group(1)
    return None


def image_artifact_identity(path: Path | str) -> dict[str, str]:
    """Return the exact immutable path and content digest used for a launch."""
    target = Path(path).expanduser().resolve(strict=True)
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return {"path": str(target), "sha256": digest.hexdigest()}


__all__ = [
    "MANAGED_IMAGES",
    "image_artifact_identity",
    "legacy_managed_image_name",
    "managed_image_path",
]
