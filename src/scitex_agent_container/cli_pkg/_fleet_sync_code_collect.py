"""Local git-state collector for ``sac fleet sync-code --collect``.

Shells out to git per checkout (``~/proj/scitex-*`` + ``~/.dotfiles``)
and reports the installed ``sac`` version. Emits one JSON manifest on
stdout; exits non-zero only when collection itself fails (no git, no
proj dir) — a dirty tree or a behind HEAD is DATA, not failure.

Triple-gate values (branch / dirty / ahead / behind) let the lead host
tell the operator WHY a host can't fast-forward, not merely THAT it
differs. Untracked files never count as dirty (matches fleet_sync.sh).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

__all__ = ["collect_checkout_state"]


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=30,
    )


def _collect_one_checkout(d: Path) -> dict[str, Any] | None:
    """Return the per-checkout record, or None when not a git checkout."""
    if not (d / ".git").exists():
        return None
    branch = _run_git(["branch", "--show-current"], d).stdout.strip() or None
    sha = _run_git(["rev-parse", "HEAD"], d).stdout.strip()
    if not sha:
        return None
    dirty_out = _run_git(["status", "--porcelain"], d).stdout
    dirty = any(not line.startswith("??") for line in dirty_out.splitlines())
    ahead = behind = 0
    if branch:
        _run_git(["fetch", "origin", branch], d)
        counts = _run_git(
            ["rev-list", "--count", "--left-right", f"HEAD...origin/{branch}"],
            d,
        ).stdout.strip()
        if counts:
            try:
                left, right = counts.split()
                ahead, behind = int(left), int(right)
            except ValueError:
                pass
    return {
        "branch": branch,
        "sha": sha,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
    }


def collect_checkout_state(
    *,
    proj_dir: Path | None = None,
    dotfiles_dir: Path | None = None,
    sac_version: str | None = None,
) -> dict[str, Any]:
    """Collect this host's checkout state as a JSON-serialisable dict.

    ``sac_version`` is injected by the CLI (it knows its own version);
    the collector never imports the package version itself (worker mode
    must work even from a half-installed tree).
    """
    home = Path.home()
    proj = Path(proj_dir) if proj_dir else home / "proj"
    dotfiles = Path(dotfiles_dir) if dotfiles_dir else home / ".dotfiles"

    checkouts: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if proj.is_dir():
        for sub in sorted(proj.iterdir()):
            if not sub.is_dir() or not sub.name.startswith("scitex-"):
                continue
            try:
                rec = _collect_one_checkout(sub)
            except subprocess.TimeoutExpired:
                errors.append(f"checkout {sub.name!r} timed out")
                continue
            if rec is not None:
                checkouts[sub.name] = rec
    else:
        errors.append(f"proj dir missing: {proj}")

    dotfiles_sha: str | None = None
    if (dotfiles / ".git").exists():
        dotfiles_sha = _run_git(["rev-parse", "HEAD"], dotfiles).stdout.strip() or None
        if dotfiles_sha is None:
            errors.append("dotfiles HEAD unreadable")

    return {
        "dotfiles_sha": dotfiles_sha,
        "sac_version": sac_version,
        "checkouts": checkouts,
        "errors": errors,
    }


def emit_collect(sac_version: str | None) -> int:
    """Worker-mode entry: print manifest JSON, return process exit code."""
    from .._state.checkout_manifest import build_checkout_manifest
    from .._state.host_config import load as load_config

    try:
        cfg = load_config()
        host = cfg.canonical_host()
    except Exception:
        host = "unknown"
    state = collect_checkout_state(sac_version=sac_version)
    manifest = build_checkout_manifest(
        host=host,
        dotfiles_sha=state["dotfiles_sha"],
        sac_version=state["sac_version"],
        checkouts=state["checkouts"],
        errors=state["errors"],
    )
    print(json.dumps(manifest, indent=2))
    return 0 if not state["errors"] else 2


# EOF
