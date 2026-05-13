Templates and Examples
======================

Two directories ship under ``config/`` in the repo:

* ``examples/agent-templates/`` — minimal pattern templates. Each one demonstrates
  one deployment pattern with the smallest YAML that exercises it.
  Copy-and-adapt is the intended workflow.
* ``examples/agents/`` — concrete real-world configs that document
  specific operator decisions (e.g. ``newbie-docker.yaml`` carries the
  Hawthorne-effect-free design notes from the 2026-04-12 contamination
  incident).

Both directories are validated by ``tests/test_templates_v3_valid.py``.
Every YAML must round-trip through ``load_config`` cleanly; the SLURM
template additionally renders a valid sbatch script so YAML-key drift
from ``SlurmSpec`` / ``SlurmHooks`` fails loudly in CI rather than at a
user's first ``sac agent start``.

Pattern Templates
-----------------

.. list-table::
   :header-rows: 1
   :widths: 18 22 60

   * - Template
     - Pattern
     - When to use
   * - ``apptainer.yaml``
     - claude-session inside Apptainer SIF
     - **Default**. HPC + reproducibility. F-CS17 made sac
       container-only; this is the canonical pattern.
   * - ``ssh.yaml``
     - remote agent via SSH
     - Cross-machine fleet member. ``sac --on <peer>`` (F-CS12)
       dispatches across hosts; the per-agent
       ``dot_claude/`` directory is rsync'd to the remote at start.

MCP tool wiring is no longer a separate template — drop a
``.mcp.json`` into the agent's ``dot_claude/`` directory and it'll be
merged into ``<workdir>/.mcp.json`` at start (F-DC1).

Instantiating a Template
------------------------

The v3 loader uses ``dir-as-SSoT`` — the agent name is derived from the
parent directory of the YAML, not from a ``metadata.name`` field. To
instantiate:

.. code-block:: bash

    mkdir -p ~/.scitex/agent-container/agents/my-agent
    cp examples/agent-templates/apptainer.yaml \\
       ~/.scitex/agent-container/agents/my-agent/spec.yaml
    # Optionally add a dot_claude/ sibling with CLAUDE.md / .mcp.json /
    # .env / state.md / commands/ / skills/ / hooks/ (all optional).
    sac agent start my-agent

Examples
--------

.. list-table::
   :header-rows: 1
   :widths: 25 75

   * - Example
     - What it documents
   * - ``newbie-docker.yaml``
     - Hawthorne-effect-free naive-user simulation. Zero skills, zero
       fleet identity, ``mount_host_claude: false``, ``network: bridge``
       (not ``none`` — DNS blocking caused 3-min/call retry loops).
       Disposable: ``restart.policy: never``.
   * - ``researcher-opus.yaml``
     - Opus-powered researcher with ``restart.policy: on-failure``,
       backoff tuned for long-running research sessions.
