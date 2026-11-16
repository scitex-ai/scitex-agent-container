"""Claude Code adapter: neutral layer -> ``~/.claude`` or ``<project>/.claude``.

Claude Code (2.1.318, measured 2026-11-16) does not load ``AGENTS.md`` on its
own — not with ``instructionFiles`` set either — while ``CLAUDE.md`` loads. So
the adapter links ``CLAUDE.md -> AGENTS.md``: one instruction file, and the
``CLAUDE.md`` name exists only as a rendered link (ADR-0033 §Instructions).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._model import Hook, Layer, NeutralConfigError
from ._plan import Action, Link, WriteFile

HARNESS = "claude"

CLAUDE_EVENTS = {
    "pre_tool": "PreToolUse",
    "post_tool": "PostToolUse",
    "stop": "Stop",
    "session_start": "SessionStart",
    "prompt_submit": "UserPromptSubmit",
    "notification": "Notification",
}

# settings.yaml keys the neutral layer owns; native.claude may not set them.
_NEUTRAL_KEYS = {"hooks", "model", "env", "permissions"}

USER = "user"
PROJECT = "project"


def _hook_prefix(scope: str) -> str:
    # Hook scripts reach Claude through the rendered ``hooks`` link, so the
    # command keeps the familiar ``$HOME/.claude/hooks/...`` shape.
    return "$HOME/.claude/" if scope == USER else "$CLAUDE_PROJECT_DIR/.claude/"


def render_command(command: str, scope: str) -> str:
    """``hooks/x.sh`` (layer-relative) -> a Claude path; anything else verbatim."""
    if command.startswith("hooks/"):
        return _hook_prefix(scope) + command
    return command


def claude_event(hook: Hook) -> str | None:
    """Claude's event name, or None when the hook targets another harness."""
    if hook.harness is None:
        return CLAUDE_EVENTS[hook.event]
    if hook.harness == HARNESS:
        return hook.event.split(":", 1)[1]
    return None


def render_hooks(hooks: list[Hook], scope: str) -> dict[str, list[dict[str, Any]]]:
    """Group hooks by event; consecutive hooks with one matcher share a group."""
    out: dict[str, list[dict[str, Any]]] = {}
    for hook in hooks:
        event = claude_event(hook)
        if event is None:
            continue
        handler: dict[str, Any] = {
            "type": "command",
            "command": render_command(hook.command, scope),
        }
        if hook.timeout is not None:
            handler["timeout"] = hook.timeout
        groups = out.setdefault(event, [])
        if groups and groups[-1].get("matcher", "") == hook.matcher:
            groups[-1]["hooks"].append(handler)
            continue
        group: dict[str, Any] = {"matcher": hook.matcher} if hook.matcher else {}
        group["hooks"] = [handler]
        groups.append(group)
    return out


def render_settings(layer: Layer, scope: str) -> dict[str, Any]:
    settings = layer.settings
    native = dict((settings.get("native") or {}).get(HARNESS) or {})
    clash = sorted(_NEUTRAL_KEYS & set(native))
    if clash:
        raise NeutralConfigError(
            f"{layer.root / 'settings.yaml'}: native.claude sets {clash}; "
            "write those as neutral keys instead"
        )
    out: dict[str, Any] = {}
    hooks = render_hooks(layer.hooks, scope)
    if hooks:
        out["hooks"] = hooks
    out.update(native)
    for key in ("model", "env", "permissions"):
        if settings.get(key):
            out[key] = settings[key]
    return out


def render_mcp(servers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Neutral servers -> Claude ``mcpServers`` (``${VAR}`` passes through)."""
    out: dict[str, Any] = {}
    for name, spec in servers.items():
        transport = spec.get("transport", "stdio")
        entry: dict[str, Any] = {"type": transport}
        keys = (
            ("command", "args", "env") if transport == "stdio" else ("url", "headers")
        )
        entry.update({k: spec[k] for k in keys if spec.get(k) is not None})
        out[name] = entry
    return {"mcpServers": out}


def _json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def plan(layer: Layer, target: Path, scope: str) -> tuple[list[Action], list[str]]:
    """Actions that make ``target`` (``.claude`` dir) match ``layer``.

    Returns ``(actions, notes)``; notes name what was deliberately not rendered.
    """
    if scope not in (USER, PROJECT):
        raise ValueError(f"scope must be {USER!r} or {PROJECT!r}, not {scope!r}")
    target = Path(target)
    actions: list[Action] = []
    notes: list[str] = []
    if layer.agents_md:
        actions.append(Link(target / "CLAUDE.md", layer.agents_md))
    for name, path in layer.docs.items():
        actions.append(Link(target / name, path))
    if layer.hooks_dir:
        actions.append(Link(target / "hooks", layer.hooks_dir))
    for sub, entries in (("agents", layer.subagents), ("commands", layer.commands)):
        if entries:
            actions.append(
                Link(
                    target / sub, layer.root / ("subagents" if sub == "agents" else sub)
                )
            )
    # skills/ stays a real directory: other owners (e.g. scitex-dev's
    # ``skills/scitex``) keep their entries; we link ours one by one.
    for name, path in layer.skills.items():
        actions.append(Link(target / "skills" / name, path))
    actions.append(
        WriteFile(target / "settings.json", _json(render_settings(layer, scope)))
    )
    skipped = sorted(
        {h.harness for h in layer.hooks if h.harness not in (None, HARNESS)}
    )
    for harness in skipped:
        notes.append(f"hooks for '{harness}' are not rendered for Claude Code")
    if layer.mcp_servers:
        if scope == PROJECT:
            actions.append(
                WriteFile(
                    target.parent / ".mcp.json", _json(render_mcp(layer.mcp_servers))
                )
            )
        else:
            notes.append(
                "user-scope MCP servers are not rendered: Claude Code keeps them in "
                "~/.claude.json beside live state, which sac does not edit (ADR-0033)"
            )
    return actions, notes


__all__ = [
    "CLAUDE_EVENTS",
    "PROJECT",
    "USER",
    "claude_event",
    "plan",
    "render_command",
    "render_hooks",
    "render_mcp",
    "render_settings",
]
