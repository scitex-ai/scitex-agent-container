#!/usr/bin/env python3
"""Move prompt-like startup_commands -> startup_prompts.

Background: until 2026-05-17 the apptainer runtime had a legacy
fallback that promoted ``spec.startup_commands[0].command`` into
``--mission`` when ``spec.startup_prompts`` was empty. Specs in the
wild relied on that fallback by storing claude-prompt prose (e.g.
"Audit this codebase and report findings") inside startup_commands.
Commit b367f50 removed the fallback and switched startup_commands to
container-internal shell execution. Without migration, every spec
whose startup_commands held prose would bash-error on next start.

This script reads each spec.yaml under the target roots, classifies
each startup_commands entry, and moves PROMPT-LIKE entries into
startup_prompts. SHELL-LIKE entries are left in startup_commands.
MIXED entries (both kinds present) are skipped and reported for
manual review.

Classification heuristic:
  SHELL-LIKE if the command text contains any of: ; && | $ > < =
              OR starts with a known shell command prefix (pip,
              apt, bash, sh, source, export, cd, ls, echo, python,
              git, curl, wget, make, cmake, mkdir, rm, mv, cp,
              chmod, chown, tar, gzip).
  PROMPT-LIKE otherwise.

Idempotent: skips specs where startup_commands is empty or already
fully migrated.

Usage:
  python migrate_startup_commands_to_prompts.py [--apply] [--root <dir>]

Without --apply: dry-run report only.
With --apply: rewrites spec files (preserving comments via
ruamel.yaml). Backups go to <root>.bak-startup-migrate-<timestamp>/.

Default roots scanned (override with --root):
  ~/.scitex/agent-container/agents/

For Spartan: run this script remotely via:
  scp ... spartan:/tmp/
  ssh spartan 'python3 /tmp/migrate_startup_commands_to_prompts.py --apply'
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

try:
    from ruamel.yaml import YAML
except ImportError:
    print("FATAL: ruamel.yaml not installed", file=sys.stderr)
    sys.exit(2)

SHELL_PREFIXES = (
    "pip ",
    "pip3 ",
    "apt ",
    "apt-get ",
    "bash ",
    "sh ",
    "source ",
    "export ",
    "cd ",
    "ls ",
    "echo ",
    "python ",
    "python3 ",
    "git ",
    "curl ",
    "wget ",
    "make ",
    "cmake ",
    "mkdir ",
    "rm ",
    "mv ",
    "cp ",
    "chmod ",
    "chown ",
    "tar ",
    "gzip ",
    "ssh ",
    "scp ",
    "rsync ",
    "uv ",
    "venv ",
    ".",
)
SHELL_OPS = re.compile(r"[;&|$<>=]|\\$")


def _classify(cmd: str) -> str:
    """Return 'shell' or 'prompt'.

    Multi-line or long content is treated as prompt — these are
    legacy claude-prompt blobs (the typical quality-spec pattern) that
    happen to mention shell commands inline as instructions to claude,
    not as commands to bash. Genuine shell entries are short single
    lines (``pip install foo``, ``echo hi``).
    """
    s = cmd.strip()
    if not s:
        return "prompt"
    if "\n" in s or len(s) > 150:
        return "prompt"
    if SHELL_OPS.search(s):
        return "shell"
    if any(s.startswith(p) for p in SHELL_PREFIXES):
        return "shell"
    return "prompt"


def _split(items: list) -> tuple[list, list]:
    """Return (shell_entries, prompt_strs)."""
    shell, prompts = [], []
    for it in items:
        if isinstance(it, dict):
            cmd = (it.get("command") or "").strip()
        elif isinstance(it, str):
            cmd = it.strip()
        else:
            continue
        if not cmd:
            continue
        if _classify(cmd) == "shell":
            shell.append(it)
        else:
            prompts.append(cmd)
    return shell, prompts


def _process(spec_path: Path, apply: bool, yaml: YAML) -> dict:
    """Return action dict: {status, n_prompts_moved, n_shell_kept}."""
    text = spec_path.read_text()
    doc = yaml.load(text)
    if not isinstance(doc, dict):
        return {"status": "skip-not-dict", "n_prompts_moved": 0, "n_shell_kept": 0}
    spec = doc.get("spec") or {}
    cmds = spec.get("startup_commands") or []
    if not cmds:
        return {"status": "skip-empty", "n_prompts_moved": 0, "n_shell_kept": 0}

    shell, prompt_strs = _split(list(cmds))
    if not prompt_strs:
        return {
            "status": "skip-all-shell",
            "n_prompts_moved": 0,
            "n_shell_kept": len(shell),
        }

    if shell and prompt_strs:
        return {
            "status": "mixed-manual-required",
            "n_prompts_moved": 0,
            "n_shell_kept": len(shell),
            "n_prompts_pending": len(prompt_strs),
        }

    if not apply:
        return {
            "status": "would-migrate",
            "n_prompts_moved": len(prompt_strs),
            "n_shell_kept": 0,
        }

    existing_prompts = spec.get("startup_prompts") or []
    if not isinstance(existing_prompts, list):
        existing_prompts = []
    new_prompts = list(existing_prompts) + prompt_strs
    spec["startup_prompts"] = new_prompts
    if shell:
        spec["startup_commands"] = shell
    else:
        spec.pop("startup_commands", None)
    doc["spec"] = spec

    with spec_path.open("w") as f:
        yaml.dump(doc, f)
    return {
        "status": "migrated",
        "n_prompts_moved": len(prompt_strs),
        "n_shell_kept": len(shell),
    }


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--root",
        action="append",
        default=None,
        help="Root dir to scan (repeatable). Default: ~/.scitex/agent-container/agents/",
    )
    p.add_argument(
        "--apply", action="store_true", help="Rewrite files (default: dry-run report)"
    )
    args = p.parse_args(argv)

    roots = args.root or [str(Path.home() / ".scitex/agent-container/agents")]
    roots = [Path(r).expanduser() for r in roots]

    yaml = YAML()
    yaml.preserve_quotes = True
    yaml.width = 4096
    yaml.indent(mapping=2, sequence=4, offset=2)

    if args.apply:
        ts = time.strftime("%Y%m%d-%H%M%S")
        for r in roots:
            bak = r.parent / f"{r.name}.bak-startup-migrate-{ts}"
            if r.exists() and not bak.exists():
                print(f"backup {r} -> {bak}")
                shutil.copytree(r, bak)

    results: dict[str, int] = {}
    mixed: list[Path] = []
    migrated: list[Path] = []
    for root in roots:
        if not root.exists():
            print(f"WARN root missing: {root}", file=sys.stderr)
            continue
        for spec in sorted(root.glob("*/spec.yaml")):
            r = _process(spec, args.apply, yaml)
            st = r["status"]
            results[st] = results.get(st, 0) + 1
            if st == "mixed-manual-required":
                mixed.append(spec)
            elif st == "migrated":
                migrated.append(spec)

    print()
    print("=== summary ===")
    for k, v in sorted(results.items()):
        print(f"  {k}: {v}")
    if mixed:
        print()
        print("=== MIXED — manual review required ===")
        for m in mixed:
            print(f"  {m}")
    if args.apply and migrated:
        print()
        print(f"=== migrated {len(migrated)} specs ===")
        for m in migrated[:20]:
            print(f"  {m}")
        if len(migrated) > 20:
            print(f"  ... and {len(migrated) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
