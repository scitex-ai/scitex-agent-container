"""Resolve and stage metadata needed by source-bundled image builds."""

from __future__ import annotations

import shutil
from pathlib import Path


def _locate_bundled_sibling(
    pkg_root: Path, name: str, *, editable_rel: str | None = None
) -> Path:
    """Return the path to a wheel-bundled or repo-root file by name.

    Resolution order — both supported because sac is installed BOTH
    ways in real use:

      1. ``pkg_root/_bundled/<name>`` — wheel install. The wheel
         ships the file via ``[tool.hatch.build.targets.wheel.
         force-include]`` in the repo's own pyproject.toml.
      2. ``pkg_root.parent.parent/<editable_rel or name>`` — editable
         install (``pip install -e .``). The package is at
         ``<repo>/src/scitex_agent_container/``; ``parent.parent``
         walks up through ``src/`` to the repo root.

    ``editable_rel`` exists because a file's slot in the wheel's FLAT
    ``_bundled/`` dir need not mirror its path in the repo:
    ``hatch_build.py`` bundles to ``_bundled/hatch_build.py`` but lives
    at ``<repo>/scripts/hatch_build.py``. Defaults to ``name`` (the
    repo-root case: pyproject.toml, README.md).

    Raises
    ------
    FileNotFoundError
        If neither location holds the file. Hard failure — the SIF
        build can't produce a pip-installable source tree without it.
        No silent fallback.
    """
    bundled = pkg_root / "_bundled" / name
    if bundled.is_file():
        return bundled
    editable_repo = pkg_root.parent.parent / (editable_rel or name)
    if editable_repo.is_file():
        return editable_repo
    raise FileNotFoundError(
        f"could not locate {name} for source-bundled SIF build. "
        f"checked:\n  - {bundled} (wheel install / force-included)\n"
        f"  - {editable_repo} (editable install / repo root)\n"
        f"ensure the wheel ships {name} under _bundled/ or run "
        "from an editable install."
    )


_DICTIONARY_SOURCE = ".scitex/dev/cli-audit-dict.yaml"
_DICTIONARY_WHEEL = "scitex_agent_container/_cli_audit_dict.yaml"


def locate_cli_dictionary(
    pkg_root: Path, pyproject_path: Path, project_data: dict
) -> Path | None:
    """Find declared CLI vocabulary before any existing staging tree is reset.

    Older distributions without this build input remain supported. Declared
    inputs must exist within their source root or installed package root.
    """
    targets = (
        project_data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
    )
    includes = targets.get("wheel", {}).get("force-include", {})
    if _DICTIONARY_SOURCE not in includes:
        return None
    if includes[_DICTIONARY_SOURCE] != _DICTIONARY_WHEEL:
        raise ValueError("Unsupported SAC CLI dictionary wheel destination")
    candidates = (
        (pyproject_path.parent / _DICTIONARY_SOURCE, pyproject_path.parent),
        (pkg_root / "_cli_audit_dict.yaml", pkg_root),
    )
    for candidate, root in candidates:
        if candidate.is_file():
            if not candidate.resolve().is_relative_to(root.resolve()):
                raise ValueError("SAC CLI dictionary escapes its source root")
            return candidate
    raise FileNotFoundError("Declared SAC CLI dictionary is missing from build inputs")


def stage_cli_dictionary(source: Path | None, staged_root: Path) -> None:
    """Reconstruct the canonical build input from source or installed data."""
    if source is None:
        return
    destination = staged_root / _DICTIONARY_SOURCE
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
