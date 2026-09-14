"""Resolve and stage the exact Hermes source installed in the base image."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

# Immutable source for the cache-lineage fix proposed upstream in
# https://github.com/NousResearch/hermes-agent/pull/110480.  Keep the fork URL
# paired with its commit until the change is available from the upstream repo.
HERMES_COMMIT = "b635448768d6ba49bc1f75bd381f32336dde7ac8"
HERMES_REPOSITORY = "https://github.com/ywatanabe1989/hermes-agent.git"
HERMES_SOURCE_ENV = "SAC_HERMES_SOURCE_DIR"
STAGED_HERMES_SOURCE = "hermes-agent-src"


class HermesSourceError(RuntimeError):
    """The pinned Hermes source could not be resolved or verified."""


def _git(*args: str, cwd: Path | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise HermesSourceError(detail or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _contains_commit(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{HERMES_COMMIT}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def hermes_cache_repo() -> Path:
    """Return the stable, user-owned upstream cache location."""
    cache_root = Path(
        os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    ).expanduser()
    return cache_root / "scitex-agent-container" / "upstream" / "hermes-agent.git"


def resolve_hermes_repo() -> Path:
    """Resolve a repository containing the pinned commit, fetching if absent."""
    explicit = os.environ.get(HERMES_SOURCE_ENV)
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        if not repo.is_dir() or not _contains_commit(repo):
            raise HermesSourceError(
                f"{HERMES_SOURCE_ENV} must name a git repository containing "
                f"Hermes commit {HERMES_COMMIT}: {repo}"
            )
        return repo

    cache = hermes_cache_repo()
    if cache.is_dir() and _contains_commit(cache):
        return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="hermes-fetch-", dir=cache.parent) as temp:
        candidate = Path(temp) / "repo.git"
        _git("init", "--bare", str(candidate))
        try:
            _git(
                "-C",
                str(candidate),
                "fetch",
                "--depth",
                "1",
                HERMES_REPOSITORY,
                HERMES_COMMIT,
            )
        except HermesSourceError as exc:
            raise HermesSourceError(
                f"could not fetch pinned Hermes commit {HERMES_COMMIT}: {exc}. "
                f"Set {HERMES_SOURCE_ENV} to an existing verified checkout and retry."
            ) from exc
        if not _contains_commit(candidate):
            raise HermesSourceError(
                f"fetch completed without pinned Hermes commit {HERMES_COMMIT}"
            )
        if cache.exists():
            shutil.rmtree(cache)
        candidate.rename(cache)
    return cache


def stage_hermes_source(dest_dir: Path) -> Path:
    """Export only tracked bytes at the pinned commit into the build context."""
    repo = resolve_hermes_repo()
    destination = dest_dir / STAGED_HERMES_SOURCE
    archive_path = dest_dir / ".hermes-agent.tar"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    try:
        _git(
            "-C",
            str(repo),
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            HERMES_COMMIT,
        )
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(destination, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)
    (destination / "SAC_UPSTREAM_COMMIT").write_text(
        f"{HERMES_COMMIT}\n", encoding="utf-8"
    )
    (destination / "SAC_UPSTREAM_REPOSITORY").write_text(
        f"{HERMES_REPOSITORY}\n", encoding="utf-8"
    )
    return destination


__all__ = [
    "HERMES_COMMIT",
    "HERMES_REPOSITORY",
    "HERMES_SOURCE_ENV",
    "STAGED_HERMES_SOURCE",
    "HermesSourceError",
    "hermes_cache_repo",
    "resolve_hermes_repo",
    "stage_hermes_source",
]
