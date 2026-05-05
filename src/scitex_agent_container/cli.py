"""Click-based CLI for scitex-agent-container.

This module is a thin shim over :mod:`scitex_agent_container.cli_pkg`.
The actual command logic lives in that subpackage — split out because
the monolithic cli.py grew past the project's 512-line file limit and
was blocking edits (e.g., the new ``stop --all`` / ``start --force``
flags landing in ``cli_pkg.lifecycle_cmds``).

Importers that did::

    from scitex_agent_container.cli import main

still work because ``main`` is re-exported here.
"""

from __future__ import annotations

from .cli_pkg import cli_entry_point, main

# Console-script entry: honours --on <peer> before Click parses the
# subcommand (F-CS12 phase 3). Importers that did::
#
#     from scitex_agent_container.cli import main
#
# get the Click group unchanged. New code wanting transparent remote
# dispatch should use ``cli_entry_point``.
__all__ = ["cli_entry_point", "main"]


if __name__ == "__main__":
    cli_entry_point()
