"""Import an existing Claude Code config into a neutral layer (migration).

Reads ``settings.json`` (hooks, model, env, permissions, everything else as
``native.claude``) and ``CLAUDE.md``; optionally copies the ``hooks``,
``agents``, ``commands`` and ``skills`` trees. Import followed by the Claude
adapter must reproduce the same hooks (:func:`hooks_equivalent`).
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import yaml

from ._claude import CLAUDE_EVENTS

_FROM_CLAUDE = {v: k for k, v in CLAUDE_EVENTS.items()}
_HOOK_PATH = re.compile(
    r"^(?:\$HOME|~|\$\{HOME\}|/home/[^/]+|/Users/[^/]+)/\.claude/(hooks/\S+)$"
)


def neutral_command(command: str) -> str:
    """``$HOME/.claude/hooks/x.sh`` -> ``hooks/x.sh``; other commands verbatim."""
    match = _HOOK_PATH.match(command.strip())
    return match.group(1) if match else command


def _hook_name(command: str, taken: set[str], event: str) -> str:
    stem = Path(command.split()[0]).stem if command.strip() else "hook"
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", stem) or "hook"
    name = base if base not in taken else f"{base}-{event}"
    n = 2
    while name in taken:
        name = f"{base}-{event}-{n}"
        n += 1
    taken.add(name)
    return name


def hooks_from_claude(hooks: dict[str, Any]) -> list[dict[str, Any]]:
    """Claude ``hooks`` block -> ordered neutral ``hooks.yaml`` entries."""
    out: list[dict[str, Any]] = []
    taken: set[str] = set()
    for event, groups in hooks.items():
        neutral_event = _FROM_CLAUDE.get(event, f"claude:{event}")
        for group in groups or []:
            matcher = group.get("matcher") or ""
            for handler in group.get("hooks") or []:
                if handler.get("type", "command") != "command":
                    raise ValueError(
                        f"{event}: hook type {handler.get('type')!r} has no neutral "
                        "form; keep it in settings.yaml native.claude by hand"
                    )
                command = neutral_command(handler["command"])
                entry: dict[str, Any] = {
                    "name": _hook_name(command, taken, neutral_event.replace(":", "_")),
                    "event": neutral_event,
                }
                if matcher:
                    entry["matcher"] = matcher
                entry["command"] = command
                if handler.get("timeout") is not None:
                    entry["timeout"] = handler["timeout"]
                out.append(entry)
    return out


def _flatten(hooks: dict[str, Any]) -> list[tuple[str, str, str, Any]]:
    rows = []
    for event, groups in hooks.items():
        for group in groups or []:
            for h in group.get("hooks") or []:
                rows.append(
                    (
                        event,
                        group.get("matcher") or "",
                        h.get("command"),
                        h.get("timeout"),
                    )
                )
    return sorted(rows, key=repr)


def hooks_equivalent(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Same (event, matcher, command, timeout) handlers, ignoring grouping."""
    return _flatten(a) == _flatten(b)


def settings_from_claude(
    settings: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Split Claude settings into neutral ``settings.yaml`` + hook entries."""
    neutral: dict[str, Any] = {}
    for key in ("model", "env", "permissions"):
        if settings.get(key):
            neutral[key] = settings[key]
    native = {
        k: v
        for k, v in settings.items()
        if k not in {"hooks", "model", "env", "permissions"}
    }
    if native:
        neutral["native"] = {"claude": native}
    return neutral, hooks_from_claude(settings.get("hooks") or {})


def _dump(path: Path, data: dict[str, Any], header: str) -> None:
    body = yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
    path.write_text(f"# {header}\n{body}", encoding="utf-8")


def import_claude(
    claude_dir: Path, dest: Path, *, copy_trees: bool = False, dry_run: bool = False
) -> list[str]:
    """Write a neutral layer at ``dest`` from ``claude_dir``; returns report lines.

    Refuses to overwrite an existing ``hooks.yaml``/``settings.yaml``/``AGENTS.md``.
    With ``dry_run`` nothing is written; the report says what would be.
    """
    claude_dir, dest = Path(claude_dir), Path(dest)
    outputs = [dest / "settings.yaml", dest / "hooks.yaml", dest / "AGENTS.md"]
    existing = [str(p) for p in outputs if p.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite: {', '.join(existing)}")
    verb = "would write" if dry_run else "wrote"
    settings_path = claude_dir / "settings.json"
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8"))
        if settings_path.is_file()
        else {}
    )
    neutral, hooks = settings_from_claude(settings)
    report = [
        f"{verb} {dest / 'settings.yaml'}: {sorted(neutral)}",
        f"{verb} {dest / 'hooks.yaml'}: {len(hooks)} hooks",
    ]
    claude_md = claude_dir / "CLAUDE.md"
    if claude_md.is_file():
        report.append(f"{verb} {dest / 'AGENTS.md'} <- {claude_md}")
    trees = []
    if copy_trees:
        for src_name, dst_name in _TREES:
            src = claude_dir / src_name
            if src.is_dir() and not (dest / dst_name).exists():
                trees.append((src, dest / dst_name))
                report.append(f"{verb} {dest / dst_name}/ <- {src}")
    if dry_run:
        return report
    dest.mkdir(parents=True, exist_ok=True)
    _dump(dest / "settings.yaml", neutral, f"Neutral settings imported from {settings_path} (ADR-0033)")
    _dump(dest / "hooks.yaml", {"hooks": hooks}, f"Hooks imported from {settings_path} (ADR-0033)")
    if claude_md.is_file():
        shutil.copyfile(claude_md, dest / "AGENTS.md")
    for src, dst in trees:
        shutil.copytree(src, dst, symlinks=False, ignore=shutil.ignore_patterns("__pycache__", "*.log"))
    return report


_TREES = (
    ("hooks", "hooks"),
    ("agents", "subagents"),
    ("commands", "commands"),
    ("skills", "skills"),
)


__all__ = [
    "hooks_equivalent",
    "hooks_from_claude",
    "import_claude",
    "neutral_command",
    "settings_from_claude",
]
