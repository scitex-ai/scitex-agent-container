"""Hermes adapter: neutral layer -> Hermes profile ``config.yaml`` hooks (rendering).

Slice 1 (this module): pure rendering only — no ``plan()``, no ``sac config
deploy --harness hermes`` wiring. Two measured facts force that boundary:

* Event vocabulary is measured, not assumed. All 5 live Hermes homes on
  scitex-compute-04 (2026-11-18) use exactly ``{pre_tool_call,
  post_tool_call}``; no native counterpart for neutral ``stop`` /
  ``session_start`` / ``prompt_submit`` / ``notification`` was observed, so
  those render as ``None`` (skipped) rather than invented.
* ``config.yaml`` carries model + credential settings regenerated per
  profile (``runtimes/_hermes_hook_config.py`` preserves only ``hooks``).
  A full-file neutral render would clobber secrets, so deploy wiring waits
  for a credential-safe merge design (slice 2).

Doctrine (skill 47): this adapter renders only what the layer declares and
grants nothing — policy/selection stays with the layer author (scitex-dev /
leaf packages own hook content), Hermes authorizes at runtime.

Measured live handler shape (dotfiles home, scitex-compute-04)::

    hooks:
      post_tool_call:
      - command: <venv>/bin/python -m scitex_dev.hooks.dispatch ...
        matcher: ^(?:Bash|apply_patch|terminal)$
        timeout: 30

Matchers pass through verbatim (live regexes already cover ``Bash`` — the
Write|Edit-only gap is closable here). Commands pass through verbatim:
live commands are absolute canonical paths and no ``$VAR`` expansion in
hook commands was evidenced for Hermes, so no prefix is invented (unlike
Claude's ``$HOME/.claude`` / ``$CLAUDE_PROJECT_DIR`` shape).
"""

from __future__ import annotations

from typing import Any

from ._model import Hook

HARNESS = "hermes"

# Neutral event -> measured Hermes native event. Events without a measured
# counterpart are absent on purpose (see module docstring).
HERMES_EVENTS = {
    "pre_tool": "pre_tool_call",
    "post_tool": "post_tool_call",
}


def render_command(command: str) -> str:
    """Neutral command -> Hermes command (verbatim; see module docstring)."""
    return command


def hermes_event(hook: Hook) -> str | None:
    """Hermes' event name, or None when unrenderable for Hermes.

    ``None`` covers harness-scoped hooks for another harness AND neutral
    events with no measured Hermes counterpart (stop, session_start,
    prompt_submit, notification).
    """
    if hook.harness is None:
        return HERMES_EVENTS.get(hook.event)
    if hook.harness == HARNESS:
        native = hook.event.split(":", 1)[1]
        return native if native in HERMES_EVENTS.values() else None
    return None


def render_hooks(hooks: list[Hook]) -> dict[str, list[dict[str, Any]]]:
    """Neutral hooks -> Hermes ``hooks:`` mapping (flat per-event lists)."""
    out: dict[str, list[dict[str, Any]]] = {}
    for hook in hooks:
        event = hermes_event(hook)
        if event is None:
            continue
        handler: dict[str, Any] = {"command": render_command(hook.command)}
        if hook.matcher:
            handler["matcher"] = hook.matcher
        if hook.timeout is not None:
            handler["timeout"] = hook.timeout
        out.setdefault(event, []).append(handler)
    return out


__all__ = [
    "HARNESS",
    "HERMES_EVENTS",
    "hermes_event",
    "render_command",
    "render_hooks",
]
