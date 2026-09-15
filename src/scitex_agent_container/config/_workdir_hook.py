"""Map an in-container workdir to its declared writable host bind."""

from __future__ import annotations

import shlex
from pathlib import Path, PurePosixPath
from typing import Iterable


def _bind_fields(raw: str) -> tuple[str, str, str]:
    """Return ``(source, destination, mode)`` for one normalized bind."""
    source, separator, remainder = str(raw).partition(":")
    if not separator:
        return source, source, ""
    destination, mode_separator, mode = remainder.partition(":")
    return source, destination, mode if mode_separator else ""


def _writable_host_workdir(workdir: str, binds: Iterable[str]) -> Path | None:
    """Resolve ``workdir`` through the most-specific writable bind.

    ``spec.workdir`` is an in-container path.  Host lifecycle hooks must not
    operate on that spelling unless an explicit bind proves which host path
    supplies it.  Nested destinations use the longest matching prefix; a
    later declaration wins an exact-prefix tie, matching the argv ordering.
    """
    normalized = PurePosixPath(workdir)
    if not normalized.is_absolute():
        return None
    candidates: list[tuple[int, int, Path]] = []
    for index, raw in enumerate(binds):
        source, destination, mode = _bind_fields(str(raw))
        if not source or not destination or "ro" in mode.split(","):
            continue
        target = PurePosixPath(destination)
        if not target.is_absolute():
            continue
        try:
            relative = normalized.relative_to(target)
        except ValueError:
            continue
        host_path = Path(source).expanduser().joinpath(*relative.parts)
        candidates.append((len(target.parts), index, host_path))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (item[0], item[1]))[2]


def mapped_workdir_mkdir_hook(workdir: str, binds: Iterable[str]) -> str | None:
    """Build the host-side ``.claude`` mkdir hook for a declared bind."""
    host_workdir = _writable_host_workdir(workdir, binds)
    if host_workdir is None:
        return None
    return f"mkdir -p {shlex.quote(str(host_workdir / '.claude'))}"


__all__ = ["mapped_workdir_mkdir_hook"]
