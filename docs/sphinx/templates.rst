Templates and Examples
======================

The source checkout contains examples under ``examples/agents/`` as agent
directories, including ``hello-agent``, ``minimal-agent``, ``full-agent`` and
``proxy-agent``. These are separate from the installed CLI's inline presets
and locally discovered directory templates.
Each directory holds a ``spec.yaml`` (plus an optional ``to_home/``
sibling) and demonstrates a deployment pattern. Copy-and-adapt is the
intended workflow.

These repository examples must round-trip through ``load_config`` cleanly so
YAML-key drift fails loudly in CI. Loading a spec is not evidence that its
credentials, images, engine or host are ready for ``sac agents start``.

Creating a Spec
---------------

.. code-block:: bash

    sac agents create my-agent --template minimal --dry-run
    sac agents create my-agent --template minimal --yes
    sac agents create --base-dir /path/to/agents --help

The built-in ``minimal`` and ``full`` presets come from
``cli_pkg/_create_templates.py``. Help also discovers ``_template_<kind>/``
directories under the selected agents root. ``create`` writes a spec and
home skeleton; it does not launch an agent. Review warnings and the resulting
spec before starting it. Existing inline specs are not overwritten.

Normal CLI creation requires ``--yes/-y``; scripts must supply it explicitly.
An explicit Python/MCP creation call forwards approval. Preview takes precedence,
and approval does not permit overwriting a spec or bypass other backend checks.

``--dry-run`` reports arguments only and hides ``--set`` values. It does not
read templates or credentials, resolve the default root, check destination
state or qualify a launch. MCP ``agent_create(dry_run=True)`` also suppresses
the optional follow-up start.
Cold CLI startup may initialize shared logging; preview does not write agent files.
Python callers use ``scitex_agent_container.agent.create(..., dry_run=True)``,
the same callable registered for MCP.

The shared implementation is ``cli_pkg/_create.py:scaffold_agent``. Its
scaffolding and field controls are in
``tests/scitex_agent_container/cli_pkg/test__create.py`` and
``test__create_templates.py``. Repository examples have separate loader and
asset checks in ``tests/integration/test_templates_v3_valid.py``.

.. _pattern-templates:

Selected Repository Examples
----------------------------

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Example
     - Pattern
     - When to use
   * - ``minimal-agent``
     - Smallest valid ``spec.yaml``
     - Starting point for review and configuration; loading does not prove launch readiness.
   * - ``hello-agent``
     - Anthropic harness inside an Apptainer SIF
     - An explicit ``harness: anthropic`` example. Choose the harness and
       engine for the intended environment; see :doc:`spec-reference`.
   * - ``full-agent``
     - Broader configuration example
     - Review its health, restart, A2A and ``to_home/`` settings for the
       intended environment.
   * - ``proxy-agent``
     - ``kind: AgentProxy``
     - HTTP forwarder with no SDK runner — routes ``/v1/turn`` to
       another agent.

MCP tool wiring is no longer a separate template — drop a
``.mcp.json`` into the agent's ``to_home/`` directory and it lands at
``$HOME/.mcp.json`` at start (ADR-0006).

.. _instantiating-a-template:

Copying a Repository Example
----------------------------

The v3 loader uses ``dir-as-SSoT`` — the agent name is derived from the
parent directory of the YAML, not from a ``metadata.name`` field.

As an alternative to CLI scaffolding, copy an example into a new directory
from a source checkout. Review the copied spec and home assets and qualify
runtime prerequisites before starting the agent.

.. code-block:: bash

    mkdir -p ~/.scitex/agent-container/agents
    cp -r examples/agents/hello-agent ~/.scitex/agent-container/agents/copied-agent
    # The to_home/ sibling (AGENTS.md / .mcp.json / .env / state.md /
    # .claude/{commands,skills,hooks}/) is all optional.
