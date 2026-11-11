"""CLI protocol for the packaged, harness-neutral worktree policy checker.

stdout defaults to one JSON decision; inspect can select a text view.
Errors remain JSON and human diagnostics use stderr.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .._logging import render_content
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


def build_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    parser = JsonArgumentParser(
        prog=prog,
        description=(
            "Inspect Git context and enforce the configured worktree policy. "
            "Decisions are JSON; a denial exits 2 and a policy/argument error exits 3."
        ),
        epilog=(
            "Examples:\n"
            "  $ %(prog)s inspect --repo .\n"
            "  $ %(prog)s assert-context --repo . --intent edit\n"
            "  $ %(prog)s check-projections"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--policy", type=Path, default=None)
    sub = parser.add_subparsers(dest="operation", required=True)
    inspect = sub.add_parser(
        "inspect",
        description="Inspect Git context and policy identity without changing the repository.",
    )
    inspect.add_argument("--repo", type=Path, required=True)
    output = inspect.add_mutually_exclusive_group()
    output.add_argument(
        "--json", action="store_true", help="Select the default JSON decision output."
    )
    output.add_argument(
        "--text",
        action="store_true",
        help="Show inspection fields as text; errors stay JSON.",
    )
    context = sub.add_parser(
        "assert-context",
        description="Check whether Git context permits the selected intent.",
    )
    context.add_argument("--repo", type=Path, required=True)
    context.add_argument(
        "--intent",
        choices=("read", "edit", "commit", "push", "branch-change"),
        required=True,
    )
    edit = sub.add_parser(
        "check-edit",
        description="Check edit permission for a path under the selected policy.",
    )
    edit.add_argument("--path", type=Path, required=True)
    shell = sub.add_parser(
        "check-shell", description="Check a shell command against the worktree policy."
    )
    shell.add_argument("--cwd", type=Path, required=True)
    shell.add_argument("--command", required=True)
    generate = sub.add_parser(
        "generate-projections",
        description="Generate policy projections after explicit approval.",
    )
    generate.add_argument("--output-dir", type=Path, default=None)
    generate.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview supplied arguments without reading policy or generating files.",
    )
    generate.add_argument(
        "--yes", "-y", action="store_true", help="Approve writing policy projections."
    )
    check = sub.add_parser(
        "check-projections",
        description="Verify generated projections match the selected policy.",
    )
    check.add_argument("--output-dir", type=Path, default=None)
    return parser


def _emit_inspection(payload: dict, *, as_json: bool) -> int:
    if as_json:
        return emit(payload, 0)
    render_content(
        "\n".join(
            f"{key}: {json.dumps(value, sort_keys=True, ensure_ascii=False)}"
            for key, value in sorted(payload.items())
        )
    )
    return 0


def main(
    argv: list[str] | None = None,
    *,
    prog: str | None = None,
    json_output: bool = False,
) -> int:
    args = build_parser(prog=prog).parse_args(argv)
    try:
        if args.operation == "generate-projections" and args.dry_run:
            from ._cli_preview import projection_argument_preview

            return emit(projection_argument_preview(args.policy, args.output_dir), 0)
        if args.operation == "generate-projections" and not args.yes:
            message = "Projection generation requires --yes; use --dry-run to preview."
            return emit(
                {"allowed": False, "decision": "error", "error": message}, 3, message
            )
        if args.policy is None:
            args.policy = default_policy_path()
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
            return _emit_inspection(
                {
                    "allowed": True,
                    "decision": "allow",
                    **inspect_repo(args.repo, policy, source_hash, projection_dir),
                },
                as_json=json_output or args.json or not args.text,
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
