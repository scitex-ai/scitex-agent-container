#!/usr/bin/env python3
# File: .github/ci/autobump.py
"""Deterministic patch-version bump + CHANGELOG promotion for the
merge->release sweep (``autobump-release-sweep.yaml``).

Pure stdlib on purpose: no TOML dependency, no build frontend. It must run
unit-tested AND on the bare Spartan node inside the sweep, where nothing is
pip-installed.

WHAT IT TOUCHES (and nothing else):

  1. pyproject.toml — the SINGLE column-0 ``version = \"X.Y.Z\"`` line in the
     ``[project]`` table. This is byte-for-byte the line
     ``src/hatch_build.py::_declared_version()`` parses (``line.startswith
     (\"version\")``) and the value hatchling bakes into the wheel at the tagged
     commit. Indented dependency constraints (``\"click>=8.0\"``), ``requires-
     python``, and ``[tool.ruff] target-version`` all start with whitespace or a
     different token, so a col-0 anchor can never hit them. This is the
     root-cause fix (card req 1): advancing this literal every release changes
     uv's ``(name, version)`` wheel-cache key, so no stale wheel is ever reused.

  2. CHANGELOG.md — promotes ``## [Unreleased]`` to a released
     ``## [X.Y.Z] - <UTC-date>`` section (Keep-a-Changelog), leaving a fresh
     empty ``## [Unreleased]`` on top. The heading shape matches the release
     pipeline's awk extractor (``^## \\[X.Y.Z\\]``).

Both move together from ONE computed version, so tag == pyproject == CHANGELOG
can never drift (the ghost-tag-at-birth class).

Subcommands
-----------
  current-version              print the bare [project] version (e.g. 0.24.1)
  next-version                 print the next patch (0.24.1 -> 0.24.2)
  bump                         rewrite pyproject + promote CHANGELOG to the next
                               patch; print the new BARE version to stdout
  verify --version X.Y.Z       exit 3 (fail-loud) unless pyproject == X.Y.Z AND
                               CHANGELOG carries a released ``## [X.Y.Z]``
                               section AND a ``## [Unreleased]`` header is
                               present. (exit 2 is argparse usage, kept distinct
                               so a usage typo can never masquerade as
                               \"inconsistent\" — see the exit-code-collision
                               incident.)

Paths default to the repo root inferred from this file
(``<root>/.github/ci/autobump.py`` -> ``<root>``); override with ``--root`` in
tests. All mutations are fail-loud: a missing version line, an unparseable
version, or a missing ``## [Unreleased]`` heading raises rather than guessing.
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Col-0 anchored: only the [project] version line, never an indented dep line.
_VERSION_RE = re.compile(r'^version\s*=\s*"(?P<v>[^"]+)"\s*$', re.MULTILINE)
_SEMVER_RE = re.compile(r"^(?P<maj>\d+)\.(?P<min>\d+)\.(?P<pat>\d+)$")
# [ \t]* NOT \s*: \s would greedily swallow the trailing newline(s) after the
# heading, so the insert point drifts past the blank line and the promoted
# section butts against the previous one. Anchor to the heading line only.
_UNRELEASED_RE = re.compile(r"^## \[Unreleased\][ \t]*$", re.MULTILINE)

EXIT_INCONSISTENT = 3


class AutobumpError(RuntimeError):
    """Fail-loud error — the sweep converts this to a red step + off-rail alarm."""


def _root_from_here() -> Path:
    return Path(__file__).resolve().parents[2]


def _pyproject_path(root: Path) -> Path:
    return root / "pyproject.toml"


def _changelog_path(root: Path) -> Path:
    return root / "CHANGELOG.md"


def read_current_version(root: Path) -> str:
    """Return the bare ``[project]`` version, e.g. ``0.24.1``. Fail-loud."""
    text = _pyproject_path(root).read_text(encoding="utf-8")
    matches = _VERSION_RE.findall(text)
    if not matches:
        raise AutobumpError(
            'no column-0 `version = "..."` line found in pyproject.toml '
            "(the [project] version is the single source of truth)"
        )
    if len(matches) > 1:
        raise AutobumpError(
            f"ambiguous: {len(matches)} column-0 version lines in pyproject.toml; "
            "refusing to guess which one is the [project] version"
        )
    v = matches[0]
    if not _SEMVER_RE.match(v):
        raise AutobumpError(
            f"[project] version {v!r} is not a clean X.Y.Z semver; "
            "patch auto-bump only supports plain three-part versions"
        )
    return v


def compute_next_patch(version: str) -> str:
    """0.24.1 -> 0.24.2. Fail-loud on a non-semver input."""
    m = _SEMVER_RE.match(version)
    if not m:
        raise AutobumpError(f"cannot patch-bump non-semver version {version!r}")
    return f"{m['maj']}.{m['min']}.{int(m['pat']) + 1}"


def rewrite_pyproject_version(root: Path, new_version: str) -> None:
    """Rewrite ONLY the col-0 [project] version line to ``new_version``."""
    path = _pyproject_path(root)
    text = path.read_text(encoding="utf-8")
    new_text, n = _VERSION_RE.subn(f'version = "{new_version}"', text)
    if n != 1:
        raise AutobumpError(
            f"expected exactly 1 column-0 version line to rewrite, changed {n}"
        )
    path.write_text(new_text, encoding="utf-8")


def promote_changelog(root: Path, new_version: str, date: str | None = None) -> None:
    """Promote ``## [Unreleased]`` -> ``## [X.Y.Z] - <date>`` + fresh Unreleased.

    Keep-a-Changelog: whatever sat under ``## [Unreleased]`` becomes the body of
    the new released section; a new empty ``## [Unreleased]`` is left on top.
    Idempotency guard: refuses if a released ``## [X.Y.Z]`` section already
    exists (that version was already promoted).
    """
    path = _changelog_path(root)
    text = path.read_text(encoding="utf-8")
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if re.search(rf"^## \[{re.escape(new_version)}\]", text, re.MULTILINE):
        raise AutobumpError(
            f"CHANGELOG already has a `## [{new_version}]` section — refusing to "
            "double-promote (idempotency guard)"
        )
    m = _UNRELEASED_RE.search(text)
    if not m:
        raise AutobumpError("CHANGELOG.md has no `## [Unreleased]` heading to promote")

    insert_at = m.end()
    released = f"\n\n## [{new_version}] - {date}"
    new_text = text[:insert_at] + released + text[insert_at:]
    path.write_text(new_text, encoding="utf-8")


def verify_consistency(root: Path, version: str) -> list[str]:
    """Return a list of problems; empty list == consistent. Never raises."""
    problems: list[str] = []
    try:
        cur = read_current_version(root)
    except AutobumpError as exc:
        return [str(exc)]
    if cur != version:
        problems.append(f"pyproject [project] version is {cur!r}, expected {version!r}")
    text = _changelog_path(root).read_text(encoding="utf-8")
    if not re.search(rf"^## \[{re.escape(version)}\]", text, re.MULTILINE):
        problems.append(f"CHANGELOG has no released `## [{version}]` section")
    if not _UNRELEASED_RE.search(text):
        problems.append("CHANGELOG has no `## [Unreleased]` heading")
    return problems


def do_bump(root: Path, date: str | None = None) -> str:
    """Bump pyproject + promote CHANGELOG to the next patch; return new version."""
    cur = read_current_version(root)
    new = compute_next_patch(cur)
    # CHANGELOG first: its idempotency guard fails loud BEFORE we touch
    # pyproject, so a re-run cannot leave a half-applied bump.
    promote_changelog(root, new, date=date)
    rewrite_pyproject_version(root, new)
    return new


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="autobump", description=__doc__)
    p.add_argument("--root", type=Path, default=None, help="repo root (test override)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("current-version")
    sub.add_parser("next-version")
    b = sub.add_parser("bump")
    b.add_argument("--date", default=None, help="override the UTC date (tests)")
    v = sub.add_parser("verify")
    v.add_argument(
        "--version", required=True, help="the version to assert, e.g. 0.24.2"
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    root = (args.root or _root_from_here()).resolve()
    try:
        if args.cmd == "current-version":
            print(read_current_version(root))
        elif args.cmd == "next-version":
            print(compute_next_patch(read_current_version(root)))
        elif args.cmd == "bump":
            print(do_bump(root, date=args.date))
        elif args.cmd == "verify":
            version = args.version.lstrip("v")
            problems = verify_consistency(root, version)
            if problems:
                for pr in problems:
                    print(f"::error::autobump verify: {pr}", file=sys.stderr)
                return EXIT_INCONSISTENT
            print(f"OK: pyproject + CHANGELOG are consistent at {version}")
    except AutobumpError as exc:
        print(f"::error::autobump: {exc}", file=sys.stderr)
        return EXIT_INCONSISTENT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# EOF
