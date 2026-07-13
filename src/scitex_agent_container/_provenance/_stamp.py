#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_stamp.py

"""Compute the build stamp baked into a distribution as ``_build_info.py``.

Pure functions — no hatchling import — so the logic is unit-testable
without a build frontend. ``hatch_build.py`` at the repo root is only a
thin adapter that calls ``compute_stamp`` + ``render_module``.

WHY A BAKED STAMP AT ALL: a pip-installed copy has no ``.git``, so the
commit can only be known if the build writes it down. The runtime prefers
a LIVE git read when it is running from a checkout (a stamp written at
``pip install -e`` time goes stale the moment you commit), and falls back
to the stamp when there is no checkout — which is precisely the
wheel/SIF case where this bites hardest.

COMMIT PRECEDENCE (order matters, and the third entry is not optional):

1. ``SAC_BUILD_COMMIT`` — an explicit override CI can inject.
2. A live ``.git`` at the project root — the normal repo build.
3. **An existing ``_build_info.py`` already in the source tree.**
   ``python -m build`` (what release CI runs) builds the sdist first and
   then builds the wheel FROM THE UNPACKED SDIST — a temp dir with no
   ``.git``. Without this rule the sdist would carry the real commit and
   the published wheel would carry ``unknown``, which is the only
   artifact anyone actually installs.

If all three miss, the stamp still carries ``code_hash`` — a content
digest of the tree being built. So the scheme never degrades to useless:
there is always something that changes when the code changes.
"""

from __future__ import annotations

import ast
import os
from datetime import datetime, timezone
from pathlib import Path

from ._git import head_sha
from ._hash import code_hash

__all__ = ["BUILD_INFO_NAME", "compute_stamp", "read_existing_stamp", "render_module"]

BUILD_INFO_NAME = "_build_info.py"
COMMIT_ENV_VAR = "SAC_BUILD_COMMIT"


def read_existing_stamp(package_dir: Path) -> dict | None:
    """Parse an already-baked ``_build_info.py`` WITHOUT importing it.

    The file may belong to a half-built tree, so it is parsed as data
    (``ast.literal_eval`` on the ``STAMP`` assignment) rather than
    executed.
    """
    path = Path(package_dir) / BUILD_INFO_NAME
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:  # stx-allow: fallback (reason: no stamp yet is the normal first-build case)
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:  # stx-allow: fallback (reason: a corrupt generated file must not break the build; we simply re-generate)
        return None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id == "STAMP":
                try:
                    value = ast.literal_eval(node.value)
                except ValueError:  # stx-allow: fallback (reason: same as above — regenerate rather than fail the build)
                    return None
                return value if isinstance(value, dict) else None
    return None


def compute_stamp(root: Path, package_dir: Path, version: str) -> dict:
    """Build the stamp dict for a distribution rooted at ``root``.

    ``root`` is the project root (holding ``.git`` when built from a
    checkout); ``package_dir`` is the source package being packaged.
    """
    root = Path(root)
    package_dir = Path(package_dir)

    commit = (os.environ.get(COMMIT_ENV_VAR) or "").strip() or None
    source = "env" if commit else None

    if commit is None:
        commit = head_sha(root)
        source = "git" if commit else None

    if commit is None:
        # sdist -> wheel: the sdist stage already wrote the real commit.
        previous = read_existing_stamp(package_dir) or {}
        commit = previous.get("commit") or None
        source = "inherited" if commit else "unknown"

    return {
        "version": version,
        "commit": commit,
        "commit_source": source,
        "code_hash": code_hash(package_dir),
        "built_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def render_module(stamp: dict) -> str:
    """Render the stamp as an importable module.

    A plain dict literal — no imports, no logic — so importing it at
    runtime costs one small file read and cannot fail in a way that
    breaks ``sac --version``.
    """
    lines = [
        "#!/usr/bin/env python3",
        f"# File: src/scitex_agent_container/_provenance/{BUILD_INFO_NAME}",
        "",
        '"""GENERATED AT BUILD TIME by hatch_build.py — do not edit, do not commit.',
        "",
        "Records which commit this distribution was built from, so an installed",
        'copy can answer "is my fix actually in here?" without a .git dir.',
        '"""',
        "",
        "STAMP = {",
    ]
    for key in ("version", "commit", "commit_source", "code_hash", "built_at"):
        lines.append(f"    {key!r}: {stamp.get(key)!r},")
    lines += ["}", "", "# EOF", ""]
    return "\n".join(lines)


# EOF
