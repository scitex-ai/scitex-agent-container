"""Ensure the operator's ``~/.scitex`` is git-managed before spec writes/reads.

Single home for SAC's track contract: the spec files a launch must prove,
plus the package runtime dirs that must never commit. Both ``sac agents
create`` (via ``cli_pkg._create.scaffold_agent``) and the launch gate (via
``_lifecycle._start_preflight``) funnel through :func:`ensure_home_managed`
so the authority proof in ``._authority`` never meets an unmanaged home.

``scitex-dev`` is imported LAZILY and its absence is fatal and loud — the
floor (``scitex-dev>=0.61.0``, first release carrying ``scitex_dev.home``)
belongs in ``pyproject.toml`` dependencies once that release ships; until
then this ImportError is the pin.
"""

from __future__ import annotations

from pathlib import Path

#: Spec inputs a launch must prove: the agent spec plus the materialised
#: container-$HOME source. Anything else under an agent dir (notably stray
#: runtime files) stays untracked, keeps the repo dirty, and fails the
#: launch LOUDLY — the operator moves the file instead of it silently
#: riding into history.
SAC_TRACK = (
    "agent-container/agents/*/spec.yaml",
    "agent-container/agents/*/to_home/**",
    "agent-container/agents/*/*.md",
    "agent-container/agents/*/*.yaml",
    "agent-container/agents/*/*.yml",
    "agent-container/agents/*/*.json",
    "agent-container/agents/*/*.sh",
)

#: Package runtime state that must never commit (container overlays hold
#: whole root filesystems; the generic ``<pkg>/runtime/`` contract in
#: scitex-dev covers the rest).
SAC_EXTRA_IGNORE = (
    "agent-container/containers/",
)


def ensure_home_managed(
    home: str | Path | None = None, *, commit_message: str | None = None
) -> dict:
    """Adopt ``~/.scitex`` into git management with SAC's track contract.

    Idempotent: a clean managed home stages and commits nothing. Raises
    RuntimeError with an actionable message when ``scitex-dev`` is missing
    or too old — never a silent skip, or the gate downstream fails closed
    on an unmanaged home with no explanation.
    """
    try:
        from scitex_dev.home import ensure_dotscitex_managed_by_git
    except ImportError as exc:
        raise RuntimeError(
            "sac needs scitex-dev>=0.61.0 (scitex_dev.home) to manage "
            f"~/.scitex for spec authority: {exc}"
        ) from exc
    kwargs: dict = {"track": SAC_TRACK, "extra_ignore": SAC_EXTRA_IGNORE}
    if commit_message is not None:
        kwargs["commit_message"] = commit_message
    return ensure_dotscitex_managed_by_git(home, **kwargs)


__all__ = [
    "SAC_EXTRA_IGNORE",
    "SAC_TRACK",
    "ensure_home_managed",
]
