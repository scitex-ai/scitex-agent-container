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


#: Leaf directory name every scitex package manages under the user's home.
#: (Mirrors scitex_dev.home.DOTSCITEX_DIRNAME without importing scitex-dev
#: at module scope — the import stays lazy inside ensure_home_managed.)
DOTSCITEX_DIRNAME = ".scitex"


def home_for_spec(spec_path: str | Path) -> Path | None:
    """Return the home dir whose ``.scitex`` tree contains the spec.

    Walks up from the resolved spec looking for a ``.scitex`` ancestor;
    the home is its parent. Returns None when the spec lives outside any
    ``.scitex`` tree (custom ``--base-dir``, test fixtures).

    This exists because the PROCESS home (``Path.home()``) is not always
    the DATA home: containers commonly run as root while the operative
    specs live under ``/home/user/.scitex``. Managing ``Path.home()``
    there would adopt (and commit!) the wrong tree while the real specs
    stay unprovable — exactly the failure this helper exists to close.
    """
    try:
        current = Path(spec_path).resolve()
    except OSError:
        return None
    candidates = [current, *current.parents]
    for candidate in candidates:
        if candidate.name == DOTSCITEX_DIRNAME and candidate.is_dir():
            return candidate.parent
    return None


def ensure_home_managed(
    home: str | Path | None = None,
    *,
    for_spec: str | Path | None = None,
    commit_message: str | None = None,
) -> dict:
    """Adopt ``~/.scitex`` into git management with SAC's track contract.

    Idempotent: a clean managed home stages and commits nothing. Raises
    RuntimeError with an actionable message when ``scitex-dev`` is missing
    or too old — never a silent skip, or the gate downstream fails closed
    on an unmanaged home with no explanation.

    ``for_spec`` targets the home CONTAINING that spec instead of the
    process home — required wherever the process user differs from the
    data owner (containers). Explicit ``home`` wins over ``for_spec``.
    """
    if home is None and for_spec is not None:
        home = home_for_spec(for_spec)
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
    "DOTSCITEX_DIRNAME",
    "SAC_EXTRA_IGNORE",
    "SAC_TRACK",
    "ensure_home_managed",
    "home_for_spec",
]
