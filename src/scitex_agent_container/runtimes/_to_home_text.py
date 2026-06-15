"""Text helpers for the to_home/ materialization pipeline.

Extracted from :mod:`_to_home` to keep that module under the 512-line
file-size cap (2026-06-15 — see ``GITIGNORED/REFACTORING.md``).
Contains the marker constants, marker-invariant validator, user-tail
extractor, and ``${VAR}`` / ``${metadata.*}`` interpolators used by
:func:`_deploy_marker_protected` and :func:`_deploy_plain_file` in
the orchestrator module.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import AgentConfig
from ._to_home_errors import WorkspaceCLAUDEMarkerError

END_MARKER = "<!-- End of scitex-agent-container generated section -->"
START_MARKER_PREFIX = "<!-- Start of scitex-agent-container generated section"


def validate_marker_invariants(text: str, source_name: str) -> None:
    """Hard-fail if Start/End markers are missing or malformed."""
    start_count = text.count(START_MARKER_PREFIX)
    end_count = text.count(END_MARKER)
    if start_count != 1 or end_count != 1:
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: expected exactly 1 Start marker and 1 End "
            f"marker, found Start={start_count} End={end_count}. "
            "Refusing to deploy to avoid data loss. Restore the markers "
            "manually before retrying."
        )
    if text.find(START_MARKER_PREFIX) > text.find(END_MARKER):
        raise WorkspaceCLAUDEMarkerError(
            f"{source_name}: Start marker appears AFTER End marker. "
            "This indicates a corrupted file. Refusing to deploy."
        )


def extract_user_tail(workspace_path: Path) -> str:
    """Return content past the End marker in an existing workspace file.

    Empty string when the file is missing, unreadable, or has no End
    marker. Used to preserve user-appended content across a re-deploy
    of marker-protected files (CLAUDE.md / state.md).
    """
    if not workspace_path.exists():
        return ""
    try:
        existing = workspace_path.read_text()
    except OSError:  # stx-allow: fallback (reason: file system operation failure)
        return ""
    idx = existing.rfind(END_MARKER)
    if idx == -1:
        return ""
    return existing[idx + len(END_MARKER) :]


def interpolate_env(text: str) -> str:
    """Substitute ``${VAR}`` with ``os.environ[VAR]``, leaving unknown
    refs untouched (so an unset env var becomes a visible artefact
    rather than silently collapsing to empty string).
    """
    return re.sub(
        r"\$\{(\w+)\}",
        lambda m: os.environ.get(m.group(1), m.group(0)),
        text,
    )


def interpolate_metadata(text: str, config: AgentConfig) -> str:
    """Substitute ``${metadata.name}`` and ``${metadata.labels.<k>}``
    against ``config``. Unknown keys pass through unchanged.
    """

    def _replace(m: re.Match) -> str:
        key = m.group(1)
        if key == "metadata.name":
            return config.name
        if key.startswith("metadata.labels."):
            label = key[len("metadata.labels.") :]
            return config.labels.get(label) or m.group(0)
        return m.group(0)

    return re.sub(r"\$\{([^}]+)\}", _replace, text)


__all__ = [
    "END_MARKER",
    "START_MARKER_PREFIX",
    "extract_user_tail",
    "interpolate_env",
    "interpolate_metadata",
    "validate_marker_invariants",
]
