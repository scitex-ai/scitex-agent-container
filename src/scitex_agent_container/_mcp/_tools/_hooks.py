"""MCP tool: what Claude Code hooks does THIS container actually enforce?

Its own leaf module rather than another entry in ``_agent.py`` for two
reasons: ``_agent.py`` is already over the per-file line cap, and this is not
an agent-LIFECYCLE verb — it is a self-measurement, closer in kind to
``_info.mcp_doctor`` than to ``agent_restart``.
"""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json


def agent_hooks(name: str = "") -> dict[str, Any]:
    """Which Claude Code hooks are ACTUALLY armed here, and is the declared
    floor met? Mirrors ``sac agents hooks [<name>] --json``.

    Returns the cross-package standard health report — ``{"package", "ok",
    "checks": [{name, ok, detail, hint}], "summary"}`` — plus the raw
    ``inventory`` (every armed script, per hook event directory) and the
    ``floor`` verdict. Each check's ``ok`` is THREE-VALUED: ``true`` / ``false``
    / ``null`` (UNKNOWN — "I could not measure this"), and every failing OR
    unknown check carries an actionable ``hint``.

    MEASURED WHERE IT IS CONSUMED, which is the entire point of this tool. An
    agent's effective hook set is the union of TWO stacked mounts over
    ``/home/agent``, and reading either one from the host UNDERCOUNTS: measured
    2026-08-10, a host-side layer read reported 67 pre-tool-use hooks and
    called ``log_post_tool_use.sh`` missing, while the same listing inside that
    container returned 71 and the hook was there.

    So this answers for the container the MCP server is running in. Called
    about a DIFFERENT agent it does not guess: ``measurement_site`` reports
    UNKNOWN and the floor verdict stays ``null`` rather than answering
    confidently about the wrong ``$HOME``. To ask about a peer, reach INTO it —
    ``agent_send(<peer>, "sac agents hooks --json")``.

    ``name`` defaults to ``$SAC_NAME`` (this agent).
    """
    argv = ["agents", "hooks"]
    if name:
        argv.append(name)
    return invoke_cli_json(argv + ["--json"])


def register_hooks_tools(mcp) -> None:
    """Attach ``@mcp.tool()`` to every public function in this module."""
    for fn in (agent_hooks,):
        mcp.tool()(fn)


__all__ = ["agent_hooks", "register_hooks_tools"]
