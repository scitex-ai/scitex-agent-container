#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_git.py

"""Resolve a git HEAD sha by READING .git — never by shelling out.

``subprocess git rev-parse HEAD`` costs ~89 ms (measured, warm cache).
Reading ``.git/HEAD`` and chasing the ref costs ~0.44 ms — 200x cheaper.
``sac --version`` is typed constantly and called by scripts, so the fast
path must not fork a process.

Handles the three layouts a real checkout can be in:

* ``.git/`` is a directory (normal clone) — read ``.git/HEAD``.
* ``.git`` is a FILE (``git worktree``) — it holds ``gitdir: <path>``;
  the worktree's own HEAD lives there, but refs resolve through
  ``commondir`` back to the main repo.
* the ref is not a loose file — fall back to ``packed-refs``.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["git_dir_for", "head_sha", "repo_root_for_package"]


def _read(path: Path) -> str | None:
    """Read a small text file, or None if it isn't there / isn't readable."""
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:  # stx-allow: fallback (reason: absent/unreadable .git file is a normal "not a checkout" answer, not an error)
        return None


def git_dir_for(root: Path) -> Path | None:
    """Return the real git dir for ``root``, or None if it isn't a checkout.

    ``root/.git`` is either a directory (plain clone) or a file holding
    ``gitdir: <path>`` (a ``git worktree`` — which is exactly how agents
    check this repo out, so it is not an edge case here).
    """
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    if not dot_git.is_file():
        return None
    text = _read(dot_git) or ""
    if "gitdir:" not in text:
        return None
    target = Path(text.split("gitdir:", 1)[1].strip())
    if not target.is_absolute():
        target = (root / target).resolve()
    return target if target.is_dir() else None


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """Resolve a ref name to a sha: loose file first, then packed-refs.

    A linked worktree keeps its own HEAD but shares refs with the main
    repo, reachable via ``commondir``.
    """
    loose = git_dir / ref
    if loose.is_file():
        return _read(loose)

    search_dirs = [git_dir]
    commondir = _read(git_dir / "commondir")
    if commondir:
        common = Path(commondir)
        if not common.is_absolute():
            common = (git_dir / common).resolve()
        search_dirs.append(common)

    for base in search_dirs:
        loose = base / ref
        if loose.is_file():
            return _read(loose)
        packed = _read(base / "packed-refs")
        if not packed:
            continue
        for line in packed.splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    return None


def head_sha(root: Path) -> str | None:
    """Return the 40-char sha at HEAD for the checkout at ``root``.

    Returns None when ``root`` is not a git checkout, or when HEAD points
    at a ref that cannot be resolved (a freshly ``git init``-ed repo with
    no commit yet).
    """
    git_dir = git_dir_for(root)
    if git_dir is None:
        return None
    head = _read(git_dir / "HEAD")
    if not head:
        return None
    if not head.startswith("ref:"):
        # Detached HEAD — the file holds the sha directly.
        return head or None
    return _resolve_ref(git_dir, head.split(None, 1)[1].strip())


def repo_root_for_package(package_dir: Path) -> Path | None:
    """Return the source checkout root IF ``package_dir`` is a live one.

    Deliberately strict: the package must sit at ``<root>/src/<pkg>`` AND
    ``<root>`` must be a git checkout. A wheel unpacked into a
    site-packages that merely happens to live inside some unrelated git
    repo must NOT be mistaken for a source checkout — that would report a
    commit that has nothing to do with the installed code, which is the
    exact class of lie this module exists to kill.
    """
    if package_dir.parent.name != "src":
        return None
    root = package_dir.parent.parent
    return root if git_dir_for(root) is not None else None


# EOF
