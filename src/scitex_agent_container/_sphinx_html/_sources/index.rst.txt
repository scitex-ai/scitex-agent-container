.. scitex-agent-container documentation master file

scitex-agent-container - Declarative AI Agent Lifecycle Management
===================================================================

**scitex-agent-container** is a declarative YAML-based framework for defining, managing, and orchestrating AI coding agent instances. Part of `SciTeX <https://scitex.ai>`_.

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   quickstart
   templates
   slurm
   status_and_hooks
   actions

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/scitex_agent_container

Key Features
------------

- **YAML Definitions**: Declarative agent configuration via YAML files
- **Lifecycle Management**: Create, start, stop, and destroy agent instances
- **Rich Status**: ``status --json`` emits pane state, workspace files,
  hook-captured tool history, and ``last_tool_at`` /
  ``last_mcp_tool_at`` functional-heartbeat shortcuts for dashboards
- **Claude Code Hook Integration**: ``hook-event`` ingests Claude Code
  ``PreToolUse`` / ``PostToolUse`` / ``UserPromptSubmit`` / ``Stop``
  events into a per-agent ring buffer
- **Pane Actions**: ``PaneAction`` ABC + ``actions run|query|stats|purge``
  CLI for typed, logged pane interactions (nonce-probe liveness,
  ``/compact`` on context drop) backed by a host-wide SQLite attempt
  log -- see :doc:`actions`
- **Zero Coupling**: No knowledge of any downstream orchestrator;
  consumers (e.g. scitex-orochi) wrap ``status --json`` to post to
  their own hubs

Quick Example
-------------

.. code-block:: bash

    # Create and launch an agent from YAML definition
    scitex-agent-container start my-agent.yaml

    # List running agents
    scitex-agent-container list

    # Inspect live pane state / rich status
    scitex-agent-container status my-agent --json

    # Stop an agent
    scitex-agent-container stop my-agent

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
