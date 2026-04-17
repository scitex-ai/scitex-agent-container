Status and Hook Integration
===========================

scitex-agent-container ships a non-agentic status collector and a
companion Claude Code hook ingestor. Together they let any downstream
consumer observe what an agent is doing without adding code to the
agent itself.

Rich Status
-----------

``scitex-agent-container status <name> --json`` emits a dict suitable
for dashboards or fleet monitors. It merges the registry entry with
fields collected by ``agent_meta.collect_rich()`` and
``event_log.summarize()``.

.. code-block:: bash

    scitex-agent-container status my-agent --json

Selected fields:

``pane_text``
    Recent tmux ``capture-pane`` output, with secrets (``sk-ant-*``,
    ``wks_*``, token/password key=value pairs) redacted in-place.

``pane_state``
    Deterministic classifier over ``pane_text``. One of ``running``,
    ``idle_prompt``, ``y_n_prompt``, ``auth_error``,
    ``compose_pending_unsent``, ``limit_reached``, ``unknown``.

``stuck_prompt_text``
    Last meaningful line when ``pane_state`` indicates a blocking
    prompt (e.g. a y/n question or an auth error).

``claude_md`` / ``mcp_json``
    Workspace ``CLAUDE.md`` (truncated) and ``.mcp.json`` (with
    token-style values redacted) so downstream consumers do not need
    per-host filesystem access.

``recent_tools`` / ``recent_prompts``
    Last N tool uses and user prompts drawn from the hook ring-buffer.

``agent_calls`` / ``background_tasks``
    Sub-agent launches and ``Bash`` invocations with
    ``run_in_background=true``.

``tool_counts``
    ``{tool_name: count}`` over the window.

``last_tool_at`` / ``last_tool_name``
    ISO timestamp and tool name of the newest ``pretool`` event in the
    ring buffer (any tool, e.g. ``Edit``, ``Bash``,
    ``mcp__orochi__send_message``). Acts as a *functional heartbeat*:
    lets orchestrators distinguish "process alive" from "LLM actually
    producing tool calls" without any extra collection -- the values
    are derived from the existing hook buffer.

``last_mcp_tool_at`` / ``last_mcp_tool_name``
    Same as above, restricted to the newest ``pretool`` event whose
    tool name starts with ``mcp__``. Serves as an MCP sidecar health
    probe -- confirms the MCP route is flowing, not just local tools.

``quota_5h_used_pct``, ``quota_7d_used_pct``, ``quota_*_reset_at``
    Claude usage (best-effort; cached between calls).

``metrics``
    Host-level CPU / memory / load / disk (psutil).

All fields are best-effort. Any failure leaves the default value
(``""``, ``0``, ``0.0``, ``[]``) rather than raising.

Claude Code Hook Integration
----------------------------

``scitex-agent-container hook-event <kind>`` is designed to be called
from Claude Code's hook configuration. It reads the Claude Code hook
payload from stdin and appends a compact JSON record to a per-agent
ring-buffer at ``$XDG_DATA_HOME/.scitex/agent-container/events/<agent>.jsonl``
(capped at 500 lines).

Wire it into the agent workspace's ``.claude/settings.local.json``:

.. code-block:: json

    {
      "hooks": {
        "PreToolUse":       [{"matcher": "", "hooks": [
          {"type": "command", "command": "scitex-agent-container hook-event pretool"}
        ]}],
        "PostToolUse":      [{"matcher": "", "hooks": [
          {"type": "command", "command": "scitex-agent-container hook-event posttool"}
        ]}],
        "UserPromptSubmit": [{"matcher": "", "hooks": [
          {"type": "command", "command": "scitex-agent-container hook-event prompt"}
        ]}],
        "Stop":             [{"matcher": "", "hooks": [
          {"type": "command", "command": "scitex-agent-container hook-event stop"}
        ]}]
      }
    }

Agent name resolution order:

1. ``--agent <name>`` CLI flag
2. ``SCITEX_OROCHI_AGENT`` env var
3. ``CLAUDE_AGENT_ID`` env var
4. basename of the current working directory

The handler is **fail-closed**: any error is swallowed so that a broken
event log can never abort a tool call. For ``PreToolUse`` in
particular, a non-zero exit from a hook aborts the tool use; this
implementation always exits zero.

``status --json`` reads the ring-buffer through
``event_log.summarize()`` to populate ``recent_tools``,
``recent_prompts``, ``agent_calls``, ``background_tasks``, and
``tool_counts``.

Zero Coupling Design
--------------------

scitex-agent-container has no knowledge of any particular downstream
orchestrator (scitex-orochi, a custom hub, etc.). ``status --json``
emits a self-describing dict; consumers wrap it themselves. For
example, scitex-orochi's ``heartbeat-push`` command:

1. Shells out to ``scitex-agent-container status <name> --json``.
2. Reshapes the payload into its own hub schema.
3. POSTs to its ``/api/agents/register/`` endpoint.

Keeping the library decoupled from the orchestrator lets you swap the
transport, the schema, or the orchestrator without touching this
package.
