"""Agent-workdir hygiene: auditing heavy trees, and what a walk must skip.

Both modules here answer one question — *what is in an agent's workdir,
and how much of it should a synchronous walk touch?*

* :mod:`._audit` — the F-CS8 ``<workdir>/.claude/`` bloat audit. A heavy
  ``.claude/`` tree makes the claude-agent-sdk time out spawning MCP
  servers or swallow discovery errors and return 0 tokens per turn, with
  no log line either way.
* :mod:`._walk_exclusions` — the shared "do not descend into this"
  predicate for SAC's heavy filesystem walks. The audit's own walk is one
  consumer; ``runtimes/_host_merge`` and ``runtimes/_symlink_resolve``'s
  ``to_home`` deref-copy are the others.

They were two flat modules at the package root until PS-108b (>15 flat
``.py`` files under ``src/scitex_agent_container/``) fired on develop:
three individually-clean merges crossed a CUMULATIVE threshold together,
and every PR inherited the red because PR jobs test the merge result.
Grouping the pair by responsibility is the fix the rule asks for — the
threshold is not the problem, an ungrouped root is.

This ``__init__`` re-exports only the surface SRC consumes; tests reach
into ``._audit`` / ``._walk_exclusions`` directly, which is also what the
PS-205 test mirror (``tests/…/_workdir/test__audit.py``) names.
"""

from __future__ import annotations

from ._audit import audit_workdir_claude, to_dict

__all__ = ["audit_workdir_claude", "to_dict"]
