Installation
============

From PyPI
---------

.. code-block:: bash

    pip install scitex-agent-container

From Source
-----------

.. code-block:: bash

    git clone https://github.com/ywatanabe1989/scitex-agent-container.git
    cd scitex-agent-container
    pip install -e .

Requirements
------------

- Python >= 3.10
- The harness your specs select, installed in the image: the Claude Code
  CLI for ``harness: anthropic`` (the default), the ``openai-agents``
  SDK for ``harness: openai``, the ``openai-codex`` SDK for
  ``harness: codex``. sac itself requires none of them. Note that only
  the ``anthropic`` harnesses can currently be started — see
  :doc:`how-sac-works`.
