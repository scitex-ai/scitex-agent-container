Templates and Examples
======================

``examples/agents/`` ships pattern templates as agent directories
(``hello-agent``, ``minimal-agent``, ``full-agent``, ``proxy-agent``).
Each directory holds a ``spec.yaml`` (plus an optional ``to_home/``
sibling) and demonstrates one deployment pattern with the smallest
manifest that exercises it. Copy-and-adapt is the intended workflow.

The shipped YAML must round-trip through ``load_config`` cleanly so
YAML-key drift fails loudly in CI rather than at a user's first
``sac agents start``.

Pattern Templates
-----------------

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Template
     - Pattern
     - When to use
   * - ``minimal-agent``
     - Smallest valid ``spec.yaml``
     - Starting point. The fewest fields that load and start.
   * - ``hello-agent``
     - claude-session inside an Apptainer SIF
     - **Default**. F-CS17 made sac container-only; this is the
       canonical single-turn pattern.
   * - ``full-agent``
     - Fully-featured agent
     - Health, restart, A2A, and ``to_home/`` wiring all enabled.
   * - ``proxy-agent``
     - ``kind: AgentProxy``
     - HTTP forwarder with no SDK runner — routes ``/v1/turn`` to
       another agent.

MCP tool wiring is no longer a separate template — drop a
``.mcp.json`` into the agent's ``to_home/`` directory and it lands at
``$HOME/.mcp.json`` at start (ADR-0006).

Instantiating a Template
------------------------

The v3 loader uses ``dir-as-SSoT`` — the agent name is derived from the
parent directory of the YAML, not from a ``metadata.name`` field. To
instantiate:

.. code-block:: bash

    cp -r examples/agents/hello-agent \\
       ~/.scitex/agent-container/agents/my-agent
    # The to_home/ sibling (CLAUDE.md / .mcp.json / .env / state.md /
    # .claude/{commands,skills,hooks}/) is all optional.
    sac agents start my-agent
