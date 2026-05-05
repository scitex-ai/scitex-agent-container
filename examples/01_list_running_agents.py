#!/usr/bin/env python3
"""List every registered agent + liveness using sac's public Python API.

Equivalent to:

    sac agent list --json

…but useful when sac is embedded inside a longer-running orchestrator
(orochi, a notebook, a CI step, etc.). Outputs auto-organized by
``@stx.session`` to ``script_out/FINISHED_SUCCESS/<session_id>/``.
"""

from __future__ import annotations

import scitex as stx


@stx.session
def main(
    capability: str = "",
    machine: str = "",
    logger=stx.INJECTED,
):
    """List sac agents (optionally filtered).

    Args:
        capability: Substring filter for the agent's capabilities label.
        machine:    Exact-match filter for the agent's machine label.
    """
    from scitex_agent_container._state.registry import Registry
    from scitex_agent_container.cli_pkg._helpers import get_agent_list_data

    rows = get_agent_list_data(
        Registry(),
        capability=capability or None,
        machine=machine or None,
    )
    payload = {"count": len(rows), "agents": rows}
    logger.info(f"Found {payload['count']} agent(s)")
    stx.io.save(payload, "agents.json")
    return 0


if __name__ == "__main__":
    main()
