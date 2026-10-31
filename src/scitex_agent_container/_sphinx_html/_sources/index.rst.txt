.. scitex-agent-container documentation master file

scitex-agent-container — Declarative, On-Prem-First Agent Container Runtime
===========================================================================

**scitex-agent-container** (``sac``) turns one ``spec.yaml`` into one
long-lived, sandboxed, fleet-addressable agent process. Runs
anywhere Apptainer runs — laptop, HPC login node, on-prem cluster,
air-gapped server. Part of `SciTeX <https://scitex.ai>`_.

.. toctree::
   :maxdepth: 2
   :caption: Getting Started

   installation
   quickstart
   templates
   status_and_hooks

.. toctree::
   :maxdepth: 2
   :caption: Architecture & Reference

   how-sac-works
   spec-reference
   talking-to-agents
   isolation
   directories
   credentials
   images

.. toctree::
   :maxdepth: 2
   :caption: API Reference

   api/scitex_agent_container

Why sac
-------

- **Declarative agents.** One ``spec.yaml`` per agent — the file *is*
  the agent (dir-as-SSoT). Reproducible, version-controlled,
  diff-reviewable.
- **Rootless Apptainer isolation.** Runs where Docker/cloud sandboxes
  cannot — HPC login nodes, on-prem, air-gapped. No root, no daemon,
  no Docker socket. Hardened by default via ``--containall``.
- **Two independent axes, one spec.** sac owns the agent *process*; a
  *harness* owns only the *turn*. ``spec.harness`` picks the harness —
  ``anthropic`` (default; ``spec.runtime`` then picks the Claude Code
  TUI or the headless Claude Agent SDK), ``openai``, or ``codex``.
  ``spec.claude.provider`` is the separate *inference* axis: which
  endpoint answers the turn. **Only the ``anthropic`` harnesses can be
  started today** — the other two validate and resolve, then the launch
  path refuses them rather than silently running a Claude runner. See
  :doc:`spec-reference` and :doc:`how-sac-works`.
- **On-prem-capable inference.** Default: Anthropic OAuth.
  Alternative: any Anthropic-API-compatible backend (DeepSeek, MiMo /
  Xiaomi, a self-hosted LiteLLM / vLLM-with-Anthropic-shim gateway)
  via ``spec.claude.provider``. Data, code, and inference can stay
  entirely on your network.
- **Fleet ops out of the box.** A2A push (``POST /v1/turn``),
  health / heartbeat, restart policies, multi-account credential
  rotation with auto-quota-watch, MCP + CLI + Python surface,
  cross-host orchestration via ``sac fleet``.
- **AGPL-3.0.** Research-freedom license — infrastructure stays open,
  modifications stay shareable.

Quick Example
-------------

.. code-block:: bash

    # Create and launch an agent from its YAML definition
    sac agents start my-agent

    # Fleet view / per-agent status
    sac agents status
    sac agents status my-agent --json

    # Send a follow-up turn to a running agent
    sac agents send my-agent "What is 2+2?"

    # Tail the structured transcript
    sac agents tail my-agent

    # Stop / delete
    sac agents stop my-agent
    sac agents delete my-agent -y

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
