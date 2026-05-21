Quickstart
==========

1. Install the package:

.. code-block:: bash

    pip install scitex-agent-container

   The fastest path is to copy a pattern template from
   ``examples/agents/`` (``hello-agent``, ``minimal-agent``,
   ``full-agent``, ``proxy-agent``). See :doc:`templates` for details.

2. Create an agent definition directory with a ``spec.yaml`` manifest
   plus an optional ``to_home/`` sibling (CLAUDE.md / .mcp.json / ...):

.. code-block:: yaml

    # ~/.scitex/agent-container/agents/my-agent/spec.yaml
    apiVersion: scitex-agent-container/v3
    kind: Agent
    metadata:
      labels:
        role: worker
    spec:
      runtime: apptainer
      apptainer:
        image: ~/.scitex/agent-container/containers/sac-base.sif
      claude:
        model: sonnet
        flags:
          - --dangerously-skip-permissions
        session: continue-or-new
      health:
        enabled: true
        interval: 60
        method: sdk-alive
      restart:
        policy: on-failure
        max_retries: 3

3. Start and inspect the agent:

.. code-block:: bash

    sac agents start my-agent
    sac agents list my-agent --json
    sac agents tail my-agent
    sac agents recall my-agent

4. (Optional) Wire Claude Code hooks so ``list --json`` can surface
   recent tool calls, prompts, and sub-agent launches. See
   :doc:`status_and_hooks` for the full ``.claude/settings.local.json``
   snippet.
