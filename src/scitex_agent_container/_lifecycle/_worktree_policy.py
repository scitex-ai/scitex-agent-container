"""Fail-closed adapter to the harness-neutral worktree policy CLI.

Policy does not live in this package.  SAC asks the operator-owned
``scitex-worktree-policy`` executable whether a write-capable task may start,
then records the returned policy/projection identities on the launch config.
The executable remains the only decision engine.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_WORKTREE_POLICY_CLI",
    "WorktreePolicyError",
    "WorktreePolicyProof",
    "enforce_task_worktree_policy",
    "worktree_policy_artifact",
]

DEFAULT_WORKTREE_POLICY_CLI = (
    Path.home() / ".dotfiles" / "src" / ".bin" / "scitex-worktree-policy"
)
_SHA256_LENGTH = 64


class WorktreePolicyError(RuntimeError):
    """A write-capable task has no valid external policy approval."""


@dataclass(frozen=True)
class WorktreePolicyProof:
    """Identity and context returned by the external decision engine."""

    policy_id: str
    schema_version: int
    policy_sha256: str
    projection_sha256: str
    repo_root: str
    branch: str
    surface: str

    def as_artifact(self) -> dict[str, str | int]:
        return {
            "policy_id": self.policy_id,
            "schema_version": self.schema_version,
            "policy_sha256": self.policy_sha256,
            "projection_sha256": self.projection_sha256,
            "repo_root": self.repo_root,
            "branch": self.branch,
            "surface": self.surface,
        }


def _diagnostic(stderr: str) -> str:
    text = stderr.strip()
    return text if text else "no diagnostic on stderr"


def _invoke(cli: Path, args: list[str], *, timeout_s: float) -> dict[str, Any]:
    try:
        result = subprocess.run(
            [str(cli), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_s,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreePolicyError(
            f"worktree policy CLI unavailable at {cli}: {exc}"
        ) from exc
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise WorktreePolicyError(
            f"worktree policy CLI returned invalid JSON for {' '.join(args)}: "
            f"{_diagnostic(result.stderr)}"
        ) from exc
    if not isinstance(payload, dict):
        raise WorktreePolicyError("worktree policy CLI JSON must be an object")
    if result.returncode != 0:
        reason = str(payload.get("reason") or payload.get("error") or "").strip()
        raise WorktreePolicyError(
            f"worktree policy refused task launch (exit {result.returncode}): "
            f"{reason or _diagnostic(result.stderr)}"
        )
    if payload.get("allowed") is not True:
        raise WorktreePolicyError(
            "worktree policy CLI exited successfully without an explicit allow"
        )
    return payload


def _sha256(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        raise WorktreePolicyError(f"worktree policy result has no valid {key}")
    try:
        bytes.fromhex(value)
    except ValueError as exc:
        raise WorktreePolicyError(f"worktree policy result has no valid {key}") from exc
    return value


def _text(payload: Mapping[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorktreePolicyError(f"worktree policy result has no valid {key}")
    return value


def enforce_task_worktree_policy(
    config: Any,
    *,
    cli_path: Path | str = DEFAULT_WORKTREE_POLICY_CLI,
    timeout_s: float = 10.0,
) -> WorktreePolicyProof | None:
    """Approve a write-capable agent task before its harness is started.

    ``AgentProxy`` launches no shell/file-capable harness and is therefore
    outside this gate.  Every actual ``Agent`` harness (Claude, Codex,
    Hermes, and future registry entries) takes the same path.
    """
    if str(getattr(config, "kind", "Agent")) == "AgentProxy":
        return None

    cli = Path(cli_path).expanduser()
    projection = _invoke(cli, ["check-projections"], timeout_s=timeout_s)
    if projection.get("decision") != "current":
        raise WorktreePolicyError(
            "worktree policy projections were not reported current"
        )
    projection_policy_sha = _sha256(projection, "policy_sha256")
    projection_sha = _sha256(projection, "projection_sha256")

    workdir = Path(str(getattr(config, "expanded_workdir"))).expanduser().resolve()
    context = _invoke(
        cli,
        ["assert-context", "--repo", str(workdir), "--intent", "edit"],
        timeout_s=timeout_s,
    )
    if context.get("decision") != "allow":
        raise WorktreePolicyError("worktree policy did not return an allow decision")
    context_policy_sha = _sha256(context, "policy_sha256")
    context_projection_sha = _sha256(context, "projection_sha256")
    if (context_policy_sha, context_projection_sha) != (
        projection_policy_sha,
        projection_sha,
    ):
        raise WorktreePolicyError(
            "worktree policy identity changed between projection and context checks"
        )
    schema_version = context.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise WorktreePolicyError("worktree policy result has no valid schema_version")

    proof = WorktreePolicyProof(
        policy_id=_text(context, "policy_id"),
        schema_version=schema_version,
        policy_sha256=context_policy_sha,
        projection_sha256=context_projection_sha,
        repo_root=_text(context, "repo_root"),
        branch=_text(context, "branch"),
        surface=_text(context, "surface"),
    )
    setattr(config, "_worktree_policy_proof", proof)
    return proof


def worktree_policy_artifact(config: Any) -> dict[str, str | int] | None:
    """Return the launch artifact attached by the successful gate."""
    proof = getattr(config, "_worktree_policy_proof", None)
    return proof.as_artifact() if isinstance(proof, WorktreePolicyProof) else None
