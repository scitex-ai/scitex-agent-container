"""In-process harness-neutral worktree policy engine.

SAC owns this policy: every allow/deny decision is computed here from the
bundled ``worktree-policy.json`` manifest plus real Git inspection. Nothing
is delegated to an operator-side executable — the former
``~/.dotfiles/src/.bin/scitex-worktree-policy`` CLI was retired, and
requiring it made every agent start fail closed on hosts without it.

This module is a direct port of that retired CLI (verbs, decision rules,
and receipt shapes preserved) minus the process boundary: callers use
:func:`invoke` with the same verb/flag grammar instead of ``subprocess``.
Git itself is still shelled out to for repository inspection; Git is a
tool, not policy.

Operators can override the manifest and the generated projection directory
without code changes via ``SAC_WORKTREE_POLICY_PATH`` and
``SAC_WORKTREE_POLICY_PROJECTION_DIR``. After editing an override manifest,
regenerate its projections with :func:`generate_projections`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "HARNESS_NAMES",
    "WorktreePolicyError",
    "assert_context",
    "check_projections",
    "check_shell",
    "generate_projections",
    "inspect_repo",
    "invoke",
    "load_policy",
    "manifest_path",
    "projection_dir",
    "render_projection",
]

HARNESS_NAMES = ("AGENTS", "CLAUDE", "HERMES")

_MANIFEST_ENV = "SAC_WORKTREE_POLICY_PATH"
_PROJECTION_ENV = "SAC_WORKTREE_POLICY_PROJECTION_DIR"

_BUNDLED_MANIFEST = (
    Path(__file__).resolve().parent.parent
    / "_baseline_assets"
    / "worktree_policy"
    / "worktree-policy.json"
)


class WorktreePolicyError(RuntimeError):
    """A write-capable task has no valid policy approval."""


def manifest_path() -> Path:
    """Bundled manifest, unless an operator override is configured."""
    override = os.environ.get(_MANIFEST_ENV, "").strip()
    return Path(override).expanduser() if override else _BUNDLED_MANIFEST


def projection_dir() -> Path:
    """Generated-projection directory, unless an operator override applies."""
    override = os.environ.get(_PROJECTION_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return manifest_path().parent / "generated"


def load_policy(path: Path | None = None) -> tuple[dict[str, Any], str]:
    """Load and validate the manifest; return (policy, sha256 of raw bytes)."""
    manifest = (path or manifest_path()).expanduser()
    try:
        raw = manifest.read_bytes()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise WorktreePolicyError(f"cannot load policy {manifest}: {exc}") from exc
    if not isinstance(data, dict):
        raise WorktreePolicyError(f"policy {manifest} is not a JSON object")
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
        raise WorktreePolicyError(f"policy missing keys: {', '.join(missing)}")
    expected_codes = {"allow": 0, "deny": 2, "invalid": 3, "inspection_error": 4}
    if data["exit_codes"] != expected_codes:
        raise WorktreePolicyError(f"exit_codes must be {expected_codes}")
    return data, hashlib.sha256(raw).hexdigest()


def _run_git(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise WorktreePolicyError(
            f"git inspection failed in {directory}: {exc}"
        ) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise WorktreePolicyError(f"git inspection failed in {directory}: {detail}")
    return result.stdout.strip()


def projection_hash(directory: Path | None = None) -> str | None:
    """Hash of the generated harness projections, or None when incomplete."""
    target = directory or projection_dir()
    paths = [target / f"{name}.worktree-policy.md" for name in HARNESS_NAMES]
    if not all(path.is_file() for path in paths):
        return None
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def inspect_repo(
    directory: Path | str,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Resolve a checkout's neutral policy record (always allowed)."""
    directory = Path(directory).expanduser()
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    resolved = directory.resolve()
    root = Path(_run_git(resolved, "rev-parse", "--show-toplevel")).resolve()
    git_dir_raw = Path(_run_git(resolved, "rev-parse", "--git-dir"))
    common_raw = Path(_run_git(resolved, "rev-parse", "--git-common-dir"))
    git_dir = (
        (resolved / git_dir_raw).resolve()
        if not git_dir_raw.is_absolute()
        else git_dir_raw.resolve()
    )
    common_dir = (
        (resolved / common_raw).resolve()
        if not common_raw.is_absolute()
        else common_raw.resolve()
    )
    branch = _run_git(resolved, "rev-parse", "--abbrev-ref", "HEAD")
    surface = "authority" if git_dir == common_dir else "linked-worktree"
    return {
        "schema_version": policy["schema_version"],
        "policy_id": policy["policy_id"],
        "policy_sha256": source_hash,
        "projection_sha256": projection_hash(),
        "repo_root": str(root),
        "git_common_dir": str(common_dir),
        "branch": branch,
        "surface": surface,
    }


def is_topic(branch: str, policy: dict[str, Any]) -> bool:
    return any(branch.startswith(prefix) for prefix in policy["topic_branch_prefixes"])


def decide_context(
    info: dict[str, Any], intent: str, policy: dict[str, Any]
) -> tuple[bool, str]:
    """Authority checkouts are read-only; worktrees need topic branches."""
    branch = info["branch"]
    surface = info["surface"]
    authority = policy["authority_branch"]
    if surface == "authority":
        if branch != authority:
            return False, (
                f"authority checkout must remain on {authority!r}; found {branch!r}"
            )
        if intent != "read":
            return False, (
                f"authority checkout on {authority!r} is read-only; "
                f"{intent!r} requires a linked worktree"
            )
        return True, (
            "authority checkout is on the required branch and the intent is read-only"
        )
    if not is_topic(branch, policy):
        return False, (
            f"linked worktree branch {branch!r} is not an allowed topic branch"
        )
    if intent == "branch-change":
        return False, (
            "a linked worktree is branch-stable; "
            "create another linked worktree for another topic"
        )
    return True, "linked worktree is on an allowed topic branch"


def assert_context(
    repo: Path | str,
    intent: str,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Authorize an intent against a checkout; deny by raising."""
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    info = inspect_repo(repo, policy, source_hash)
    allowed, reason = decide_context(info, intent, policy)
    if not allowed:
        raise WorktreePolicyError(reason)
    return {**info, "allowed": True, "decision": "allow", "reason": reason}


def _split_shell(command: str) -> list[list[str]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|")
    lexer.whitespace_split = True
    lexer.commenters = ""
    tokens = list(lexer)
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and set(token) <= {";", "&", "|"}:
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _git_invocations(command: str, cwd: Path) -> list[tuple[str, Path, list[str]]]:
    invocations: list[tuple[str, Path, list[str]]] = []
    effective_cwd = cwd
    try:
        segments = _split_shell(command)
    except ValueError as exc:
        if "git" in command:
            raise WorktreePolicyError(
                f"cannot parse git command safely: {exc}"
            ) from exc
        return []
    for segment in segments:
        if segment[0] == "cd":
            if len(segment) != 2 or any(c in segment[1] for c in "$`"):
                raise WorktreePolicyError(
                    "cannot resolve dynamic or malformed cd before a git mutation"
                )
            candidate = Path(segment[1]).expanduser()
            effective_cwd = (
                (effective_cwd / candidate).resolve()
                if not candidate.is_absolute()
                else candidate.resolve()
            )
            continue
        try:
            git_at = segment.index("git")
        except ValueError:
            continue
        if git_at != 0:
            raise WorktreePolicyError(
                "wrapped git commands are not accepted; invoke git directly "
                "for deterministic policy evaluation"
            )
        args = segment[1:]
        target = effective_cwd
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag == "-C":
                if not args or any(c in args[0] for c in "$`"):
                    raise WorktreePolicyError("git -C requires a static path")
                raw = Path(args.pop(0)).expanduser()
                target = (
                    (target / raw).resolve() if not raw.is_absolute() else raw.resolve()
                )
            elif flag.startswith("-C"):
                raw_text = flag[2:]
                if not raw_text or any(c in raw_text for c in "$`"):
                    raise WorktreePolicyError("git -C requires a static path")
                raw = Path(raw_text).expanduser()
                target = (
                    (target / raw).resolve() if not raw.is_absolute() else raw.resolve()
                )
            elif flag == "-c":
                if not args:
                    raise WorktreePolicyError("git -c requires a value")
                args.pop(0)
        if args:
            invocations.append((args[0], target, args[1:]))
    return invocations


def check_shell(
    command: str,
    cwd: Path | str,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Authorize a shell string's git mutations; deny by raising."""
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    resolved_cwd = Path(cwd).expanduser().resolve()
    mutations = {"commit", "push", "checkout", "switch", "branch", "worktree"}
    checks: list[dict[str, Any]] = []
    invocations = _git_invocations(command, resolved_cwd)
    if not invocations and re.search(
        r"\bgit\s+(?:[^\s]+\s+)*(?:commit|push|checkout|switch|branch|worktree)\b",
        command,
    ):
        raise WorktreePolicyError(
            "git mutation was detected but could not be resolved as a direct invocation"
        )
    for subcommand, target, args in invocations:
        if subcommand not in mutations:
            continue
        if subcommand == "worktree" and (not args or args[0] != "add"):
            continue
        if subcommand == "checkout" and "--" in args:
            continue
        if subcommand == "branch" and not any(
            a in ("-d", "-D", "-m", "-M") for a in args
        ):
            continue
        info = inspect_repo(target, policy, source_hash)
        if subcommand == "worktree":
            branch: str | None = None
            path: str | None = None
            index = 1
            while index < len(args):
                arg = args[index]
                if arg in {"-b", "-B"}:
                    if index + 1 >= len(args):
                        raise WorktreePolicyError(
                            "git worktree add requires a branch after -b/-B"
                        )
                    branch = args[index + 1]
                    index += 2
                    continue
                if arg.startswith("-"):
                    index += 1
                    continue
                if path is None:
                    path = arg
                elif branch is None:
                    branch = arg
                index += 1
            if not path or not branch:
                allowed, reason = (
                    False,
                    "git worktree add must name a static path and topic branch",
                )
            elif any(c in path + branch for c in "$`"):
                allowed, reason = (
                    False,
                    "git worktree add path and branch must be static",
                )
            else:
                raw_path = Path(path).expanduser()
                resolved_path = (
                    (target / raw_path).resolve()
                    if not raw_path.is_absolute()
                    else raw_path.resolve()
                )
                authority_root = Path(info["git_common_dir"]).parent.resolve()
                required_parent = authority_root / policy["worktree_directory"]
                under_required = required_parent in resolved_path.parents
                allowed = (
                    info["surface"] == "authority"
                    and info["branch"] == policy["authority_branch"]
                    and under_required
                    and is_topic(branch, policy)
                )
                reason = (
                    "worktree add uses the authority checkout, required "
                    "directory, and a topic branch"
                    if allowed
                    else "worktree add requires authority/develop, "
                    "<authority>/.worktrees/, and an allowed topic branch"
                )
        else:
            allowed, reason = decide_context(
                info,
                "branch-change"
                if subcommand in {"checkout", "switch", "branch"}
                else subcommand,
                policy,
            )
        checks.append(
            {**info, "operation": subcommand, "allowed": allowed, "reason": reason}
        )
    denied = next((check for check in checks if not check["allowed"]), None)
    if denied is not None:
        raise WorktreePolicyError(denied["reason"])
    return {
        "allowed": True,
        "decision": "allow",
        "reason": "no forbidden git mutation was found",
        "checks": checks,
        "policy_sha256": source_hash,
        "projection_sha256": projection_hash(),
    }


def _canonical(path: Path, label: str) -> Path:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path != path.resolve()
        or any(ord(char) < 32 for char in str(path))
    ):
        raise WorktreePolicyError(
            f"{label} must be an absolute canonical path without traversal or symlinks"
        )
    return path


def approve_owned_restore(
    *,
    repo: Path | str,
    agent: str,
    owner_file: Path | str,
    expected_tip: str,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Approve recreating one absent, previously SAC-owned checkout.

    Returns only the existing-branch, no-force Git receipt whose ownership
    and state were checked. Any deviation denies by raising.
    """
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    settings = policy.get("owned_restore", {})
    if settings != {
        "enabled": True,
        "owner_file": "worktree-owner.json",
        "directory_prefix": "sac-",
        "branch_prefix": "feature/sac-",
    }:
        raise WorktreePolicyError(
            "the manifest does not enable the bounded SAC owned restore operation"
        )
    if policy["worktree_directory"] != ".worktrees":
        raise WorktreePolicyError(
            "owned restore requires the canonical .worktrees storage directory"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", agent):
        raise WorktreePolicyError("invalid agent identity")
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_tip):
        raise WorktreePolicyError(
            "expected tip must be a full lowercase commit object ID"
        )
    repository = _canonical(Path(repo).expanduser(), "repository")
    record_path = _canonical(Path(owner_file).expanduser(), "owner file")
    if record_path.name != settings["owner_file"] or record_path.parent.name != agent:
        raise WorktreePolicyError(
            "owner file must belong to the exact agent runtime directory"
        )
    if not record_path.is_file() or record_path.is_symlink():
        raise WorktreePolicyError("the original regular ownership record is required")
    raw_owner = record_path.read_bytes()
    if len(raw_owner) > 65536:
        raise WorktreePolicyError("ownership record is oversized")
    try:
        owner = json.loads(raw_owner)
    except (ValueError, UnicodeError) as exc:
        raise WorktreePolicyError("ownership record is invalid JSON") from exc
    if not isinstance(owner, dict):
        raise WorktreePolicyError("ownership record must be an object")
    rendered = re.sub(r"[^a-z0-9]+", "-", agent.lower()).strip("-")
    slug = rendered[:48] + "-" + hashlib.sha256(agent.encode()).hexdigest()[:8]
    target = (
        repository
        / policy["worktree_directory"]
        / (settings["directory_prefix"] + slug)
    )
    branch = settings["branch_prefix"] + slug
    if not any(branch.startswith(prefix) for prefix in policy["topic_branch_prefixes"]):
        raise WorktreePolicyError(
            "the retained branch must remain an allowed topic branch"
        )
    expected = {
        "agent": agent,
        "repo_root": str(repository),
        "worktree": str(target),
        "branch": branch,
    }
    if any(owner.get(key) != value for key, value in expected.items()):
        raise WorktreePolicyError(
            "ownership does not match the exact agent, repository, "
            "checkout path, and branch"
        )
    _canonical(target, "owned checkout")
    if os.path.lexists(target):
        raise WorktreePolicyError("owned checkout target already exists")
    info = inspect_repo(repository, policy, source_hash)
    if info["surface"] != "authority" or info["repo_root"] != str(repository):
        raise WorktreePolicyError(
            "restore must inspect the canonical primary checkout "
            "of the owned repository"
        )
    if info["branch"] == "HEAD":
        raise WorktreePolicyError(
            "the primary checkout must have a retained named branch"
        )
    registry = _run_git(repository, "worktree", "list", "--porcelain", "-z")
    registered: list[Path] = []
    for block in registry.split("\0\0"):
        fields = block.split("\0")
        if f"worktree {target}" in fields or f"branch refs/heads/{branch}" in fields:
            raise WorktreePolicyError(
                "owned path or retained branch is already registered in a worktree"
            )
        registered.extend(
            Path(field[9:]) for field in fields if field.startswith("worktree ")
        )
    storage = repository / policy["worktree_directory"]
    exclusions = {target.relative_to(repository).as_posix()}
    exclusions.update(
        path.relative_to(repository).as_posix()
        for path in registered
        if storage in path.parents
    )
    status_argv = [
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        ".",
        *(f":(exclude){path}" for path in sorted(exclusions)),
    ]
    status = _run_git(repository, *status_argv)
    if status:
        raise WorktreePolicyError(
            "primary checkout is dirty; owned restore preserves its contents"
        )
    if _run_git(repository, "diff", "HEAD", "--", policy["worktree_directory"]):
        raise WorktreePolicyError("primary tracked worktree-storage contents are dirty")
    tip = _run_git(
        repository, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}"
    )
    if tip != expected_tip:
        raise WorktreePolicyError(
            "retained branch tip differs from the expected full commit ID"
        )
    return {
        **info,
        "allowed": True,
        "decision": "allow",
        "operation": "restore-owned-worktree",
        "agent": agent,
        "owner_file": str(record_path),
        "owner_sha256": hashlib.sha256(raw_owner).hexdigest(),
        "worktree": str(target),
        "restore_branch": branch,
        "expected_tip": tip,
        "primary_head": _run_git(repository, "rev-parse", "HEAD"),
        "primary_status_argv": status_argv,
        "primary_status_sha256": hashlib.sha256(status.encode()).hexdigest(),
        "worktree_registry_sha256": hashlib.sha256(registry.encode()).hexdigest(),
        "command": [
            "git",
            "-C",
            str(repository),
            "worktree",
            "add",
            str(target),
            branch,
        ],
        "reason": (
            "recreate only the absent recorded checkout "
            "from its retained existing branch"
        ),
    }


def render_projection(name: str, policy: dict[str, Any], source_hash: str) -> str:
    prefixes = ", ".join(f"`{p}`" for p in policy["topic_branch_prefixes"])
    examples = "\n".join(
        f"- `{item['command']}` → {item['outcome']}" for item in policy["examples"]
    )
    return f"""<!-- GENERATED. source-sha256: {source_hash} -->
# Worktree policy ({name})

This file is a discovery projection, not policy authority. The authoritative
manifest is the `worktree-policy.json` bundled with SAC
(`_baseline_assets/worktree_policy/`); runtime decisions come only from SAC's
in-process policy engine.

- The authority checkout stays on `{policy["authority_branch"]}` and is read-only.
- All edits, commits, pushes, and branch changes happen in linked worktrees.
- Linked worktrees use topic prefixes: {prefixes}.
- Worktrees live below `{policy["worktree_directory"]}/`.
- There is no bypass, exemption, or permissive fallback.

Examples generated from the tested manifest fixtures:

{examples}
"""


def generate_projections(
    output_dir: Path | str | None = None,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Write the harness projection files; return the freshness record."""
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    target = Path(output_dir).expanduser() if output_dir else projection_dir()
    target.mkdir(parents=True, exist_ok=True)
    expected = {
        name: render_projection(name, policy, source_hash) for name in HARNESS_NAMES
    }
    for name, content in expected.items():
        (target / f"{name}.worktree-policy.md").write_text(content, encoding="utf-8")
    return {
        "allowed": True,
        "decision": "generated",
        "policy_sha256": source_hash,
        "projection_sha256": projection_hash(target),
    }


def check_projections(
    output_dir: Path | str | None = None,
    policy: dict[str, Any] | None = None,
    source_hash: str | None = None,
) -> dict[str, Any]:
    """Refuse when a generated projection is missing or stale."""
    if policy is None or source_hash is None:
        policy, source_hash = load_policy()
    target = Path(output_dir).expanduser() if output_dir else projection_dir()
    expected = {
        name: render_projection(name, policy, source_hash) for name in HARNESS_NAMES
    }
    stale = [
        name
        for name, content in expected.items()
        if not (target / f"{name}.worktree-policy.md").is_file()
        or (target / f"{name}.worktree-policy.md").read_text(encoding="utf-8")
        != content
    ]
    if stale:
        raise WorktreePolicyError(
            "generated policy projections are stale: " + ", ".join(stale)
        )
    return {
        "allowed": True,
        "decision": "current",
        "policy_sha256": source_hash,
        "projection_sha256": projection_hash(target),
    }


def _flag(argv: list[str], name: str) -> str:
    try:
        return argv[argv.index(name) + 1]
    except (ValueError, IndexError) as exc:
        raise WorktreePolicyError(f"invalid policy invocation: missing {name}") from exc


def invoke(argv: list[str]) -> dict[str, Any]:
    """Run one policy verb in-process (same grammar as the retired CLI).

    Returns the success payload; refusals and malformed invocations raise
    :class:`WorktreePolicyError`, mirroring the CLI's nonzero exits.
    """
    if not argv:
        raise WorktreePolicyError("invalid policy invocation: no operation")
    verb, rest = argv[0], argv[1:]
    if verb == "check-projections":
        if rest:
            raise WorktreePolicyError(
                "invalid policy invocation: check-projections takes no flags"
            )
        return check_projections()
    if verb == "inspect":
        return {
            "allowed": True,
            "decision": "allow",
            **inspect_repo(_flag(argv, "--repo")),
        }
    if verb == "assert-context":
        return assert_context(_flag(argv, "--repo"), _flag(argv, "--intent"))
    if verb == "check-shell":
        return check_shell(_flag(argv, "--command"), _flag(argv, "--cwd"))
    if verb == "check-owned-restore":
        return approve_owned_restore(
            repo=_flag(argv, "--repo"),
            agent=_flag(argv, "--agent"),
            owner_file=_flag(argv, "--owner-file"),
            expected_tip=_flag(argv, "--expected-tip"),
        )
    raise WorktreePolicyError(f"invalid policy invocation: {verb!r}")
