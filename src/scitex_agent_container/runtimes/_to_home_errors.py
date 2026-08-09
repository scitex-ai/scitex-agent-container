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


class LayerMergeConflict(RuntimeError):
    """Two to_home cascade layers set the same scalar key to DIFFERENT values.

    The layered-config cascade (ADR-0018: user ``_shared`` → project
    ``_shared`` → per-agent) deep-merges each agent's effective
    ``.claude/settings.json`` / ``.mcp.json``. Nested dicts recurse,
    ``hooks`` blocks and lists are additive, and an identical scalar from
    two layers is idempotent — but two layers assigning the SAME key two
    DIFFERENT scalar values is a genuine conflict with no safe winner.

    Per the operator's SSOT rule (2026-06-24) the deploy hard-aborts here
    rather than silently picking a layer: each key must be owned by
    exactly ONE layer. The message names the conflicting key path, both
    layers, and both values, and the fix (set the key in only one layer).
    """


class UnknownToHomeLayer(RuntimeError):
    """``spec.to_home_layers`` names a cascade layer that does not exist.

    The declaration exists so a spec states which layers get merged into it
    (``user-shared`` / ``project-shared`` / ``per-agent``). A misspelt name has
    no matching layer, so it would quietly contribute nothing — an agent could
    declare ``user_shared`` and silently inherit no hooks at all while looking
    correctly configured.

    That is the failure the declaration was introduced to remove, so a name
    that matches no layer is refused rather than ignored. The message names the
    unknown value and the valid set.
    """


class WorkspaceSettingsMergeError(RuntimeError):
    """A ``settings.json`` to_home layer is not valid JSON — refused.

    The settings cascade (ADR-0018) deep-merges each layer's
    ``.claude/settings.json``. A layer whose file is unparseable hard-aborts
    the deploy (no silent skip) so the operator sees which layer's file is
    broken — guessing or skipping could drop hook wiring an agent needs. A
    genuine cross-layer scalar conflict surfaces as :class:`LayerMergeConflict`.
    """


__all__ = [
    "LayerMergeConflict",
    "WorkspaceSettingsMergeError",
    "WorkspaceCLAUDEMarkerError",
    "WorkspaceCredentialLeakError",
    "WorkspaceMcpMergeError",
]
