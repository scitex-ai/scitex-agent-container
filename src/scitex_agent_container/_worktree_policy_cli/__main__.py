"""Run the checker through ``python -m`` or its installed absolute path."""

from __future__ import annotations

if __package__:
    from ._cli import main
else:
    # Lifecycle uses this installed path so cwd and PYTHONPATH cannot select
    # another SAC checkout. The parent package retains its lazy imports.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from scitex_agent_container._worktree_policy_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
