"""``python -I -m scitex_agent_container._sandbox supervise ...`` (see _supervisor)."""

from __future__ import annotations

import sys

from ._supervisor import main


def _entry(argv: list[str]) -> int:
    if not argv or argv[0] != "supervise":
        sys.stderr.write("usage: python -m scitex_agent_container._sandbox supervise ...\n")
        return 2
    return main(argv[1:])


if __name__ == "__main__":
    sys.exit(_entry(sys.argv[1:]))
