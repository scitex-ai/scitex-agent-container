#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/_render.py

"""Human rendering of a :class:`DeletionReport`.

Two rules, both learned the hard way:

1. An error that only states WHAT broke is half-written. Every violation
   names the file, the symbol, and the baseline line span; every report
   ends with what to do next.
2. ``could-not-determine`` must never read like a pass. It prints
   ``CANNOT TELL — this is NOT a pass`` in the same slot where ``clean``
   prints ``OK``, so a human skimming the last line cannot confuse them.
"""

from __future__ import annotations

from ._report import CLEAN, UNDETERMINED, DeletionReport

__all__ = ["render"]

_TITLE = "unrequested-deletion guard"
_LABEL_W = 22


def _kv(label: str, value) -> str:
    return f"  {label:<{_LABEL_W}}{value}"


def _deletion_lines(deletions) -> list:
    """One line per deletion, with a class's own methods folded into it.

    A deleted class already implies its methods; printing all of them turns
    one removal into twenty lines and buries the next finding. The JSON
    keeps every entry — this only tidies the human view.
    """
    classes = {
        (d.path, d.symbol)
        for d in deletions
        if d.symbol.startswith("class:") and "." not in d.symbol
    }
    folded: dict = {}
    for item in deletions:
        owner = (item.path, item.symbol.rsplit(".", 1)[0])
        if "." in item.symbol and owner in classes:
            folded[owner] = folded.get(owner, 0) + 1
    lines = []
    for item in deletions:
        owner = (item.path, item.symbol.rsplit(".", 1)[0])
        if "." in item.symbol and owner in classes:
            continue
        suffix = ""
        count = folded.get((item.path, item.symbol), 0)
        if count:
            suffix = f"  (+{count} method{'s' if count > 1 else ''})"
        lines.append(f"    {item.where:<28} {item.symbol}{suffix}")
    return lines


def render(report: DeletionReport) -> str:
    """Render ``report`` as the human table the CLI prints."""
    lines = [_TITLE, ""]
    lines.append(_kv("baseline", report.baseline))
    lines.append(_kv("target", report.target))
    lines.append(_kv("files compared", report.files_compared))
    lines.append(_kv("verdict", report.verdict.upper()))
    if report.allowed_deletions:
        lines.append(_kv("allowed (requested)", len(report.allowed_deletions)))
    lines.append("")

    if report.verdict == CLEAN:
        lines.append("  OK — nothing vanished that the task did not ask for.")
        return "\n".join(lines)

    if report.verdict == UNDETERMINED:
        lines.append("  CANNOT TELL — this is NOT a pass.")
        lines.append(f"    {report.undetermined_reason}")
    else:
        if report.deletions:
            shown = _deletion_lines(report.deletions)
            total = len(report.deletions)
            extra = "" if len(shown) == total else f" ({total} with methods)"
            lines.append(
                f"  {len(shown)} symbol(s) deleted without being "
                f"requested{extra}:"
            )
            lines.extend(shown)
        if report.deleted_files:
            lines.append(f"  {len(report.deleted_files)} file(s) removed:")
            for path in report.deleted_files:
                lines.append(f"    {path}")

    if report.broken_files:
        lines.append("")
        lines.append(
            f"  {len(report.broken_files)} file(s) no longer parse and were "
            "NOT compared:"
        )
        for path in report.broken_files:
            lines.append(f"    {path}")

    if report.next_steps:
        lines.append("")
        lines.append("  what to do next:")
        for step in report.next_steps:
            lines.append(f"    - {step}")
    return "\n".join(lines)


# EOF
