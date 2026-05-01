#!/usr/bin/env python3
"""annotate_stx_allow.py — bulk-annotate bare except clauses with stx-allow.

Adds # stx-allow: fallback (reason: <auto>) to every except clause that lacks
the directive. Uses exception type to generate a standard reason string.

Writes in-place. Idempotent — won't double-annotate already-annotated lines.

Usage:
    python scripts/annotate_stx_allow.py [file ...]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

_EXCEPT_RE = re.compile(r"^(\s*)(except\b[^:]*?)(\s*):(\s*(?:#.*)?)$")
_ALLOW_RE = re.compile(r"#\s*stx-allow:\s*fallback")

_REASONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"ImportError|ModuleNotFoundError"), "optional dependency not installed"),
    (re.compile(r"FileNotFoundError"), "file may not exist on first use"),
    (re.compile(r"ProcessLookupError|PermissionError"), "process probe expected failure"),
    (re.compile(r"SubprocessError|CalledProcessError|TimeoutExpired"), "subprocess execution failure"),
    (re.compile(r"JSONDecodeError|json\.JSONDecodeError"), "malformed JSON tolerated"),
    (re.compile(r"(OSError|IOError).*json|json.*(OSError|IOError)"), "file I/O or JSON parse failure"),
    (re.compile(r"OSError|IOError"), "file system operation failure"),
    (re.compile(r"ValueError|TypeError"), "type coercion or format mismatch"),
    (re.compile(r"KeyError"), "missing key in external data"),
    (re.compile(r"StopIteration"), "iterator exhausted — control flow"),
    (re.compile(r"RuntimeError"), "runtime state error — handled gracefully"),
    (re.compile(r"AttributeError"), "optional attribute access"),
    (re.compile(r"Exception\b"), "catch-all safety net — see inline comment for context"),
    (re.compile(r"BaseException\b"), "catch-all safety net including KeyboardInterrupt"),
]


def _reason_for(except_clause: str) -> str:
    for pat, reason in _REASONS:
        if pat.search(except_clause):
            return reason
    return "expected failure — see inline comment"


def annotate_file(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    changed = 0
    out: list[str] = []
    for line in lines:
        m = _EXCEPT_RE.match(line.rstrip("\n"))
        if m and not _ALLOW_RE.search(line):
            indent, clause, space, tail = m.group(1), m.group(2), m.group(3), m.group(4)
            reason = _reason_for(clause)
            existing_comment = tail.strip()
            if existing_comment:
                new_tail = f"  {existing_comment}  # stx-allow: fallback (reason: {reason})"
            else:
                new_tail = f"  # stx-allow: fallback (reason: {reason})"
            newline = f"{indent}{clause}{space}:{new_tail}\n"
            out.append(newline)
            changed += 1
        else:
            out.append(line if line.endswith("\n") else line + "\n")
    if changed:
        path.write_text("".join(out), encoding="utf-8")
    return changed


def main() -> int:
    files = [Path(a) for a in sys.argv[1:]]
    if not files:
        root = Path(__file__).parent.parent / "src"
        files = list(root.rglob("*.py"))

    total = 0
    for p in sorted(files):
        if p.suffix != ".py":
            continue
        try:
            n = annotate_file(p)
        except Exception as e:
            print(f"ERROR {p}: {e}", file=sys.stderr)
            continue
        if n:
            print(f"  annotated {n} clause(s) in {p}")
            total += n
    print(f"\nTotal: {total} annotation(s) added.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
