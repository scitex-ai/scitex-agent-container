"""Error classes for the to_home/ materialization pipeline.

Extracted from :mod:`_to_home` to keep that module under the 512-line
file-size cap (2026-06-15: adding ``WorkspaceCredentialLeakError`` for
P0 task #6 pushed the file over). Both classes remain re-exported from
``_to_home`` for backward compatibility — existing imports
``from ...runtimes._to_home import WorkspaceCLAUDEMarkerError`` keep
working.

Co-locating the two error classes in one tiny module is intentional:
they both signal a deploy-time refusal (operator-fix required, not a
runtime fault), and grouping them keeps the file count modest while
still respecting the line-cap doctrine.
"""

from __future__ import annotations


class WorkspaceCLAUDEMarkerError(RuntimeError):
    """Existing workspace marker-protected file has malformed markers.

    The deploy is hard-aborted on this error rather than silently
    overwriting or guessing — preserving user content past the End
    marker is a safety contract and any ambiguity in marker placement
    could destroy work.
    """


class WorkspaceCredentialLeakError(RuntimeError):
    """``to_home/`` contains a ``.credentials.json`` — refused.

    Credentials are operator-rotated runtime state from the auth-stage
    rw bind, NEVER static workspace content. Lead-reported 2026-06-15:
    an expired ``.credentials.json`` under
    ``proj-scitex-todo/to_home/.claude/`` masked the bind and the
    agent OAuth-spun on a stale token. The guard is hard-loud (no
    silent skip) so the operator sees the offending source path and
    removes it; the deploy fails BEFORE any sibling files land so an
    aborted run cannot land partial state next to the leaked credential.
    """


class WorkspaceMcpMergeError(RuntimeError):
    """A ``.mcp.json`` under ``to_home/`` could not be deep-merged — refused.

    The two-pass overlay deep-merges the shared baseline ``.mcp.json`` with
    each agent's own so default servers (sac / scitex-todo /
    claude-code-telegrammer) and the agent's own both survive. The deploy is
    hard-aborted (no silent fallback) when a source ``.mcp.json`` is not valid
    JSON — guessing/overwriting could drop server wiring an agent needs. A
    genuine same-name server conflict surfaces as ``_mcp_merge.McpMergeConflict``
    (the operator resolves it explicitly).
    """


__all__ = [
    "WorkspaceCLAUDEMarkerError",
    "WorkspaceCredentialLeakError",
    "WorkspaceMcpMergeError",
]
