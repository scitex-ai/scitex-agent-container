"""Per-agent ``$HOME`` materialisation for the TUI runtime.

Extracted from :mod:`tui_session` (which re-exports :func:`materialize_workspace`,
so existing imports keep working) to keep that module under the line limit. The
logic mirrors ``ClaudeSessionRuntime._setup_workspace`` EXACTLY so the
in-apptainer TUI gets the same ``$HOME`` surface as the SDK path; see the
:func:`materialize_workspace` docstring for the per-step rationale.
"""

from __future__ import annotations

from pathlib import Path

from ..config import AgentConfig
from ._skills_boot_log import log_effective_skills
from ._to_home import deploy_to_home
from ._to_home_overlay import deploy_to_home_overlay, resolve_overlay_upper_home
from .claude_md import setup_claude_md
from .onboarding import ensure_project_onboarding
from .settings_json import ensure_global_settings_json, setup_settings_json

__all__ = ["materialize_workspace"]

_REQUIRED_CONFIG_ATTRS = ("expanded_workdir", "skills", "claude", "env", "labels")


def materialize_workspace(
    config: AgentConfig,
    *,
    state_dir_for_config,
) -> Path | None:
    """Materialise per-agent ``to_home/`` + CLAUDE.md into the container ``$HOME``
    and return the host-side ``<state>/home/`` path.

    ``state_dir_for_config`` is injected (the runtime passes its module-level
    resolver) to avoid an import cycle with :mod:`tui_session`.

    Mirrors ``ClaudeSessionRuntime._setup_workspace`` EXACTLY so the in-apptainer
    TUI gets the same $HOME surface as the SDK path:

      * ``setup_claude_md`` writes the sac-managed CLAUDE.md skill chain into
        ``<state>/home/CLAUDE.md``.
      * ``deploy_to_home`` overlays the shared ``_shared/to_home`` baseline +
        per-agent ``to_home/`` (.mcp.json, .env, .claude/{hooks,skills,
        settings.json}) into ``<state>/home/`` (the host dir bound at
        ``/home/agent``). The baseline ships its hook suite as
        ``.claude/settings.json`` so the TUI reads it at USER scope.
      * ``setup_settings_json`` deep-merges sac's managed keys into
        ``$HOME/.claude/settings.json`` (``filename="settings.json"``):
        ``skipDangerousModePermissionPrompt`` + the SAC channel/event-ring hooks
        + statusLine — PRESERVING the ``_shared`` baseline's honest-grounding
        Stop gate / lint PostToolUse. The interactive TUI reads hooks from
        ``$HOME/.claude/settings.json`` at USER scope and NEVER from a
        ``$HOME/.claude/settings.local.json`` (no such scope), so the file MUST
        be ``settings.json`` or the whole hook suite is INERT. MUST run after
        ``deploy_to_home``; it also folds a legacy ``settings.local.json``
        sibling forward so a baseline deployed under the old name survives.
      * ``deploy_to_home_overlay`` mirrors the SAME tree into the overlay
        upper-home for relaxed ``--home``/``--overlay`` specs. No-op otherwise;
        ``setup_settings_json`` runs against the upper-home too so the merged
        settings reach the container ``$HOME`` under either home-delivery mode.

    Credentials are NOT staged here: the in-apptainer TUI receives them via the
    writable file-bind ``spec.claude.credentials_file`` (or the account/host
    dir-bind) emitted in ``build_run_argv`` — single source of truth.

    ``ensure_project_onboarding`` pre-seeds the per-workspace entry in
    ``$HOME/.claude.json`` so the TUI skips the workspace-trust wizard; written
    into BOTH the workspace-home and (when present) the overlay upper-home.

    Returns ``None`` for stub configs lacking the full AgentConfig surface
    (unit-test ``SimpleNamespace`` fixtures); the caller treats that as
    "skip materialise".
    """
    if not all(hasattr(config, a) for a in _REQUIRED_CONFIG_ATTRS):
        return None
    home_dir = state_dir_for_config(config) / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    setup_claude_md(config, str(home_dir))
    deploy_to_home(config, str(home_dir))
    log_effective_skills(config, home_dir)
    ensure_global_settings_json()
    setup_settings_json(config, str(home_dir), filename="settings.json")
    deploy_to_home_overlay(config)
    workdir = (
        getattr(config, "expanded_workdir", "")
        or getattr(config, "workdir", "")
        or "/tmp"
    )
    ensure_project_onboarding(workdir, home=home_dir)
    upper_home = resolve_overlay_upper_home(config)
    if upper_home is not None and upper_home.is_dir():
        ensure_project_onboarding(workdir, home=upper_home)
        setup_settings_json(config, str(upper_home), filename="settings.json")
    return home_dir
