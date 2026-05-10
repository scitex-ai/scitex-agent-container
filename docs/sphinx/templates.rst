Templates and Examples
======================

Two directories ship under ``config/`` in the repo:

* ``examples/agent-templates/`` — minimal pattern templates. Each one demonstrates
  one deployment pattern with the smallest YAML that exercises it.
  Copy-and-adapt is the intended workflow.
* ``examples/agent-specs/`` — concrete real-world configs that document
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
     - Runtime
     - When to use
   * - ``local.yaml``
     - claude-code (no container)
     - Default. Agent shares the operator's environment — skills, MCP,
       venv, ``claude`` CLI on PATH.
   * - ``docker.yaml``
     - claude-code in Docker
     - Local isolation. Container is the boundary;
       ``mount_host_claude`` is opt-in so host identity does not leak.
   * - ``apptainer.yaml``
     - claude-code in Apptainer/Singularity
     - HPC compute nodes or locked-down hosts where Docker is
       unavailable. Pair with ``ssh-slurm`` for SIFs on shared FS.
   * - ``ssh.yaml``
     - claude-code via SSH
     - Cross-machine fleet member. ``sac`` copies YAML +
       ``src_CLAUDE.md`` / ``src_mcp.json`` to the remote and drives
       lifecycle over SSH. Set ``no_preflight: true`` for HPC login
       nodes that need ``module load`` before ``python3``.
   * - ``ssh-slurm.yaml``
     - SLURM
     - Long-running compute on a shared cluster. Wraps the agent in
       ``sbatch``, runs in tmux on the compute node, holds the
       allocation, and auto-resubmits ~1h before walltime via a
       ``SIGUSR1`` trap. Use ``slurm.hooks.pre_agent`` for ``module
       load`` etc.
   * - ``mcp.yaml``
     - claude-code with MCP wiring
     - Agent that needs MCP tool access. Demonstrates the
       ``mcp_servers`` block with stdio transport and ``${metadata.name}``
       / ``${ENV_VAR}`` interpolation.

Instantiating a Template
------------------------

The v3 loader uses ``dir-as-SSoT`` — the agent name is derived from the
parent directory of the YAML, not from a ``metadata.name`` field. To
instantiate:

.. code-block:: bash

    mkdir -p ~/.scitex/orochi/agents/my-agent
    cp examples/agent-templates/local.yaml ~/.scitex/orochi/agents/my-agent/my-agent.yaml
    # Edit fields you want to customize, then:
    scitex-agent-container start my-agent

For SSH and SSH+SLURM patterns, also drop sibling ``src_CLAUDE.md`` and
``src_mcp.json`` files into the agent directory; ``sac`` copies them to
``/tmp/`` on the remote and materializes them into the workspace at
agent start.

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
