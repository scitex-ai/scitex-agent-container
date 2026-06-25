"""Static Claude Code hook-wiring SAC injects into every agent.

``_HOOKS_CONFIG`` is pushed into each spawned agent's settings file (via
:func:`scitex_agent_container.runtimes.settings_json.setup_settings_json`) so
the agent's Claude Code lifecycle events flow into SAC's per-agent event
ring-buffer (``~/.scitex/agent-container/runtime/events/<agent>.jsonl``,
consumed by ``event_log.summarize()`` which feeds the Orochi dashboard rows).

Each entry routes a Claude Code hook event to
``scitex-agent-container event ingest <kind>`` (see
:mod:`scitex_agent_container.cli_pkg.hook_cmds`).

Extracted from ``settings_json.py`` to keep that orchestrator under the
512-line cap and to give the hook wiring a single cohesive home.
"""

from __future__ import annotations

# Hook config pushed into every spawned agent's settings file so
# PreToolUse / PostToolUse / UserPromptSubmit / Stop / Notification events
# flow into the per-agent event ring-buffer. Without the first four, the
# dashboard's Last tool / Last MCP / Last action rows render as dashes
# (scitex-orochi todo#59).
_HOOKS_CONFIG = {
    "PreToolUse": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest pretool",
                }
            ],
        }
    ],
    "PostToolUse": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest posttool",
                }
            ],
        }
    ],
    "UserPromptSubmit": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest prompt",
                }
            ],
        }
    ],
    "Stop": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest stop",
                }
            ],
        }
    ],
    # Notification fires when Claude Code is waiting for input / permission
    # (matcher types: permission_prompt, idle_prompt, auth_success,
    # elicitation_dialog). The ingest handler records the blocker on the
    # agent's active in_progress card so the board shows the agent is stuck
    # even if the agent itself never speaks up (sac-card-anchored-stop-
    # reconciler — the live failure: a maintainer sat blocked at a "Submit
    # answers" prompt and the operator only found it via the terminal).
    "Notification": [
        {
            "matcher": "",
            "hooks": [
                {
                    "type": "command",
                    "command": "scitex-agent-container event ingest notification",
                }
            ],
        }
    ],
}

__all__ = ["_HOOKS_CONFIG"]
