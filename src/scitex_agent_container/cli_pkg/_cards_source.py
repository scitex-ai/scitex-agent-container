"""Resolve and stage the exact scitex-cards source installed in the base image."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

CARDS_COMMIT = "6e7fd467ba1c4bc77ed08e8a3ad45c17f8f46c5d"
CARDS_REPOSITORY = "https://github.com/scitex-ai/scitex-cards.git"
CARDS_SOURCE_ENV = "SAC_SCITEX_CARDS_SOURCE_DIR"
STAGED_CARDS_SOURCE = "scitex-cards-src"


class CardsSourceError(RuntimeError):
    """The pinned scitex-cards source could not be resolved or verified."""


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
        raise CardsSourceError(detail or f"git {' '.join(args)} failed")
    return completed.stdout.strip()


def _contains_commit(repo: Path) -> bool:
    completed = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "-e", f"{CARDS_COMMIT}^{{commit}}"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def cards_cache_repo() -> Path:
    """Return the stable, user-owned upstream cache location."""
    cache_root = Path(
        os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))
    ).expanduser()
    return cache_root / "scitex-agent-container" / "upstream" / "scitex-cards.git"


def resolve_cards_repo() -> Path:
    """Resolve a repository containing the pinned commit, fetching if absent."""
    explicit = os.environ.get(CARDS_SOURCE_ENV)
    if explicit:
        repo = Path(explicit).expanduser().resolve()
        if not repo.is_dir() or not _contains_commit(repo):
            raise CardsSourceError(
                f"{CARDS_SOURCE_ENV} must name a git repository containing "
                f"scitex-cards commit {CARDS_COMMIT}: {repo}"
            )
        return repo

    cache = cards_cache_repo()
    if cache.is_dir() and _contains_commit(cache):
        return cache

    cache.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cards-fetch-", dir=cache.parent) as temp:
        candidate = Path(temp) / "repo.git"
        _git("init", "--bare", str(candidate))
        try:
            _git(
                "-C",
                str(candidate),
                "fetch",
                "--depth",
                "1",
                CARDS_REPOSITORY,
                CARDS_COMMIT,
            )
        except CardsSourceError as exc:
            raise CardsSourceError(
                f"could not fetch pinned scitex-cards commit {CARDS_COMMIT}: {exc}. "
                f"Set {CARDS_SOURCE_ENV} to an existing verified checkout and retry."
            ) from exc
        if not _contains_commit(candidate):
            raise CardsSourceError(
                f"fetch completed without pinned scitex-cards commit {CARDS_COMMIT}"
            )
        if cache.exists():
            shutil.rmtree(cache)
        candidate.rename(cache)
    return cache


def stage_cards_source(dest_dir: Path) -> Path:
    """Export only tracked bytes at the pinned commit into the build context."""
    repo = resolve_cards_repo()
    destination = dest_dir / STAGED_CARDS_SOURCE
    archive_path = dest_dir / ".scitex-cards.tar"
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
            CARDS_COMMIT,
        )
        with tarfile.open(archive_path, mode="r:") as archive:
            archive.extractall(destination, filter="data")
    finally:
        archive_path.unlink(missing_ok=True)
    (destination / "SAC_UPSTREAM_COMMIT").write_text(
        f"{CARDS_COMMIT}\n", encoding="utf-8"
    )
    return destination


__all__ = [
    "CARDS_COMMIT",
    "CARDS_REPOSITORY",
    "CARDS_SOURCE_ENV",
    "STAGED_CARDS_SOURCE",
    "CardsSourceError",
    "cards_cache_repo",
    "resolve_cards_repo",
    "stage_cards_source",
]
