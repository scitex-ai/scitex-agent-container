#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_hash.py

"""Content hash of a package's ``.py`` tree — the identity that cannot lie.

A version string is *declared*; a git sha is *claimed at build time*. This
hash is *derived from the bytes actually on disk*, so it is the only signal
that still tells the truth when someone hand-patches a file in
site-packages, when a wheel is built from a dirty tree, or when a fossil
``.dist-info`` advertises a version whose code is long gone.

It is NOT on the ``sac --version`` fast path: hashing this package's 491
``.py`` files (4 MB) costs ~35 ms warm, and worse on a SIF's compressed
squashfs. It backs ``sac provenance`` instead.

Two rules make the build-time and runtime digests comparable:

* **Exclude ``_build_info.py``.** It is generated *from* this hash, so
  including it would make the build-time digest unmatchable by definition.
* **Hash only content and POSIX-relative paths** — never mtimes, absolute
  paths, or ``.pyc``. The digest must be reproducible on another machine.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

__all__ = ["EXCLUDED_NAMES", "code_hash", "iter_py_files"]

# Generated at build time from the hash itself — see the module docstring.
EXCLUDED_NAMES = frozenset({"_build_info.py"})
_EXCLUDED_DIRS = frozenset({"__pycache__"})
_DIGEST_SIZE = 16


def iter_py_files(package_dir: Path) -> list[Path]:
    """Return every hashable ``.py`` file under ``package_dir``, sorted.

    Sorted by POSIX-relative path so the digest is independent of
    filesystem walk order.
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(package_dir):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS]
        for name in filenames:
            if name.endswith(".py") and name not in EXCLUDED_NAMES:
                found.append(Path(dirpath) / name)
    return sorted(found, key=lambda p: p.relative_to(package_dir).as_posix())


def code_hash(package_dir: Path) -> str | None:
    """Return a stable content digest of the ``.py`` tree at ``package_dir``.

    Returns None if the directory does not exist. Length-prefixing each
    file keeps the stream unambiguous, so no combination of renames or
    concatenations can collide two different trees onto one digest.
    """
    package_dir = Path(package_dir)
    if not package_dir.is_dir():
        return None
    digest = hashlib.blake2b(digest_size=_DIGEST_SIZE)
    for path in iter_py_files(package_dir):
        rel = path.relative_to(package_dir).as_posix()
        try:
            data = path.read_bytes()
        except OSError:  # stx-allow: fallback (reason: an unreadable file must change the digest, not crash --version's sibling command)
            data = b"\0unreadable"
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(len(data)).encode("ascii"))
        digest.update(b"\0")
        digest.update(data)
    return digest.hexdigest()


# EOF
