"""Resolve and stage the exact Hermes source installed in the base image."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

# Immutable SAC-lineage source for the cache fix proposed upstream in
# https://github.com/NousResearch/hermes-agent/pull/110480 plus the external
# inbound renderer proposed for current Hermes main in
# https://github.com/ywatanabe1989/hermes-agent/pull/1. The current-main
# history is unrelated to SAC's b635448 pin, so use this validated one-commit
# descendant instead of importing that unrelated lineage into the base image.
HERMES_COMMIT = "9ca9b7e5b9092465d37e4af0c2132aed188af5dd"
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


def _patch_hermes_lifecycle_instrumentation(staged: Path) -> bool:
    """Make heartbeat lifecycle events independent of optional UI chrome.

    Hermes' pinned source suppresses ``tool.start`` / ``tool.complete`` when
    ``display.tool_progress=off`` (including focus mode). SAC consumes those
    events as an authoritative instrument, so the staging step removes only
    those two display gates and fails closed if the pinned anchors drift.
    """
    module = staged / "tui_gateway" / "tool_progress.py"
    if not module.is_file():
        return False
    text = module.read_text(encoding="utf-8")
    replacements = {
        """    if not _connector_tool_lifecycle(name, args):
        return _emit(event, sid, payload)""": """    if not _connector_tool_lifecycle(name, args):
        visible = (
            _tool_progress_enabled(sid)
            or _tool_lifecycle_required_for_ui(name)
            or (
                event == "tool.complete"
                and (payload.get("inline_diff") or name in _TODO_TOOL_NAMES)
            )
        )
        if visible:
            return _emit(event, sid, payload)
        # SAC heartbeat instrumentation replay-only: preserve optional UI
        # chrome while recording every authoritative tool lifecycle event.
        from tui_gateway.event_replay import _stamp_event

        frame = _event_frame(event, sid, payload)
        _stamp_event(frame)
        return None""",
        """    if (_tool_progress_enabled(sid) or _tool_lifecycle_required_for_ui(name)
            or _connector_tool_lifecycle(name, args)):""": (
            "    if True:  # SAC heartbeat instrumentation is display-independent"
        ),
        """    if (_tool_progress_enabled(sid) or payload.get("inline_diff") or _tool_lifecycle_required_for_ui(name)
            or name in _TODO_TOOL_NAMES or _connector_tool_lifecycle(name, args)):""": (
            "    if True:  # SAC heartbeat instrumentation is display-independent"
        ),
    }
    for old, new in replacements.items():
        if text.count(old) != 1:
            raise HermesSourceError(
                "pinned Hermes tool lifecycle gate drifted; refusing an "
                "uninstrumented build"
            )
        text = text.replace(old, new)
    module.write_text(text, encoding="utf-8")
    return True


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
    if _patch_hermes_lifecycle_instrumentation(destination):
        (destination / "SAC_LOCAL_PATCHES").write_text(
            "heartbeat-tool-lifecycle-display-independent\n", encoding="utf-8"
        )
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
