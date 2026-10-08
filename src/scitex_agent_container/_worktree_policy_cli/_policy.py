"""Selected policy configuration, proof hashes, and generated projections."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .._logging import get_logger, render_content

PACKAGED_POLICY = (
    Path(__file__).parent.parent
    / "_baseline_assets"
    / "worktree_policy"
    / "worktree-policy.json"
)
HARNESS_NAMES = ("AGENTS", "CLAUDE", "HERMES")


def host_policy_path() -> Path:
    """Resolve configuration beneath the host's SAC root."""
    explicit_home = os.environ.get("SCITEX_AGENT_CONTAINER_HOME")
    state_root = (
        Path(explicit_home).expanduser()
        if explicit_home
        else Path(os.environ.get("SCITEX_DIR", "~/.scitex")).expanduser()
        / "agent-container"
    )
    return state_root / "worktree-policy" / "worktree-policy.json"


def default_policy_path() -> Path:
    """Host policy overrides packaged defaults without depending on dotfiles."""
    configured = host_policy_path()
    if configured.exists() or configured.is_symlink():
        return configured
    return PACKAGED_POLICY


class PolicyError(Exception):
    def __init__(self, message: str, code: int = 3):
        super().__init__(message)
        self.code = code


def emit(payload: dict[str, Any], code: int, message: str | None = None) -> int:
    render_content(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    if message:
        get_logger(__name__).error(message)
    return code


def load_policy(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PolicyError(f"cannot load policy {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise PolicyError(f"policy {path} must be a JSON object")
    required = {
        "schema_version",
        "policy_id",
        "authority_branch",
        "topic_branch_prefixes",
        "worktree_directory",
        "exit_codes",
        "examples",
    }
    missing = sorted(required - data.keys())
    if missing:
        raise PolicyError(f"policy missing keys: {', '.join(missing)}")
    expected_codes = {"allow": 0, "deny": 2, "invalid": 3, "inspection_error": 4}
    if data["exit_codes"] != expected_codes:
        raise PolicyError(f"exit_codes must be {expected_codes}")
    return data, hashlib.sha256(raw).hexdigest()


def projection_hash(directory: Path) -> str | None:
    paths = [directory / f"{name}.worktree-policy.md" for name in HARNESS_NAMES]
    if not all(path.is_file() for path in paths):
        return None
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def render_projection(name: str, policy: dict[str, Any], source_hash: str) -> str:
    prefixes = ", ".join(f"`{p}`" for p in policy["topic_branch_prefixes"])
    examples = "\n".join(
        f"- `{item['command']}` → {item['outcome']}" for item in policy["examples"]
    )
    return f"""<!-- GENERATED. source-sha256: {source_hash} -->
# Worktree policy ({name})

This file is a discovery projection, not policy authority. The authoritative
manifest is selected by `sac worktree policy` from the SAC configuration root
or the installed package defaults; runtime decisions come only from that CLI.

- The authority checkout stays on `{policy["authority_branch"]}` and is read-only.
- All edits, commits, pushes, and branch changes happen in linked worktrees.
- Linked worktrees use topic prefixes: {prefixes}.
- Worktrees live below `{policy["worktree_directory"]}/`.
- There is no bypass, exemption, or permissive fallback.

Examples generated from the tested manifest fixtures:

{examples}
"""
