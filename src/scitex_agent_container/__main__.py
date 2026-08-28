#!/usr/bin/env python3
# File: src/scitex_agent_container/__main__.py

"""Allow running as: python -m scitex_agent_container"""

# Must be cli_entry_point, NOT the bare Click group: the entry point is where
# the process's ONLY host-side store identity (fleet DSN + PGUSER) is injected
# (apply_fleet_defaults_to_process) — a bare-group call leaves store-opening
# subcommands roleless, dying with "fe_sendauth: no password supplied".
from .cli import cli_entry_point

if __name__ == "__main__":
    cli_entry_point()
