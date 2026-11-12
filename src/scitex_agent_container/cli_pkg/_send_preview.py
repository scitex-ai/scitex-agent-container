"""Argument-only send plans; no endpoint, credential or lifecycle inspection."""

from __future__ import annotations

from typing import Any


def build_send_preview(
    name: str,
    *,
    prompt: str | None,
    key: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    """Describe supplied arguments without echoing prompt/control-key contents."""
    return {
        "action": "send-agent",
        "dry_run": True,
        "scope": "arguments_only",
        "agent": name,
        "input_kind": "prompt" if prompt else "control",
        "input_characters": len(prompt or key or ""),
        "model": model,
        "max_turns": max_turns,
        "wait": wait,
        "unverified": [
            "host and endpoint resolution",
            "authorization and credentials",
            "agent state and lifecycle policy",
            "delivery and completion",
        ],
    }
