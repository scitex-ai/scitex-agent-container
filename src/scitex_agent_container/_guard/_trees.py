#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/_trees.py

"""Read a BEFORE/AFTER tree as ``{relative_path: content}``.

Three sources, one shape:

* :func:`tree_from_ref`       a git ref (``HEAD``, ``origin/develop``, a SHA)
* :func:`tree_from_worktree`  the repo's working tree as it is on disk
* :func:`tree_from_dir`       a plain snapshot directory

Every failure raises :class:`BaselineUnavailable` carrying a sentence that
says what to do about it. Nothing here ever returns an empty tree to mean
"could not read" — an empty dict compares clean against anything, which is
precisely the silent pass this guard exists to prevent.

Only ``.py`` files carry content; every other path is stored as ``""``.
The detector reads non-python paths for PRESENCE only (did the file
vanish?), so decoding them would cost memory to answer a question nobody
asks — and would choke on binaries.
"""

from __future__ import annotations

import io
import shutil
import subprocess
import tarfile
from pathlib import Path

__all__ = [
    "BaselineUnavailable",
    "tree_from_dir",
    "tree_from_ref",
    "tree_from_worktree",
]

# Directories never worth walking in a snapshot dir. Not a gitignore
# substitute — `tree_from_worktree` uses git itself for that.
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".mypy_cache",
              ".pytest_cache", ".ruff_cache", ".tox", ".worktrees"}

_GIT_TIMEOUT_S = 120


class BaselineUnavailable(Exception):
    """A tree could not be read. ``reason`` is operator-facing prose."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _git(repo: Path, *args: str) -> bytes:
    """Run git in ``repo``; raise BaselineUnavailable with git's own words."""
    if shutil.which("git") is None:
        raise BaselineUnavailable(
            "git was not found on PATH, so no baseline can be read — "
            "install git, or pass an explicit --before/--after snapshot pair"
        )
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise BaselineUnavailable(
            f"git {args[0]} timed out after {_GIT_TIMEOUT_S}s in {repo}"
        ) from None
    except OSError as exc:
        raise BaselineUnavailable(f"could not run git in {repo}: {exc}") from None
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise BaselineUnavailable(
            f"git {args[0]} failed in {repo}: "
            + (detail[-1] if detail else f"exit {proc.returncode}")
        )
    return proc.stdout


def _require_repo(repo: Path) -> None:
    if not repo.is_dir():
        raise BaselineUnavailable(
            f"{repo} is not a directory — pass --repo pointing at a checkout"
        )
    try:
        _git(repo, "rev-parse", "--git-dir")
    except BaselineUnavailable:
        raise BaselineUnavailable(
            f"{repo} is not a git repository, so there is no ref to compare "
            "against — pass an explicit --before/--after snapshot pair instead"
        ) from None


def _decode(path: str, data: bytes) -> str:
    if not path.endswith(".py"):
        return ""
    return data.decode("utf-8", "replace")


def tree_from_ref(repo: Path, ref: str) -> dict:
    """The tree recorded at ``ref``. One ``git archive``, no per-file forks."""
    _require_repo(repo)
    try:
        _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{tree}}")
    except BaselineUnavailable:
        raise BaselineUnavailable(
            f"{ref!r} does not resolve to a tree in {repo} — check the ref "
            "name, or `git fetch` if it lives on a remote you have not "
            "updated (a bare-branch fetch does NOT move origin/<branch>)"
        ) from None
    blob = _git(repo, "archive", "--format=tar", ref)
    out: dict[str, str] = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                handle = tar.extractfile(member)
                data = handle.read() if handle is not None else b""
                out[member.name] = _decode(member.name, data)
    except tarfile.TarError as exc:
        raise BaselineUnavailable(
            f"could not read the archive of {ref!r} in {repo}: {exc}"
        ) from None
    return out


def tree_from_worktree(repo: Path) -> dict:
    """The working tree as it is ON DISK, honouring .gitignore.

    Tracked paths come from the index, so a file deleted-but-not-staged is
    still listed there — it is dropped here by the ``is_file()`` check,
    which is exactly the deletion we want the diff to see.
    """
    _require_repo(repo)
    names: list[str] = []
    for args in (("ls-files", "-z"),
                 ("ls-files", "-z", "--others", "--exclude-standard")):
        raw = _git(repo, *args).decode("utf-8", "replace")
        names.extend(n for n in raw.split("\0") if n)
    out: dict[str, str] = {}
    for name in sorted(set(names)):
        path = repo / name
        if not path.is_file():
            continue
        try:
            out[name] = _decode(name, path.read_bytes()) if name.endswith(".py") else ""
        except OSError as exc:
            raise BaselineUnavailable(
                f"could not read {name} in {repo}: {exc}"
            ) from None
    return out


def tree_from_dir(root: Path) -> dict:
    """A plain snapshot directory — the ``--before``/``--after`` mode."""
    if not root.is_dir():
        raise BaselineUnavailable(
            f"{root} is not a directory — a snapshot must be a real tree on "
            "disk, and a missing one is not an empty one"
        )
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(root).parts
        if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        name = "/".join(rel_parts)
        try:
            out[name] = _decode(name, path.read_bytes()) if name.endswith(".py") else ""
        except OSError as exc:
            raise BaselineUnavailable(
                f"could not read {name} under {root}: {exc}"
            ) from None
    return out


# EOF
