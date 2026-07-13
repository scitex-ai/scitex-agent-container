#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/_audit.py

"""The exhaustive checks behind ``sac provenance`` — the ones that cost.

``sac --version`` answers "what is loaded". This answers "and is anything
about that a LIE?". It is a separate command because hashing the tree is
~35 ms and ``--version`` is typed constantly; see ``_identity`` for the
split.

Four failure modes, each of which has actually happened:

* ``shadowed``     — the imported module is NOT the installed
  distribution. A bare ``pytest`` under ``/opt/venv-sac`` imports the
  INSTALLED package, not your worktree; a run can report "1087 passed"
  having tested none of your changes.
* ``duplicate-dist`` — more than one ``.dist-info`` for this package.
  The loser is a fossil that keeps advertising a version whose code is
  gone.
* ``patched``      — the installed ``.py`` bytes no longer hash to what
  the build recorded. Someone edited site-packages in place.
* ``version-mismatch`` — the ``.dist-info`` version disagrees with the
  version baked into the code. Classic fossil ``.dist-info``.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

from ._hash import code_hash
from ._identity import DIST_NAME, identity, package_dir

__all__ = ["audit", "find_dist_infos"]

_MODULE_NAME = "scitex_agent_container"
_DIST_GLOBS = (
    "scitex_agent_container-*.dist-info",
    "scitex-agent-container-*.dist-info",
    "scitex_agent_container*.egg-info",
)


def find_dist_infos(path_entries: Sequence[str] | None = None) -> list[Path]:
    """Every ``.dist-info``/``.egg-info`` for this package on the import path.

    More than one means a fossil install is shadowing (or being shadowed
    by) the real one. Globbing the path entries directly is both cheaper
    and more honest than ``importlib.metadata.distributions()``, which
    dedupes by name and so HIDES the very duplication we are hunting.

    ``path_entries`` defaults to the live ``sys.path``; it is a parameter
    so tests can point it at real directories they built, instead of
    having to mutate global interpreter state.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for entry in sys.path if path_entries is None else path_entries:
        if not entry:
            continue
        base = Path(entry)
        if not base.is_dir():
            continue
        for pattern in _DIST_GLOBS:
            for match in base.glob(pattern):
                key = str(match.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(match)
    return sorted(found)


def _is_editable(dist_info: Path) -> bool:
    """True if this dist was installed with ``pip install -e`` (PEP 610).

    An editable install legitimately serves its module from outside
    site-packages, so it must NOT be reported as ``shadowed``.
    """
    try:
        raw = (dist_info / "direct_url.json").read_text(encoding="utf-8")
    except OSError:  # stx-allow: fallback (reason: absent direct_url.json simply means "not a direct/editable install")
        return False
    try:
        data = json.loads(raw)
    except ValueError:  # stx-allow: fallback (reason: a malformed metadata file must not crash the diagnostic that reports on it)
        return False
    return bool(data.get("dir_info", {}).get("editable"))


def _dist_metadata_version() -> str | None:
    try:
        return distribution(DIST_NAME).version
    except PackageNotFoundError:  # stx-allow: fallback (reason: source-tree run with nothing installed)
        return None


def _check_shadowed(info: dict, dist_infos: list[Path], anomalies: list) -> None:
    """Flag when the imported module is not the installed distribution."""
    origin = Path(info["origin"]).resolve()
    for dist_info in dist_infos:
        if _is_editable(dist_info):
            continue
        installed = (dist_info.parent / _MODULE_NAME).resolve()
        if installed.is_dir() and installed != origin:
            anomalies.append(
                {
                    "code": "shadowed",
                    "detail": (
                        f"imports resolve to {origin} but the installed "
                        f"distribution lives at {installed} — the code you are "
                        f"running is NOT the code that is installed"
                    ),
                }
            )
            return


def _check_duplicates(dist_infos: list[Path], anomalies: list) -> None:
    if len(dist_infos) > 1:
        joined = ", ".join(str(p) for p in dist_infos)
        anomalies.append(
            {
                "code": "duplicate-dist",
                "detail": (
                    f"{len(dist_infos)} distributions found for {DIST_NAME}: "
                    f"{joined} — one of these is a fossil advertising a "
                    f"version whose code is gone"
                ),
            }
        )


def _check_patched(info: dict, live_hash: str | None, anomalies: list) -> None:
    """Flag installed bytes that no longer match the build-time digest."""
    baked_hash = info.get("code_hash")
    if not baked_hash or not live_hash or info["install"] != "wheel":
        return
    if baked_hash != live_hash:
        anomalies.append(
            {
                "code": "patched",
                "detail": (
                    f"installed .py files hash to {live_hash} but this build "
                    f"recorded {baked_hash} — site-packages has been edited "
                    f"in place since it was built"
                ),
            }
        )


def _check_version_mismatch(info: dict, anomalies: list) -> None:
    declared = _dist_metadata_version()
    baked_version = info.get("baked_version")
    if declared and baked_version and declared != baked_version:
        anomalies.append(
            {
                "code": "version-mismatch",
                "detail": (
                    f".dist-info says {declared} but the loaded code was built "
                    f"as {baked_version} — the .dist-info is a fossil"
                ),
            }
        )


def audit() -> dict:
    """Full provenance report. Costs ~35 ms (it hashes the loaded tree)."""
    info = identity()
    from ._identity import _baked

    info["baked_version"] = _baked().get("version")

    live_hash = code_hash(package_dir())
    dist_infos = find_dist_infos()
    anomalies: list[dict] = []

    _check_shadowed(info, dist_infos, anomalies)
    _check_duplicates(dist_infos, anomalies)
    _check_patched(info, live_hash, anomalies)
    _check_version_mismatch(info, anomalies)

    return {
        **info,
        "live_code_hash": live_hash,
        "dist_infos": [str(p) for p in dist_infos],
        "python": sys.executable,
        "anomalies": anomalies,
        "ok": not anomalies,
    }


# EOF
