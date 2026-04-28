SLURM Runtimes
==============

scitex-agent-container ships two SLURM runtimes:

.. list-table::
   :header-rows: 1
   :widths: 22 78

   * - Runtime
     - Use when
   * - ``slurm``
     - You want one agent per SLURM job. The agent's sbatch wrapper holds
       the allocation, runs ``claude`` in tmux on the compute node, and
       auto-resubmits before walltime via a ``SIGUSR1`` trap. This is
       what production agents like ``head-spartan`` use.
   * - ``slurm-tenant``
     - You want **many agents inside one allocation**. Book a reservation
       once, launch agents as tenants, pay the queue wait only once for
       the whole pool. Requires ``scitex-hpc>=0.6.1`` (provides the
       ``Reservation`` primitive with tmux-server bootstrap).

Both runtimes are bastion-initiated (SSH from a non-HPC host into the
SLURM submission host). No persistent daemons or outbound tunnels are
ever installed — compatible with HPC policies that forbid them
(reference: 2026-04-26 IT Security ruling on Spartan).

Single-agent (``runtime: slurm``)
---------------------------------

Submit one agent as one ``sbatch`` job with auto-resubmit:

.. code-block:: yaml

    apiVersion: scitex-agent-container/v3
    kind: Agent
    spec:
      runtime: slurm
      model: opus
      slurm:
        partition: cascade
        cpus_per_task: 4
        mem: "16G"
        time_limit: "7-00:00:00"
        auto_resubmit: true
        hooks:
          pre_agent: ~/path/to/module-load.sh   # source 'module load Python/3.11.3' etc.

Lifecycle:

.. code-block:: bash

    sac start head-spartan/head-spartan.yaml   # submits sbatch on the SLURM submission host
    sac status head-spartan                    # squeue + tmux pane state
    sac attach head-spartan                    # srun --pty + tmux attach on the compute node
    sac logs head-spartan -n 100               # tmux capture-pane via srun --overlap
    sac stop head-spartan                      # scancel + clear local state

The ``slurm.hooks.pre_agent`` script is *sourced* (not exec'd) inside
the sbatch wrapper, so any env it sets persists into the agent
process. This is where ``module load Python/3.11.3`` lives on Lmod
clusters.

Multi-tenant (``runtime: slurm-tenant``)
-----------------------------------------

Cuts queue wait from minutes per agent launch to one ssh round-trip
per launch. Pattern: book the node once via ``scitex-hpc``, then start
tenant agents into it.

Step 1 — book the reservation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    scitex-hpc reservations book dev-pool \
        --host spartan --partition cascade \
        --cpus 8 --mem 32G --time 7-0 \
        --tmux-server sac --persistent

Two flags matter:

* ``--tmux-server sac`` bootstraps a long-lived tmux server as PID 1
  of the sbatch script. Tenant tmux sessions connect to this server
  via ``tmux -L sac …`` and so live in the **job's** cgroup, not in
  transient ``srun --overlap`` step cgroups (which would kill them on
  step exit). Without this flag, ``runtime: slurm-tenant`` will refuse
  to start with a clear error message.
* ``--persistent`` enables walltime auto-resubmit via SLURM's
  ``SIGUSR1`` signal — when the job is 1 hour from walltime, the
  trap calls ``sbatch "$0"`` to resubmit itself in place. The
  reservation's friendly name stays stable; only the SLURM ``job_id``
  changes. Use ``scitex-hpc reservations refresh dev-pool`` to update
  the cached id.

Step 2 — write tenant YAMLs
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A minimal tenant yaml:

.. code-block:: yaml

    apiVersion: scitex-agent-container/v3
    kind: Agent
    spec:
      runtime: slurm-tenant
      model: sonnet
      slurm:
        reservation: dev-pool       # name of the existing scitex-hpc lease
      claude:
        flags: [--dangerously-skip-permissions]

Drop multiple yamls under ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (or
``~/.scitex/agent-container/agents/``) following the dir-as-SSoT
layout (``<name>/<name>.yaml``).

Step 3 — start agents into the allocation
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    sac start dev-helper.yaml         # tmux session in dev-pool's allocation
    sac start doc-builder.yaml        # second session, same allocation
    sac start test-runner.yaml        # third, same allocation

Each ``sac start`` becomes one ``tmux -L sac new-session`` inside the
existing reservation — no new ``sbatch`` is submitted. The whole
operation is one ssh round-trip per agent.

Or launch them all at once:

.. code-block:: bash

    sac start --all                   # discovers all yamls, starts each

Step 4 — operate them
^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

    sac list                          # registry view; tenants show alongside other agents
    sac attach dev-helper             # srun --pty + tmux -L sac attach -t sac-dev-helper
    sac logs dev-helper -n 100        # tmux capture-pane via srun --overlap
    sac stop dev-helper               # tmux kill-session (does NOT release the allocation)
    sac stop --all                    # kill every tenant; reservation still alive

Stopping a tenant only kills its tmux session; the reservation outlives
its tenants. Releasing the reservation is a separate scitex-hpc CLI
call:

.. code-block:: bash

    scitex-hpc reservations release dev-pool

Architectural notes
-------------------

* **Why the tmux server has to be PID 1 of the job**: SLURM kills *all
  processes in a step's cgroup* when the step ends. A ``tmux
  new-session`` spawned via ``srun --jobid --overlap`` runs in such a
  step and gets killed within ~2 seconds (verified live on
  spartan-bm021 2026-04-28). The
  ``Reservation.book(tmux_server="sac", ...)`` call wires
  ``tmux -L sac new-session -d -s _root 'sleep infinity'`` into the
  sbatch script *body* — making the tmux server itself the job's main
  process. Tenants then connect via the same ``-L sac`` socket and
  their sessions are siblings of ``_root``, all in the job's cgroup.
* **Why bastion-initiated**: every ``sac`` call from your laptop
  results in ``ssh <host> 'bash -lc "srun --jobid=… --overlap …"'``.
  The HPC side never initiates a connection back. No persistent
  daemons, no autossh, no cloudflared, no ``crontab @reboot`` — the
  whole architecture is policy-compliant by construction (matches
  the 2026-04-26 IT Security ruling on Spartan).

Migration path
--------------

To move an existing ``runtime: slurm`` agent into a multi-tenant
allocation:

1. Book a reservation that fits the agent's resource requirements
   (``--cpus``, ``--mem``, ``--time``, ``--partition``).
2. Change the agent's yaml from ``runtime: slurm`` to
   ``runtime: slurm-tenant`` and replace the entire ``slurm:`` block
   with ``slurm: {reservation: <pool-name>}``.
3. ``sac stop`` the old agent (or wait for its job to walltime-out).
4. ``sac start`` the migrated yaml.

The agent runs in the same shell context (Python venv, env vars from
``pre_agent`` hook fragments — though tenants don't get the
``slurm.hooks.pre_agent`` lifecycle, since the reservation owns the
script). If your agent needs ``module load`` etc., wire that into the
reservation's ``hold_body`` via ``Reservation.book(hold_body=...)``.

Troubleshooting
---------------

* ``runtime: slurm-tenant requires spec.slurm.reservation`` — the
  yaml is missing the ``reservation`` field.
* ``reservation 'foo' was not booked with tmux_server set`` — re-book
  with ``--tmux-server sac``.
* Tenant tmux session disappears immediately — almost certainly the
  reservation was booked without ``--tmux-server``. Run
  ``scitex-hpc reservations get <name>`` and check
  ``"extras": {"tmux_server": "sac"}`` is set.
* ``sac attach`` exits immediately — same as above; or the session
  was killed externally. Run ``sac logs`` first to see whether the
  process inside crashed.
