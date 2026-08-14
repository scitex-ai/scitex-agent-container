#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/_symbols.py

"""AST symbol diff — the mechanical half of the unrequested-deletion guard.

Promoted from ``scripts/local_model_trials/detectors.py``, where it judged
36 local-model coding trials (6 rungs x 2 models x 3 reps). The semantics
here are that module's, unchanged; only the home moved, so the guard is
callable by any delegation path instead of only by the trials harness.

Nothing here trusts a summary. Every verdict is computed from file bytes:
one trial returned an EMPTY summary over a green tree, and the historical
incident this guard exists for — a local model deleting two classes an
importing sibling needed, while "adding" a function — was never mentioned
in the model's own report of what it did.

A symbol is one of::

    func:name                module-level def / async def
    class:Name               module-level class
    class:Name.method        method on a module-level class

Nested definitions are deliberately NOT walked. The failure being guarded
is a module-level name vanishing out from under an importing sibling; a
helper nested inside a function is not importable, so its disappearance is
a different (and non-breaking) event.

Known limitation, stated rather than hidden
===========================================
A symbol is a ``def`` or a ``class``, never an ``import``. So a RE-EXPORT
refactor — deleting ``def foo`` and replacing it with ``from elsewhere
import foo`` — reads as a deletion even though ``foo`` is still importable
from that module. This was measured on the very commit that promoted this
code: running the guard over it reported four functions "deleted" from
``scripts/local_model_trials/detectors.py``, which had become re-exports.

That is a real false positive for one refactor shape, and it is left in
place ON PURPOSE. Teaching :func:`symbol_set` about imports would change
what the 36 measured local-model trials measured, and the safer direction
for a guard is to over-report a shape a human clears with ``--allow`` than
to learn a rule that lets a genuine deletion through. Clear it with
``--allow '<path>::func:<name>'``.
"""

from __future__ import annotations

import ast

__all__ = [
    "added_symbols",
    "detect_deletions",
    "diff_trees",
    "symbol_locations",
    "symbol_set",
]


def _walk(tree: ast.Module):
    """Yield ``(symbol, first_line, last_line)`` for module-level symbols."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield f"func:{node.name}", node.lineno, node.end_lineno or node.lineno
        elif isinstance(node, ast.ClassDef):
            yield f"class:{node.name}", node.lineno, node.end_lineno or node.lineno
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield (
                        f"class:{node.name}.{sub.name}",
                        sub.lineno,
                        sub.end_lineno or sub.lineno,
                    )


def symbol_locations(source: str):
    """``{symbol: (first_line, last_line)}``, or None on SyntaxError.

    The line span is what turns "a symbol went missing" into an error a
    human can act on — it names WHERE in the baseline the code used to be.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return {sym: (start, end) for sym, start, end in _walk(tree)}


def symbol_set(source: str):
    """Set of def/class symbols in a python source, or None on SyntaxError.

    Symbols: 'func:name', 'class:Name', 'class:Name.method'.
    """
    located = symbol_locations(source)
    return None if located is None else set(located)


def detect_deletions(before_files: dict, after_files: dict,
                     allowed=frozenset()) -> dict:
    """Symbol-level deletion diff between two {path: content} trees.

    Returns:
      deleted        ['path::symbol', ...] NOT covered by `allowed` -> FAIL
      allowed_hits   deletions the task explicitly required
      deleted_files  files that vanished entirely
      broken_files   files that no longer parse (SyntaxError)

    ``broken_files`` is the honest UNKNOWN of this function, not a minor
    detail: a file that no longer parses is SKIPPED, so every symbol it
    used to export is invisible to the diff. Callers must not read an
    empty ``deleted`` beside a non-empty ``broken_files`` as "clean" —
    see :mod:`._report`, which encodes exactly that rule.
    """
    deleted, allowed_hits, deleted_files, broken = [], [], [], []
    for path, before in sorted(before_files.items()):
        if not path.endswith(".py"):
            if path not in after_files:
                deleted_files.append(path)
            continue
        before_syms = symbol_set(before) or set()
        if path not in after_files:
            deleted_files.append(path)
            after_syms = set()
        else:
            after_syms = symbol_set(after_files[path])
            if after_syms is None:
                broken.append(path)
                continue
        for sym in sorted(before_syms - after_syms):
            key = f"{path}::{sym}"
            (allowed_hits if key in allowed else deleted).append(key)
    return {
        "deleted": deleted,
        "allowed_hits": allowed_hits,
        "deleted_files": deleted_files,
        "broken_files": broken,
    }


def diff_trees(before_files: dict, after_files: dict) -> dict:
    """File-level diff: changed / added / removed paths."""
    changed = sorted(
        p for p in before_files
        if p in after_files and after_files[p] != before_files[p]
    )
    added = sorted(p for p in after_files if p not in before_files)
    removed = sorted(p for p in before_files if p not in after_files)
    return {"changed": changed, "added": added, "removed": removed}


def added_symbols(before_files: dict, after_files: dict) -> list:
    """['path::symbol', ...] present in AFTER and absent from BEFORE."""
    out = []
    for path, after in sorted(after_files.items()):
        if not path.endswith(".py"):
            continue
        after_syms = symbol_set(after)
        if after_syms is None:
            continue
        before_syms = symbol_set(before_files.get(path, "")) or set()
        out.extend(f"{path}::{s}" for s in sorted(after_syms - before_syms))
    return out


# EOF
