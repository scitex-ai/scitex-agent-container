"""Fail-closed authority validation for lifecycle spec reads.

Diagnostics may describe an unknown source.  A lifecycle operation may not
launch from one.  This module is the launch boundary: it accepts only the live
``develop`` main checkout at exactly its fetched upstream, or an intentionally
detached snapshot whose directory name pins both source identity and commit.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_GIT_TIMEOUT_S = 15
_LIVE_BRANCH = "develop"
_SNAPSHOT_PARENT = "sac-authority"
_SNAPSHOT_RE = re.compile(r"^(?P<source>.+)-(?P<commit>[0-9a-f]{40})$")


class SpecAuthorityError(RuntimeError):
    """The requested spec source cannot be proven safe for a launch."""


@dataclass(frozen=True)
class SpecAuthority:
    """The exact source identity accepted by the lifecycle gate."""

    kind: str
    repo: str
    head: str
    source_identity: str
    spec_digest: str
    upstream: str = ""


def _git(repo: Path, *args: str, ok: tuple[int, ...] = (0,)) -> str:
    """Run one bounded git query or raise a named authority failure."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        raise SpecAuthorityError(
            f"spec authority is unreachable: git {' '.join(args)!r} failed: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if proc.returncode not in ok:
        detail = (proc.stderr or proc.stdout).strip() or "no diagnostic"
        raise SpecAuthorityError(
            f"spec authority is unreachable: git {' '.join(args)!r} exited "
            f"{proc.returncode}: {detail}"
        )
    return proc.stdout.strip()


def _repo_for(spec_path: str | Path) -> tuple[Path, Path]:
    try:
        spec = Path(spec_path).resolve(strict=True)
    except OSError as exc:
        raise SpecAuthorityError(
            f"spec authority path cannot be resolved: {spec_path}: {exc}"
        ) from exc
    start = spec if spec.is_dir() else spec.parent
    top = _git(start, "rev-parse", "--show-toplevel")
    repo = Path(top).resolve()
    try:
        spec.relative_to(repo)
    except ValueError as exc:
        raise SpecAuthorityError(
            f"resolved spec {spec} is outside reported authority repo {repo}"
        ) from exc
    return spec, repo


def _head(repo: Path) -> str:
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise SpecAuthorityError(f"spec authority returned malformed HEAD: {head!r}")
    return head


def _require_clean(repo: Path) -> None:
    dirty = _git(repo, "status", "--porcelain=v1", "--untracked-files=all")
    if dirty:
        first = dirty.splitlines()[0]
        raise SpecAuthorityError(
            f"spec authority source is dirty: {repo} ({first}); commit or "
            "remove every change before launching"
        )


def _spec_blob_digest(repo: Path, spec: Path) -> str:
    rel = spec.relative_to(repo).as_posix()
    _git(repo, "ls-files", "--error-unmatch", "--", rel)
    observed = _git(repo, "hash-object", "--", rel)
    committed = _git(repo, "rev-parse", f"HEAD:{rel}")
    if observed != committed:
        raise SpecAuthorityError(
            f"authority spec digest does not match HEAD: {rel} "
            f"(working={observed}, committed={committed})"
        )
    return committed


def _require_stable(repo: Path, expected_head: str) -> None:
    """Reject an authority that changed while the multi-step proof ran."""
    observed = _head(repo)
    if observed != expected_head:
        raise SpecAuthorityError(
            f"spec authority changed during validation: HEAD moved from "
            f"{expected_head} to {observed}"
        )
    _require_clean(repo)


def _origin_identity(repo: Path) -> str:
    origin = _git(repo, "remote", "get-url", "origin")
    leaf = origin.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if leaf.endswith(".git"):
        leaf = leaf[:-4]
    match = _SNAPSHOT_RE.fullmatch(leaf)
    if match:
        leaf = match.group("source")
    return leaf.lstrip(".")


def _validate_snapshot(spec: Path, repo: Path, head: str) -> SpecAuthority:
    match = _SNAPSHOT_RE.fullmatch(repo.name)
    if repo.parent.name != _SNAPSHOT_PARENT or match is None:
        raise SpecAuthorityError(
            "detached spec authority is allowed only as an immutable "
            f"{_SNAPSHOT_PARENT}/<source>-<40-hex-commit> snapshot; got {repo}"
        )
    source = match.group("source")
    expected = match.group("commit")
    if head != expected:
        raise SpecAuthorityError(
            f"detached authority snapshot identity mismatch: path pins "
            f"{expected}, but HEAD is {head}"
        )
    origin_source = _origin_identity(repo)
    if source.lstrip(".") != origin_source:
        raise SpecAuthorityError(
            f"detached authority source identity mismatch: path names "
            f"{source!r}, origin identifies {origin_source!r}"
        )
    digest = _spec_blob_digest(repo, spec)
    _require_stable(repo, head)
    return SpecAuthority(
        kind="immutable-snapshot",
        repo=str(repo),
        head=head,
        source_identity=source,
        spec_digest=digest,
    )


def _main_worktree(repo: Path) -> Path:
    listing = _git(repo, "worktree", "list", "--porcelain")
    first = next(
        (line for line in listing.splitlines() if line.startswith("worktree ")), ""
    )
    if not first:
        raise SpecAuthorityError("git did not identify a main authority checkout")
    return Path(first.removeprefix("worktree ")).resolve()


def _validate_live(spec: Path, repo: Path, head: str, branch: str) -> SpecAuthority:
    main = _main_worktree(repo)
    if repo != main:
        raise SpecAuthorityError(
            f"linked worktree {repo} cannot become live spec authority implicitly; "
            f"the main checkout is {main}"
        )
    if branch != _LIVE_BRANCH:
        raise SpecAuthorityError(
            f"main spec-authority checkout must be on {_LIVE_BRANCH!r}; "
            f"{repo} is on {branch!r}. Keep feature branches in linked worktrees."
        )
    upstream = _git(repo, "rev-parse", "--abbrev-ref", "@{upstream}")
    if upstream.rsplit("/", 1)[-1] != _LIVE_BRANCH:
        raise SpecAuthorityError(
            f"live authority branch {_LIVE_BRANCH!r} must track a develop "
            f"upstream; configured upstream is {upstream!r}"
        )
    _git(repo, "fetch", "--quiet", "--prune")
    behind = int(_git(repo, "rev-list", "--count", f"HEAD..{upstream}"))
    ahead = int(_git(repo, "rev-list", "--count", f"{upstream}..HEAD"))
    if behind or ahead:
        state = "diverged" if behind and ahead else "behind" if behind else "ahead"
        raise SpecAuthorityError(
            f"live spec authority is {state}: {ahead} ahead / {behind} behind "
            f"{upstream}; synchronize it before launching"
        )
    digest = _spec_blob_digest(repo, spec)
    _require_stable(repo, head)
    return SpecAuthority(
        kind="live-develop",
        repo=str(repo),
        head=head,
        source_identity=_origin_identity(repo),
        spec_digest=digest,
        upstream=upstream,
    )


def validate_spec_authority(spec_path: str | Path) -> SpecAuthority:
    """Return exact accepted authority identity, or fail closed and loudly."""
    spec, repo = _repo_for(spec_path)
    _require_clean(repo)
    head = _head(repo)
    branch = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD", ok=(0, 1))
    if not branch:
        return _validate_snapshot(spec, repo, head)
    return _validate_live(spec, repo, head, branch)


__all__ = ["SpecAuthority", "SpecAuthorityError", "validate_spec_authority"]
