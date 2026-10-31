"""CLI protocol for the packaged, harness-neutral worktree policy checker.

stdout contains one JSON decision; human diagnostics use stderr.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ._context import decide_context, inspect_repo, resolve_edit_repo
from ._policy import (
    HARNESS_NAMES,
    PACKAGED_POLICY,
    PolicyError,
    default_policy_path,
    emit,
    host_policy_path,
    load_policy,
    projection_hash,
    render_projection,
)
from ._shell import shell_decision


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        emit({"allowed": False, "decision": "error", "error": message}, 3, message)
        raise SystemExit(3)


def build_parser() -> argparse.ArgumentParser:
    parser = JsonArgumentParser()
    parser.add_argument("--policy", type=Path, default=default_policy_path())
    sub = parser.add_subparsers(dest="operation", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--repo", type=Path, required=True)
    context = sub.add_parser("assert-context")
    context.add_argument("--repo", type=Path, required=True)
    context.add_argument(
        "--intent",
        choices=("read", "edit", "commit", "push", "branch-change"),
        required=True,
    )
    edit = sub.add_parser("check-edit")
    edit.add_argument("--path", type=Path, required=True)
    shell = sub.add_parser("check-shell")
    shell.add_argument("--cwd", type=Path, required=True)
    shell.add_argument("--command", required=True)
    generate = sub.add_parser("generate-projections")
    generate.add_argument("--output-dir", type=Path, default=None)
    check = sub.add_parser("check-projections")
    check.add_argument("--output-dir", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # An explicit generation request initializes host configuration rather
        # than writing into Python's installed package directory.
        if (
            args.operation == "generate-projections"
            and args.policy == PACKAGED_POLICY
            and args.output_dir is None
        ):
            configured = host_policy_path()
            configured.parent.mkdir(parents=True, exist_ok=True)
            try:
                with configured.open("xb") as stream:
                    stream.write(PACKAGED_POLICY.read_bytes())
            except FileExistsError:
                pass  # A concurrently supplied host policy remains authoritative.
            args.policy = configured
        projection_dir = args.policy.expanduser().resolve().parent / "generated"
        if hasattr(args, "output_dir") and args.output_dir is None:
            args.output_dir = projection_dir
        policy, source_hash = load_policy(args.policy.resolve())
        if args.operation == "inspect":
            return emit(
                {
                    "allowed": True,
                    "decision": "allow",
                    **inspect_repo(args.repo, policy, source_hash, projection_dir),
                },
                0,
            )
        if args.operation == "assert-context":
            info = inspect_repo(args.repo, policy, source_hash, projection_dir)
            allowed, reason = decide_context(info, args.intent, policy)
            return emit(
                {
                    **info,
                    "allowed": allowed,
                    "decision": "allow" if allowed else "deny",
                    "reason": reason,
                },
                0 if allowed else 2,
                None if allowed else reason,
            )
        if args.operation == "check-edit":
            repo = resolve_edit_repo(args.path.resolve())
            if repo is None:
                return emit(
                    {
                        "allowed": True,
                        "decision": "allow",
                        "reason": "path is outside a Git repository",
                        "policy_sha256": source_hash,
                        "projection_sha256": projection_hash(projection_dir),
                    },
                    0,
                )
            info = inspect_repo(repo, policy, source_hash, projection_dir)
            allowed, reason = decide_context(info, "edit", policy)
            return emit(
                {
                    **info,
                    "allowed": allowed,
                    "decision": "allow" if allowed else "deny",
                    "reason": reason,
                },
                0 if allowed else 2,
                None if allowed else reason,
            )
        if args.operation == "check-shell":
            result = shell_decision(
                args.command, args.cwd.resolve(), policy, source_hash, projection_dir
            )
            result.update(
                {
                    "policy_sha256": source_hash,
                    "projection_sha256": projection_hash(projection_dir),
                }
            )
            return emit(
                result,
                0 if result["allowed"] else 2,
                None if result["allowed"] else result["reason"],
            )
        expected = {
            name: render_projection(name, policy, source_hash) for name in HARNESS_NAMES
        }
        if args.operation == "generate-projections":
            args.output_dir.mkdir(parents=True, exist_ok=True)
            for name, content in expected.items():
                (args.output_dir / f"{name}.worktree-policy.md").write_text(content)
            return emit(
                {
                    "allowed": True,
                    "decision": "generated",
                    "policy_sha256": source_hash,
                    "projection_sha256": projection_hash(args.output_dir),
                },
                0,
            )
        stale = [
            name
            for name, content in expected.items()
            if not (args.output_dir / f"{name}.worktree-policy.md").is_file()
            or (args.output_dir / f"{name}.worktree-policy.md").read_text() != content
        ]
        if stale:
            return emit(
                {
                    "allowed": False,
                    "decision": "stale",
                    "stale": stale,
                    "policy_sha256": source_hash,
                },
                3,
                "generated policy projections are stale",
            )
        return emit(
            {
                "allowed": True,
                "decision": "current",
                "policy_sha256": source_hash,
                "projection_sha256": projection_hash(args.output_dir),
            },
            0,
        )
    except PolicyError as exc:
        return emit(
            {"allowed": False, "decision": "error", "error": str(exc)},
            exc.code,
            str(exc),
        )
    except OSError as exc:
        return emit(
            {"allowed": False, "decision": "error", "error": str(exc)}, 3, str(exc)
        )
