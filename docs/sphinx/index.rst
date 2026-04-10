.. scitex-agent-container documentation master file

scitex-agent-container - Declarative AI Agent Lifecycle Management
===================================================================

**scitex-agent-container** is a declarative YAML-based framework for defining, managing, and orchestrating AI coding agent instances. Part of `SciTeX <https://scitex.ai>`_.

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   quickstart

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/scitex_agent_container

Key Features
------------

- **YAML Definitions**: Declarative agent configuration via YAML files
- **Lifecycle Management**: Create, start, stop, and destroy agent instances
- **Orchestration**: Multi-agent coordination and communication
- **Claude Code Integration**: Native support for Claude Code agents
- **Telegram Integration**: Optional Telegram channel support

Quick Example
-------------

.. code-block:: bash

    # Create and launch an agent from YAML definition
    scitex-agent-container launch my-agent.yaml

    # List running agents
    scitex-agent-container list

    # Stop an agent
    scitex-agent-container stop my-agent

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
