#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_identity.py

"""The ``sac --version`` fast path: WHICH code is loaded, and from WHERE.

A declared version string cannot answer "is my fix deployed?". ``0.21.13``
reads identically on a machine where the fix shipped and one where it did
not, because a fix that does not bump the version does not move the
number. Every layer of this project has been burned by that: an operator
concluding "OK, it's the latest"; a SIF-baked 0.9.4 accepted as 0.9.5; a
test run reporting 1087 passed while importing site-packages instead of
the worktree.

So this reports three things instead of one:

* ``version``  — what the distribution DECLARES (unchanged, still first
  on the line, so ``sac --version | cut -d' ' -f3`` keeps working).
* ``commit``   — what the code IS. Live-read from ``.git`` when running
  from a checkout, else the sha baked in at build time.
* ``origin``   — where the loaded module actually CAME FROM.

COST: ~0.5 ms on top of the ``importlib.metadata`` lookup ``--version``
already paid. No subprocess (a ``git rev-parse`` fork is ~89 ms), no tree
walk (hashing the tree is ~35 ms). The expensive, exhaustive checks live
in ``sac provenance``.

WHY LIVE GIT WINS OVER THE BAKED STAMP: in an editable install the stamp
is written once at ``pip install -e`` time and is stale the moment you
commit. The checkout on disk is the truth. In a wheel/SIF there is no
checkout, so the stamp is the truth. Each source is preferred exactly
where it is the authoritative one.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _dist_version
from pathlib import Path

from ._git import head_sha, repo_root_for_package

__all__ = ["DIST_NAME", "baked", "format_terse", "identity", "package_dir"]

DIST_NAME = "scitex-agent-container"


def package_dir() -> Path:
    """Absolute path of the LOADED ``scitex_agent_container`` package."""
    return Path(__file__).resolve().parent.parent


def baked() -> dict:
    """Return the build stamp baked in by the build hook, or ``{}``.

    Absent in a plain git checkout that was never built or installed —
    which is fine, because that case reads its commit live from ``.git``.
    """
    try:
        from ._build_info import STAMP  # type: ignore[import-not-found]
    except ImportError:  # stx-allow: fallback (reason: an unbuilt source tree has no stamp by design; the live-git path covers it)
        return {}
    return dict(STAMP) if isinstance(STAMP, dict) else {}


def declared_version() -> str:
    """The version the INSTALLED distribution advertises."""
    try:
        return _dist_version(DIST_NAME)
    except PackageNotFoundError:  # stx-allow: fallback (reason: running straight off a source tree with nothing installed)
        return baked().get("version") or "0.0.0+unknown"


def identity() -> dict:
    """Resolve the identity of the code that is actually imported.

    ``install`` is one of:

    * ``src``     — loaded from a live git checkout (editable install, or
      a bare ``PYTHONPATH=<worktree>/src``). ``commit`` is HEAD, read
      live. Note the WORKING TREE is not verified here — uncommitted
      edits do not move HEAD. ``sac provenance`` hashes the bytes.
    * ``wheel``   — an installed/copied tree carrying a build stamp.
    * ``unknown`` — no checkout and no stamp: a tree built before this
      existed, or hand-copied. Says so rather than guessing.
    """
    origin = package_dir()
    stamp = baked()
    root = repo_root_for_package(origin)

    if root is not None:
        commit = head_sha(root)
        if commit:
            return {
                "version": declared_version(),
                "commit": commit,
                "commit_source": "git",
                "code_hash": None,
                "built_at": None,
                "install": "src",
                "origin": str(origin),
                "repo_root": str(root),
            }

    commit = stamp.get("commit")
    return {
        "version": declared_version(),
        "commit": commit,
        "commit_source": stamp.get("commit_source") if commit else None,
        "code_hash": stamp.get("code_hash"),
        "built_at": stamp.get("built_at"),
        "install": "wheel" if stamp else "unknown",
        "origin": str(origin),
        "repo_root": None,
    }


def short_id(info: dict) -> str:
    """The token that MOVES when the code moves.

    ``g<sha>`` when a commit is known; else ``h<digest>`` from the
    build-time content hash — so even a wheel built with no ``.git``
    anywhere still reports something that changes when the code changes.
    """
    if info.get("commit"):
        return "g" + info["commit"][:8]
    if info.get("code_hash"):
        return "h" + info["code_hash"][:8]
    return "unknown"


def format_terse(info: dict) -> str:
    """One line. Keeps click's ``<prog>, version <X.Y.Z>`` prefix intact.

    Scripts that parse the third whitespace field still get the version;
    everything added is appended after it.
    """
    marker = short_id(info)
    bits = [marker] if marker == "unknown" else [marker, info["install"]]
    built = info.get("built_at")
    if built:
        bits.append(built.split("T")[0])
    return (
        f"{DIST_NAME}, version {info['version']} "
        f"({' '.join(bits)}) from {info['origin']}"
    )


# EOF
