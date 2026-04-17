Quickstart
==========

1. Install the package:

.. code-block:: bash

    pip install scitex-agent-container

2. Create an agent definition directory with a YAML manifest plus
   sibling ``src_CLAUDE.md`` and ``src_mcp.json``:

.. code-block:: yaml

    # my-agent/my-agent.yaml
    apiVersion: scitex-agent-container/v2
    kind: Agent
    metadata:
      name: my-agent
      labels:
        role: worker
    spec:
      runtime: claude-code
      model: sonnet
      multiplexer: tmux
      claude:
        flags:
          - --dangerously-skip-permissions
        session: continue-or-new
      health:
        enabled: true
        interval: 60
        method: screen-alive
      restart:
        policy: on-failure
        max_retries: 3

3. Start and inspect the agent:

.. code-block:: bash

    scitex-agent-container start my-agent/my-agent.yaml
    scitex-agent-container inspect my-agent
    scitex-agent-container status my-agent --json
    scitex-agent-container logs my-agent -n 100
    scitex-agent-container attach my-agent      # Ctrl-B D to detach (tmux)

4. (Optional) Wire Claude Code hooks so ``status --json`` can surface
   recent tool calls, prompts, and sub-agent launches. See
   :doc:`status_and_hooks` for the full ``.claude/settings.local.json``
   snippet.
