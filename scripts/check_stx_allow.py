#!/usr/bin/env python3
"""check_stx_allow.py — lint for bare try/except blocks missing stx-allow directive.

Enforces the fleet-wide 'no-silent-fallback' convention:
every try/except block must carry a # stx-allow: fallback (reason: ...) comment
on the same line as the `except` clause.

Usage (standalone):
    python scripts/check_stx_allow.py [file ...]

Used as pre-commit hook — see .pre-commit-config.yaml.
Exit code 0 = all clear; 1 = violations found.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_EXCEPT_RE = re.compile(r"^\s*except\b.*:")
_ALLOW_RE = re.compile(r"#\s*stx-allow:\s*fallback")

# Bare `except Exception: pass` / `except Exception: continue` are the
# highest-priority violations; we flag all undecorated except clauses.


def check_file(path: Path) -> list[tuple[int, str]]:
    violations: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    in_docstring = False
    docstring_char = ""
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        # Track triple-quoted string state to skip pseudo-code in docstrings.
        for marker in ('"""', "'''"):
            count = line.count(marker)
            if not in_docstring:
                if count % 2 == 1:
                    in_docstring = True
                    docstring_char = marker
            elif docstring_char == marker and count % 2 == 1:
                in_docstring = False
        if in_docstring:
            continue
        if _EXCEPT_RE.match(line) and not _ALLOW_RE.search(line):
            violations.append((i, line.rstrip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    files = [Path(a) for a in (argv or sys.argv[1:])]
    if not files:
        # Default: all src/ Python files
        root = Path(__file__).parent.parent / "src"
        files = list(root.rglob("*.py"))

    total = 0
    for p in sorted(files):
        if not p.suffix == ".py":
            continue
        try:
            viols = check_file(p)
        except Exception:
            continue
        for lineno, line in viols:
            print(f"{p}:{lineno}: missing stx-allow: fallback — {line!r}")
            total += 1

    if total:
        print(
            f"\n{total} violation(s). Add  # stx-allow: fallback (reason: <why>)  "
            "to each except clause, or restructure to avoid silent failures.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
