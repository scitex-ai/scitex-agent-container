"""Evaluate static shell Git mutations against the selected worktree policy."""

from __future__ import annotations

from pathlib import Path
import re
import shlex
from typing import Any

from ._context import decide_context, inspect_repo, is_topic
from ._policy import PolicyError


def split_shell(command: str) -> list[list[str]]:
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


def git_invocations(command: str, cwd: Path) -> list[tuple[str, Path, list[str]]]:
    invocations: list[tuple[str, Path, list[str]]] = []
    effective_cwd = cwd
    try:
        segments = split_shell(command)
    except ValueError as exc:
        if "git" in command:
            raise PolicyError(f"cannot parse git command safely: {exc}") from exc
        return []
    for segment in segments:
        if segment[0] == "cd":
            if len(segment) != 2 or any(c in segment[1] for c in "$`"):
                raise PolicyError(
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
            raise PolicyError(
                "wrapped git commands are not accepted; invoke git directly for deterministic policy evaluation"
            )
        args = segment[1:]
        target = effective_cwd
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag == "-C":
                if not args or any(c in args[0] for c in "$`"):
                    raise PolicyError("git -C requires a static path")
                raw = Path(args.pop(0)).expanduser()
                target = (
                    (target / raw).resolve() if not raw.is_absolute() else raw.resolve()
                )
            elif flag.startswith("-C"):
                raw_text = flag[2:]
                if not raw_text or any(c in raw_text for c in "$`"):
                    raise PolicyError("git -C requires a static path")
                raw = Path(raw_text).expanduser()
                target = (
                    (target / raw).resolve() if not raw.is_absolute() else raw.resolve()
                )
            elif flag == "-c":
                if not args:
                    raise PolicyError("git -c requires a value")
                args.pop(0)
        if args:
            invocations.append((args[0], target, args[1:]))
    return invocations


def shell_decision(
    command: str,
    cwd: Path,
    policy: dict[str, Any],
    source_hash: str,
    projection_dir: Path,
) -> dict[str, Any]:
    mutations = {"commit", "push", "checkout", "switch", "branch", "worktree"}
    checks: list[dict[str, Any]] = []
    invocations = git_invocations(command, cwd)
    if not invocations and re.search(
        r"\bgit\s+(?:[^\s]+\s+)*(?:commit|push|checkout|switch|branch|worktree)\b",
        command,
    ):
        raise PolicyError(
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
        info = inspect_repo(target, policy, source_hash, projection_dir)
        if subcommand == "worktree":
            branch = None
            path = None
            index = 1
            while index < len(args):
                arg = args[index]
                if arg in {"-b", "-B"}:
                    if index + 1 >= len(args):
                        raise PolicyError(
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
                    "worktree add uses the authority checkout, required directory, and a topic branch"
                    if allowed
                    else "worktree add requires authority/develop, <authority>/.worktrees/, and an allowed topic branch"
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
    return {
        "allowed": denied is None,
        "decision": "allow" if denied is None else "deny",
        "reason": denied["reason"] if denied else "no forbidden git mutation was found",
        "checks": checks,
    }
