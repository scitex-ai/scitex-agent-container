"""Non-authorizing argument plan for policy projection generation."""

from pathlib import Path
from typing import Any


def projection_argument_preview(
    policy: Path | None, output_dir: Path | None
) -> dict[str, Any]:
    """Report supplied paths without choosing defaults or loading policy."""
    return {
        "allowed": False,
        "decision": "preview",
        "operation": "generate-projections",
        "dry_run": True,
        "scope": "arguments_only",
        "policy": str(policy) if policy is not None else None,
        "output_dir": str(output_dir) if output_dir is not None else None,
        "unverified": [
            "policy defaults, contents and identity",
            "projection directory, contents and hashes",
            "Git context and edit permission",
            "generation outcome",
        ],
    }
