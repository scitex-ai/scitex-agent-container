#!/usr/bin/env python3
"""check_stx_allow.py — lint for SILENT except handlers missing an stx-allow.

Enforces the fleet-wide 'no-silent-fallback' convention: an except clause that
SWALLOWS the exception must say why, with a
``# stx-allow: fallback (reason: ...)`` comment.

WHY THIS WAS REWRITTEN (2026-07-15)
-----------------------------------
The previous implementation was a regex — ``^\\s*except\\b.*:`` — applied to a
single line, and it never read the handler body. Measured against this tree it
was wrong in BOTH directions at once:

* **552 handlers flagged in src/, of which 187 (34%) were FALSE POSITIVES.**
  A handler that re-raises is the OPPOSITE of a silent fallback, and it was
  reported as one::

      except SpawnRequestError as exc:
          raise click.ClickException(str(exc)) from exc     # <-- "violation"

* **619 multi-line ``except (`` clauses were INVISIBLE to it.** There is no
  colon on the ``except (`` line, so the pattern never matched — and the
  ``# stx-allow:`` comment those handlers legitimately carry after the closing
  paren was unreadable to it as well.

A checker that both cries wolf and sleeps through the actual wolf cannot be a
gate: enabling it would have blocked essentially every commit touching src/
while still enforcing nothing on 619 handlers. It is the same shape as the
container pin-check that was a substring match on "0.3" and therefore rejected
every STRONGER pin — a guard that reads like diligence and behaves like a
freeze.

(The old version also swallowed exceptions itself, with a bare
``except Exception: continue`` carrying no stx-allow comment. The
no-silent-fallback linter contained the exact violation it existed to prevent.)

So: parse with ``ast``. A handler is a violation only when it genuinely
SWALLOWS — its body neither raises, nor exits, nor logs. The ``# stx-allow:``
comment is searched across the handler's whole header span, so a multi-line
``except (...)`` that carries the comment on its closing line is honoured.

NOTE ON STATUS: this is a usable TOOL, not yet a passable GATE. ~365 genuine
un-annotated silent fallbacks remain in src/, so it is deliberately NOT wired
into .pre-commit-config.yaml — see the note there. Each of those needs a REAL
reason string; auto-inserting 365 generic ones would be cargo-culting a linter
rather than honouring a convention.

Usage:
    python scripts/check_stx_allow.py [file ...]      # default: all of src/

Exit code 0 = clean; 1 = violations found.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

_ALLOW_RE = re.compile(r"#\s*stx-allow:")

# Calls that make a handler LOUD. A handler that reports is not silent, and the
# convention is about SILENCE, not about the mere existence of an except clause.
_LOUD_CALLS = frozenset(
    {
        # explicit process/command termination
        "exit",
        "abort",
        "fail",
        # logging / user-visible reporting
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "echo",
    }
)


def _is_silent(handler: ast.ExceptHandler) -> bool:
    """True when the handler swallows: it neither raises, exits, nor reports."""
    module = ast.Module(body=handler.body, type_ignores=[])
    for node in ast.walk(module):
        if isinstance(node, ast.Raise):
            return False
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in _LOUD_CALLS:
                return False
    return True


def _header_span(handler: ast.ExceptHandler, n_lines: int) -> range:
    """The line range of the ``except ...:`` clause itself, body excluded.

    A multi-line ``except (\\n  A,\\n  B,\\n):  # stx-allow: ...`` carries its
    comment on the CLOSING line, which is why the whole span is searched rather
    than only ``handler.lineno``. Missing that was half the old checker's bug.
    """
    first_body_line = handler.body[0].lineno if handler.body else handler.lineno + 1
    return range(handler.lineno, min(first_body_line, n_lines + 1))


def check_file(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, source-line) for each SILENT, un-annotated handler."""
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        # NOT swallowed: an unparseable file is reported as a violation of its
        # own, because a checker that silently skips what it cannot read is how
        # a gate ends up enforcing nothing. (This is also the bug the previous
        # version had here: `except Exception: continue`.)
        print(f"{path}:{exc.lineno or 0}: cannot parse ({exc.msg})")
        return [(exc.lineno or 0, "<unparseable>")]

    lines = source.splitlines()
    violations: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        if not _is_silent(node):
            continue
        span = _header_span(node, len(lines))
        if any(_ALLOW_RE.search(lines[i - 1]) for i in span):
            continue
        violations.append((node.lineno, lines[node.lineno - 1].rstrip()))
    return violations


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    files = [Path(a) for a in args]
    if not files:
        files = sorted((Path(__file__).parent.parent / "src").rglob("*.py"))

    total = 0
    for path in sorted(files):
        if path.suffix != ".py" or not path.is_file():
            continue
        for lineno, line in check_file(path):
            print(f"{path}:{lineno}: silent fallback missing stx-allow — {line!r}")
            total += 1

    if total:
        print(
            f"\n{total} silent fallback(s) with no stated reason. Add\n"
            "  # stx-allow: fallback (reason: <why swallowing is correct here>)\n"
            "to each, or restructure so the failure is not swallowed.\n"
            "A handler that raises, exits, or logs is NOT flagged — only silence is.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
