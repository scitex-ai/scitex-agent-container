"""How big is that tree, and is the copy of it the same tree?

The two MEASURING instruments of the ADR-0024 migration
(:mod:`._scratch_migrate`), split out of it so each file holds one
responsibility. Pure reads — nothing here writes, moves or deletes anything.

They answer the two questions the operator's decision rests on:

* :func:`tree_size` — the number in the preview. It must match what the
  operator will get back on the root LV, so hard-linked files (a uv cache is
  full of them) are counted ONCE, exactly as ``du`` counts them. Counting
  them per-link would promise space the move cannot free.
* :func:`verify_copy` — the instrument that licenses the ``rmtree``. The
  overlay copy is removed only after this returns empty, so it has to be able
  to say NO: a missing path, a short file, a symlink pointing elsewhere, or a
  device node the copy quietly skipped.
"""

from __future__ import annotations

import os
from pathlib import Path


def tree_size(path: Path) -> tuple[int, int]:
    """``(bytes, files)`` under ``path`` — ``lstat``, no symlink following,
    hard-linked files counted ONCE (as ``du`` counts them).

    Symlinks count as files at their own (link) size. Directories, sockets
    and device nodes contribute no bytes. A missing path is ``(0, 0)``.
    """
    if not path.is_dir():
        return (0, 0)
    seen: set[tuple[int, int]] = set()
    total = 0
    files = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            st = os.lstat(os.path.join(dirpath, name))
            files += 1
            key = (st.st_dev, st.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += st.st_size
    return (total, files)


def _tree_manifest(root: Path) -> dict[str, tuple[str, object]]:
    """``{relpath: (kind, size | link target)}`` for every entry under ``root``.

    The verification instrument: two trees are the same tree when their
    manifests are equal — every directory, every regular file at the same
    size, every symlink with the same target. Anything else (a device node,
    a socket) is recorded by kind so it can never match a copy that skipped
    it.
    """
    out: dict[str, tuple[str, object]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        base = Path(dirpath)
        for d in dirnames:
            p = base / d
            rel = str(p.relative_to(root))
            out[rel] = ("symlink", os.readlink(p)) if p.is_symlink() else ("dir", 0)
        for f in filenames:
            p = base / f
            rel = str(p.relative_to(root))
            st = os.lstat(p)
            if p.is_symlink():
                out[rel] = ("symlink", os.readlink(p))
            elif os.path.isfile(p):
                out[rel] = ("file", st.st_size)
            else:
                out[rel] = ("special", st.st_mode)
    return out


def verify_copy(source: Path, dest: Path) -> list[str]:
    """Differences between ``source`` and ``dest``; empty means verified."""
    src = _tree_manifest(source)
    dst = _tree_manifest(dest)
    problems: list[str] = []
    for rel, want in sorted(src.items()):
        got = dst.get(rel)
        if got is None:
            problems.append(f"missing in copy: {rel}")
        elif got != want:
            problems.append(f"differs: {rel} source={want} copy={got}")
    for rel in sorted(set(dst) - set(src)):
        problems.append(f"extra in copy: {rel}")
    return problems


__all__ = ["tree_size", "verify_copy"]
