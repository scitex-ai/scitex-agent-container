"""Provision and gate agent-owned worktrees through the neutral policy CLI.

SAC owns lifecycle mechanics only. Every allow/deny decision is delegated to
the operator-owned ``scitex-worktree-policy`` executable; no policy rule is
implemented here.
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .._runtime_paths import runtime_base_dir

__all__ = [
    "DEFAULT_WORKTREE_POLICY_CLI",
    "WorktreePlan",
    "WorktreePolicyError",
    "WorktreePolicyProof",
    "enforce_task_worktree_policy",
    "plan_task_worktree",
    "refresh_task_worktree_owner",
    "worktree_policy_artifact",
]

DEFAULT_WORKTREE_POLICY_CLI = (
    Path.home() / ".dotfiles" / "src" / ".bin" / "scitex-worktree-policy"
)
_SHA256_LENGTH = 64
_OWNER_FILE = "worktree-owner.json"


class WorktreePolicyError(RuntimeError):
    """A write-capable task has no valid external policy approval."""


@dataclass(frozen=True)
class WorktreePlan:
    authored_workdir: str
    resolved_workdir: str
    repo_root: str
    branch: str
    action: str
    owner_file: str


@dataclass(frozen=True)
class WorktreePolicyProof:
    policy_id: str
    schema_version: int
    policy_sha256: str
    projection_sha256: str
    repo_root: str
    branch: str
    surface: str
    authored_workdir: str = ""
    resolved_workdir: str = ""
    worktree_action: str = "reuse-explicit"

    def as_artifact(self) -> dict[str, str | int]:
        return dict(vars(self))


def _diagnostic(stderr: str) -> str:
    return stderr.strip() or "no diagnostic on stderr"


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


def _git(
    directory: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreePolicyError(
            f"git inspection failed in {directory}: {exc}"
        ) from exc
    if check and result.returncode != 0:
        raise WorktreePolicyError(
            f"git {' '.join(args)} failed in {directory}: {_diagnostic(result.stderr)}"
        )
    return result


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


def _slug(value: str) -> str:
    rendered = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not rendered:
        raise WorktreePolicyError("agent name cannot form a worktree identity")
    digest = hashlib.sha256(value.encode()).hexdigest()[:8]
    return f"{rendered[:48]}-{digest}"


def _owner_path(config: Any) -> Path:
    return runtime_base_dir() / str(config.name) / _OWNER_FILE


def _owner_record(config: Any, plan: WorktreePlan) -> dict[str, str]:
    claude = getattr(config, "claude", None)
    return {
        "agent": str(config.name),
        "spec": str(getattr(config, "config_path", "") or ""),
        "repo_root": plan.repo_root,
        "worktree": plan.resolved_workdir,
        "branch": plan.branch,
        "session": str(getattr(claude, "session", "") or ""),
        "resume_id": str(getattr(claude, "resume_id", "") or ""),
        "incarnation": str(
            (getattr(config, "env", {}) or {}).get("SAC_INSTANCE_UUID", "")
        ),
    }


def _read_owner(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorktreePolicyError(
            f"cannot read worktree ownership {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise WorktreePolicyError(f"worktree ownership {path} is not a JSON object")
    return value


def _write_owner(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(dict(record), sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _dirty(repo: Path) -> list[str]:
    output = _git(repo, "status", "--porcelain=v1").stdout
    return [
        line
        for line in output.splitlines()
        if line and line.strip() != "?? .worktrees/"
    ]


def _assert_owner(config: Any, plan: WorktreePlan, *, allow_missing: bool) -> None:
    owner = _read_owner(Path(plan.owner_file))
    expected = _owner_record(config, plan)
    if owner is None and allow_missing:
        return
    if owner is None:
        raise WorktreePolicyError(
            f"worktree {plan.resolved_workdir} exists without SAC ownership; "
            "refusing to adopt or overwrite it"
        )
    for key in ("agent", "repo_root", "worktree", "branch"):
        if owner.get(key) != expected[key]:
            raise WorktreePolicyError(
                f"worktree ownership conflict at {plan.owner_file}: "
                f"{key}={owner.get(key)!r}, expected {expected[key]!r}"
            )


def _planned_from_authority(config: Any, info: Mapping[str, Any]) -> WorktreePlan:
    repo_root = Path(_text(info, "repo_root")).resolve()
    name = _slug(str(config.name))
    target = repo_root / ".worktrees" / f"sac-{name}"
    return WorktreePlan(
        authored_workdir=str(Path(str(config.expanded_workdir)).expanduser().resolve()),
        resolved_workdir=str(target),
        repo_root=str(repo_root),
        branch=f"feature/sac-{name}",
        action="reuse" if target.exists() else "create",
        owner_file=str(_owner_path(config)),
    )


def _branch_exists(plan: WorktreePlan) -> bool:
    return (
        _git(
            Path(plan.repo_root),
            "show-ref",
            "--verify",
            f"refs/heads/{plan.branch}",
            check=False,
        ).returncode
        == 0
    )


def _worktree_add_argv(plan: WorktreePlan, *, branch_exists: bool) -> list[str]:
    argv = ["git", "worktree", "add"]
    if branch_exists:
        return [*argv, plan.resolved_workdir, plan.branch]
    return [*argv, "-b", plan.branch, plan.resolved_workdir]


def _authorize_create(
    cli: Path, plan: WorktreePlan, *, branch_exists: bool, timeout_s: float
) -> None:
    command = shlex.join(_worktree_add_argv(plan, branch_exists=branch_exists))
    result = _invoke(
        cli,
        ["check-shell", "--cwd", plan.repo_root, "--command", command],
        timeout_s=timeout_s,
    )
    if result.get("decision") != "allow":
        raise WorktreePolicyError("worktree policy did not authorize provisioning")


def _apply_runtime_workdir(config: Any, plan: WorktreePlan) -> None:
    config.workdir = plan.resolved_workdir
    setattr(config, "_worktree_plan", plan)


def plan_task_worktree(
    config: Any,
    *,
    provision: bool,
    cli_path: Path | str = DEFAULT_WORKTREE_POLICY_CLI,
    timeout_s: float = 10.0,
) -> WorktreePlan | None:
    """Resolve, optionally provision, and select the task's linked worktree."""
    if str(getattr(config, "kind", "Agent")) == "AgentProxy":
        return None
    cli = Path(cli_path).expanduser()
    authored = Path(str(config.expanded_workdir)).expanduser().resolve()
    info = _invoke(cli, ["inspect", "--repo", str(authored)], timeout_s=timeout_s)
    surface = _text(info, "surface")
    if surface == "linked-worktree":
        plan = WorktreePlan(
            authored_workdir=str(authored),
            resolved_workdir=str(authored),
            repo_root=_text(info, "repo_root"),
            branch=_text(info, "branch"),
            action="reuse-explicit",
            owner_file=str(_owner_path(config)),
        )
        owner = _read_owner(Path(plan.owner_file))
        if owner is None and _dirty(authored):
            raise WorktreePolicyError(
                f"unowned linked worktree {authored} is dirty; refusing adoption"
            )
        _assert_owner(config, plan, allow_missing=owner is None)
        if provision:
            _write_owner(Path(plan.owner_file), _owner_record(config, plan))
        _apply_runtime_workdir(config, plan)
        return plan

    _invoke(
        cli,
        ["assert-context", "--repo", str(authored), "--intent", "read"],
        timeout_s=timeout_s,
    )
    changed = _dirty(authored)
    if changed:
        preview = "; ".join(changed[:12])
        raise WorktreePolicyError(
            f"authority checkout {authored} is dirty; preserve/migrate these changes "
            f"before SAC can create an agent worktree: {preview}"
        )
    plan = _planned_from_authority(config, info)
    target = Path(plan.resolved_workdir)
    owner_path = Path(plan.owner_file)
    owner = _read_owner(owner_path)
    branch_exists = _branch_exists(plan)
    if not target.exists() and branch_exists != (owner is not None):
        detail = (
            "branch exists without ownership"
            if branch_exists
            else "ownership exists without branch"
        )
        raise WorktreePolicyError(
            f"worktree ownership conflict for {target}: {detail}; refusing recovery"
        )
    if target.exists():
        _assert_owner(config, plan, allow_missing=False)
        target_info = _invoke(
            cli, ["inspect", "--repo", str(target)], timeout_s=timeout_s
        )
        if (
            _text(target_info, "git_common_dir") != _text(info, "git_common_dir")
            or _text(target_info, "branch") != plan.branch
            or _text(target_info, "surface") != "linked-worktree"
        ):
            raise WorktreePolicyError(
                f"owned worktree {target} no longer matches its repository/branch"
            )
        if provision:
            _write_owner(owner_path, _owner_record(config, plan))
    else:
        _authorize_create(cli, plan, branch_exists=branch_exists, timeout_s=timeout_s)
    if not target.exists() and provision:
        if owner is not None:
            _assert_owner(config, plan, allow_missing=False)
        command = _worktree_add_argv(plan, branch_exists=branch_exists)[1:]
        _git(Path(plan.repo_root), *command)
        _write_owner(owner_path, _owner_record(config, plan))
    _apply_runtime_workdir(config, plan)
    return plan


def enforce_task_worktree_policy(
    config: Any,
    *,
    provision: bool = True,
    cli_path: Path | str = DEFAULT_WORKTREE_POLICY_CLI,
    timeout_s: float = 10.0,
) -> WorktreePolicyProof | None:
    """Provision/reuse and approve a write-capable agent task."""
    if str(getattr(config, "kind", "Agent")) == "AgentProxy":
        return None
    cli = Path(cli_path).expanduser()
    projection = _invoke(cli, ["check-projections"], timeout_s=timeout_s)
    if projection.get("decision") != "current":
        raise WorktreePolicyError("worktree policy projections were not current")
    policy_sha = _sha256(projection, "policy_sha256")
    projection_sha = _sha256(projection, "projection_sha256")
    plan = plan_task_worktree(
        config, provision=provision, cli_path=cli, timeout_s=timeout_s
    )
    assert plan is not None
    target = Path(plan.resolved_workdir)
    if target.exists():
        context = _invoke(
            cli,
            ["assert-context", "--repo", str(target), "--intent", "edit"],
            timeout_s=timeout_s,
        )
    else:
        context = {
            **_invoke(
                cli,
                ["inspect", "--repo", plan.repo_root],
                timeout_s=timeout_s,
            ),
            "repo_root": plan.resolved_workdir,
            "branch": plan.branch,
            "surface": "planned-linked-worktree",
        }
    context_policy = _sha256(context, "policy_sha256")
    context_projection = _sha256(context, "projection_sha256")
    if (context_policy, context_projection) != (policy_sha, projection_sha):
        raise WorktreePolicyError(
            "worktree policy identity changed between projection and context checks"
        )
    schema_version = context.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise WorktreePolicyError("worktree policy result has no valid schema_version")
    proof = WorktreePolicyProof(
        policy_id=_text(context, "policy_id"),
        schema_version=schema_version,
        policy_sha256=context_policy,
        projection_sha256=context_projection,
        repo_root=_text(context, "repo_root"),
        branch=_text(context, "branch"),
        surface=_text(context, "surface"),
        authored_workdir=plan.authored_workdir,
        resolved_workdir=plan.resolved_workdir,
        worktree_action=plan.action,
    )
    setattr(config, "_worktree_policy_proof", proof)
    return proof


def refresh_task_worktree_owner(config: Any) -> None:
    """Persist launch identity after SAC allocates the incarnation UUID."""
    plan = getattr(config, "_worktree_plan", None)
    if not isinstance(plan, WorktreePlan):
        return
    if not Path(plan.resolved_workdir).is_dir():
        raise WorktreePolicyError(
            f"resolved worktree disappeared before launch: {plan.resolved_workdir}"
        )
    _assert_owner(config, plan, allow_missing=False)
    _write_owner(Path(plan.owner_file), _owner_record(config, plan))


def worktree_policy_artifact(config: Any) -> dict[str, str | int] | None:
    proof = getattr(config, "_worktree_policy_proof", None)
    return proof.as_artifact() if isinstance(proof, WorktreePolicyProof) else None
