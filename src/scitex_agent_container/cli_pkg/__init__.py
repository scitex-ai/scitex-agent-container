"""scitex-agent-container CLI package.

Split out from the original monolithic ``cli.py`` so each topic area
stays under the project's 512-line limit. Two public entry points:

  * ``main`` — the Click group (used by tests and programmatic invocation).
  * ``cli_entry_point`` — the console-script wrapper that honours the
    global ``--on <peer>`` flag for transparent remote dispatch
    (F-CS12 phase 3) before falling through to ``main()``.

Existing imports of ``main`` continue to work unchanged; ``--on`` is
only consumed by the entry-point wrapper, never by ``main`` itself.
"""

from ._main import cli_entry_point, main

__all__ = ["cli_entry_point", "main"]
