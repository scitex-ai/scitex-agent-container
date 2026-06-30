"""Materialize ``<spec_dir>/to_home/`` into the agent's workspace ``$HOME``.

The single canonical layout for materializing files into an agent's
``$HOME`` (see ADR-0006). The layout makes the rule explicit:

    agents/<name>/
    ├── spec.yaml          (spec.to_home: ./to_home — default)
    └── to_home/           contents mirror $HOME 1:1
        ├── .claude/
        │   ├── CLAUDE.md          → $HOME/.claude/CLAUDE.md     (marker-protected)
        │   ├── settings.json      → $HOME/.claude/settings.json (USER scope —
        │   │                        the interactive TUI reads hooks here; a
        │   │                        legacy settings.local.json is folded into
        │   │                        it by settings_json.setup_settings_json)
        │   ├── hooks/
        │   └── skills/
        ├── .mcp.json              → $HOME/.mcp.json             (full overwrite)
        ├── .env                   → $HOME/.env                  (overwrite, chmod 0600)
        ├── state.md               → $HOME/state.md              (marker-protected)
        └── secrets/               → $HOME/secrets/              (mirror, perms preserved)

Inside the container ``$HOME`` is bind-mounted from
``runtime/<name>/home/`` on the host — i.e. ``workspace_home`` in this
module's API.

Semantics per entry (see :func:`materialize_to_home`):

  - **CLAUDE.md** / **state.md** — marker-protected merge. Source is
    wrapped between Start/End markers; any user tail after the End
    marker is preserved. Malformed existing markers hard-abort the
    deploy with :class:`WorkspaceCLAUDEMarkerError`.
  - **.env** — full overwrite; chmod 0600 after write.
  - **Other regular files** — full overwrite (``shutil.copy2``).
  - **Directories** — recursed; structure preserved.
  - **Symlinks** — preserved as symlinks (target not resolved). Both
    absolute and relative targets pass through verbatim.

No fragmented "leaf-vs-mirror" distinction — every path under
``to_home/`` lands at the same relative path under ``workspace_home``.

Missing ``to_home/`` dir → silent no-op (specs without one just don't
get materialization).

Baseline layer (shared/common to_home): two-pass overlay applies the
shared baseline first (resolved via :func:`resolve_baseline_to_home_dir`
— ``$SAC_TO_HOME_BASELINE`` override or ``<agents_dir>/_shared/to_home/``
sibling), then the per-agent ``to_home/`` on top. Per-agent files win
on conflict. Absent baseline → behaves as before, fully backward
compatible.

Credential-leak guard (2026-06-15, lead-reported): a ``.credentials.json``
under ``to_home/`` aborts the deploy with
:class:`WorkspaceCredentialLeakError`. Credentials are operator-rotated
runtime state from the auth-stage rw bind, never static workspace
content — see ``_to_home_errors.py`` for context.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..config import AgentConfig
from ._envrc import fold_envrc_cascade_into_env, fold_envrc_into_env
from ._host_commands import deploy_host_claude_commands
from ._host_skills import deploy_host_skills
from ._symlink_resolve import DanglingToHomeSymlinkError, deref_copy_symlink
from ._to_home_deployers import (
    _clear_readonly_dst,
    _deploy_marker_protected,
    _deploy_mcp_merge,
    _deploy_plain_file,
    _deploy_tight_perm_file,
    _deploy_verbatim_secret,
    _read_and_interpolate,
)
from ._to_home_errors import (
    WorkspaceCLAUDEMarkerError,
    WorkspaceCredentialLeakError,
    WorkspaceMcpMergeError,
)
from ._to_home_settings import deploy_settings_cascade
from ._to_home_text import (
    END_MARKER,
    extract_user_tail,
    interpolate_env,
    interpolate_metadata,
    split_around_generated_section,
    validate_marker_invariants,
)

logger = logging.getLogger(__name__)

# Marker constants + text helpers re-exported for legacy import paths
# (e.g. tests doing ``from ...runtimes._to_home import END_MARKER``).
# Implementations live in :mod:`_to_home_text` per the 2026-06-15
# extraction (see GITIGNORED/REFACTORING.md when present).
_validate_marker_invariants = validate_marker_invariants
_extract_user_tail = extract_user_tail
_interpolate_env = interpolate_env
_interpolate_metadata = interpolate_metadata

# Per-entry deploy helpers live in :mod:`_to_home_deployers` (re-imported
# above). Re-bound here so legacy imports
# (``from ...runtimes._to_home import _deploy_plain_file``) keep resolving.
_END_MARKER = END_MARKER
_split_around_generated_section = split_around_generated_section


# Files that get marker-protected merge semantics (vs. full overwrite).
# Marker protection guards against silent data loss on a hand-edited file.
_MARKER_PROTECTED_BASENAMES = frozenset({"CLAUDE.md", "state.md"})

# Files DEEP-MERGED across the two-pass overlay (vs. full overwrite). The
# shared baseline ``_shared/to_home/.mcp.json`` carries the default MCP servers
# (sac / scitex-todo / claude-code-telegrammer); a per-agent ``.mcp.json`` must
# UNION its servers with that baseline, not replace it (which would silently
# drop the defaults). Same-name conflict → fail loud. See :func:`_deploy_mcp_merge`.
_MCP_MERGE_BASENAMES = frozenset({".mcp.json"})

# Files that get chmod 0600 after copy. ``.env`` only by default; the
# rest preserve source perms via ``shutil.copy2``.
_TIGHT_PERM_BASENAMES = frozenset({".env"})

# Files copied VERBATIM (no ${...} interpolation) + chmod 0600. ``.envrc`` is a
# shell script whose own ${VAR} / $(...) syntax must reach bash intact (sac
# interpolation would expand it at deploy time and corrupt the script); it is
# evaluated host-side post-deploy by :func:`_envrc.fold_envrc_into_env`.
_VERBATIM_SECRET_BASENAMES = frozenset({".envrc"})

# Files deployed by a POST-walk CASCADE deep-merge (ADR-0018) instead of the
# per-layer plain copy — so the two-pass walk must SKIP them (a plain overwrite
# would clobber a lower layer). ``settings.json`` is assembled from the user
# ``_shared`` → project ``_shared`` → per-agent layers via
# :func:`_to_home_settings.deploy_settings_cascade` (deep-merge; raise on a
# scalar conflict). ``settings.local.json`` is the legacy baseline name, still
# accepted as a layer source but never plain-copied.
_CASCADE_DEPLOYED_BASENAMES = frozenset({"settings.json", "settings.local.json"})

# Env var: explicit override for the shared/common baseline to_home dir.
# Absolute path. When unset we fall back to ``<agents_dir>/_shared/to_home``
# (or the legacy ``_base`` sibling).
_BASELINE_ENV_VAR = "SAC_TO_HOME_BASELINE"

# Names of the sibling dir (under the agents root) that holds the common
# baseline. Agents live at ``<agents_dir>/<name>/``, so the agents root
# is the spec dir's parent and the baseline is ``<parent>/_shared/to_home``.
# ``_shared`` is the current name; ``_base`` is retained as a
# backward-compat fallback for hosts/fleets not yet renamed (first match
# under the agents root wins, in declared order).
_BASELINE_DIR_NAMES = ("_shared", "_base")


# --- public API ------------------------------------------------------------


def resolve_to_home_dir(config: AgentConfig) -> Path | None:
    """Resolve ``spec.to_home`` to an absolute directory.

    Resolution order:
      1. Absolute path: use as-is.
      2. Relative path: resolve against the directory containing
         ``spec.yaml``.
      3. Empty: auto-discover ``./to_home`` next to ``spec.yaml``.

    Returns ``None`` if no directory can be resolved (legacy specs
    without a to_home/ dir simply skip materialization).
    """
    spec_dir = _spec_dir(config)
    raw = (getattr(config, "to_home", "") or "").strip()
    if not raw:
        if spec_dir is not None and (spec_dir / "to_home").is_dir():
            return spec_dir / "to_home"
        return None
    p = Path(raw).expanduser()
    if not p.is_absolute():
        if spec_dir is None:
            return None
        p = spec_dir / p
    return p if p.is_dir() else None


def resolve_baseline_to_home_dir(spec_dir: Path | None) -> Path | None:
    """Resolve the shared/common baseline ``to_home/`` directory.

    Resolution order:
      1. ``$SAC_TO_HOME_BASELINE`` (absolute dir) — explicit override.
      2. ``<agents_dir>/_shared/to_home`` — a sibling ``_shared`` dir
         under the agents root (``_base`` accepted as a backward-compat
         fallback). Agents live at ``<agents_dir>/<name>/``, so the
         agents root is ``spec_dir.parent``.

    Returns ``None`` when no baseline dir can be resolved (no baseline =
    current behavior; fully backward compatible).
    """
    override = (os.environ.get(_BASELINE_ENV_VAR, "") or "").strip()
    if override:
        p = Path(override).expanduser()
        return p if p.is_dir() else None
    if spec_dir is None:
        return None
    for name in _BASELINE_DIR_NAMES:
        p = spec_dir.parent / name / "to_home"
        if p.is_dir():
            return p
    return None


def _user_baseline_to_home_dir() -> Path | None:
    """The USER-level shared baseline ``to_home`` — applies to every agent
    regardless of where its spec lives: ``~/.scitex/agent-container/agents/
    {_shared,_base}/to_home`` (first match wins). Returns ``None`` when absent.

    Distinct from :func:`resolve_baseline_to_home_dir`, which resolves the
    baseline *relative to the spec's* agents root (project-local for a
    project-local spec). The ``.envrc`` cascade sources BOTH so a user-global
    default and a project ``_shared`` both apply, lowest precedence first.
    """
    base = Path("~/.scitex/agent-container/agents").expanduser()
    for name in _BASELINE_DIR_NAMES:
        p = base / name / "to_home"
        if p.is_dir():
            return p
    return None


def materialize_to_home(spec_dir: Path, workspace_home: Path) -> None:
    """Mirror ``<spec_dir>/to_home/`` into ``<workspace_home>/``.

    Two-pass overlay:

      1. The shared/common baseline ``to_home/``.
      2. ``<spec_dir>/to_home/`` on top — so per-agent files win on
         conflict.

    Walks each tree and applies the per-entry semantics described in
    the module docstring (symlinks are dereference-copied to real
    content). Idempotent — safe to call on every agent start. The
    runtime never auto-reads host state; the definition is the sole
    source of truth.

    No-op when neither the baseline nor ``<spec_dir>/to_home/`` exists.
    """
    baseline = resolve_baseline_to_home_dir(spec_dir)
    root = spec_dir / "to_home"
    if baseline is None and not root.is_dir():
        return
    # Credential-leak guard runs BEFORE any deploy (both layers).
    if baseline is not None:
        _scan_for_credential_leak(baseline)
    if root.is_dir():
        _scan_for_credential_leak(root)
    workspace_home.mkdir(parents=True, exist_ok=True)
    # Idempotency (see deploy_to_home): re-derive deep-merged files fresh so a
    # changed per-agent definition never false-conflicts with a stale copy.
    for _merge_name in _MCP_MERGE_BASENAMES:
        _stale = workspace_home / _merge_name
        if _stale.is_file():
            _stale.unlink()
    # Host ~/.claude/commands/*.md — the LOWEST baseline layer. Deploy FIRST so
    # a same-name shared-baseline / per-agent command overwrites it below.
    # Skip-if-missing (no host commands dir → no-op).
    deploy_host_claude_commands(workspace_home)
    # Curated host ~/.claude/skills/<name> (ywatanabe, scitex) — symlinked in.
    # No-clobber: a per-agent / bundled same-name skill is left untouched.
    deploy_host_skills(workspace_home)
    if baseline is not None:
        _walk_and_apply(baseline, baseline, workspace_home, config=None)
    if root.is_dir():
        _walk_and_apply(root, root, workspace_home, config=None)
    fold_envrc_into_env(workspace_home)
    # settings.json CASCADE (ADR-0018) — same deep-merge as deploy_to_home, so
    # the lower-level (spec_dir, workspace_home) entrypoint composes layers too
    # instead of clobbering. Includes the user-level _shared baseline.
    deploy_settings_cascade(
        workspace_home,
        [
            ("user-shared", _user_baseline_to_home_dir()),
            ("project-shared", baseline),
            ("per-agent", root if root.is_dir() else None),
        ],
    )


def deploy_to_home(config: AgentConfig, workspace_home: str) -> None:
    """``AgentConfig``-driven entrypoint for to_home materialization.

    Two-pass overlay:

      1. The shared/common baseline ``to_home/``.
      2. The per-agent ``to_home/`` on top — so per-agent files win on
         conflict.

    Resolves the per-agent directory via :func:`resolve_to_home_dir`
    (honours ``spec.to_home`` overrides) and the baseline via
    :func:`resolve_baseline_to_home_dir`, then applies metadata-aware
    interpolation (${metadata.name}, ${metadata.labels.*}, ${ENV_VAR})
    to text files. Symlinks are dereference-copied to real content; the
    runtime never auto-reads host state. No-op when neither the baseline
    nor the per-agent to_home resolves.
    """
    root = resolve_to_home_dir(config)
    baseline = resolve_baseline_to_home_dir(_spec_dir(config))
    if root is None and baseline is None:
        return
    # Credential-leak guard runs BEFORE any deploy (both layers).
    if baseline is not None:
        _scan_for_credential_leak(baseline)
    if root is not None:
        _scan_for_credential_leak(root)
    dest = Path(workspace_home)
    dest.mkdir(parents=True, exist_ok=True)
    # Idempotency: re-derive the deep-merged .mcp.json FRESH each deploy. Drop
    # any prior-deploy copy first so a CHANGED per-agent definition (e.g. an
    # updated telegrammer command/path) re-deploys cleanly, instead of
    # false-conflicting (McpMergeConflict) against last deploy's stale result
    # in _deploy_mcp_merge. No data loss — the file is regenerated below from
    # baseline ∪ per-agent.
    for _merge_name in _MCP_MERGE_BASENAMES:
        _stale = dest / _merge_name
        if _stale.is_file():
            _stale.unlink()
    # Host ~/.claude/commands/*.md — the LOWEST baseline layer. Deploy FIRST so
    # a same-name shared-baseline / per-agent command overwrites it below.
    # Skip-if-missing (no host commands dir → no-op).
    deploy_host_claude_commands(dest)
    # Curated host ~/.claude/skills/<name> (ywatanabe, scitex) — symlinked in.
    # No-clobber: a per-agent / bundled same-name skill is left untouched.
    deploy_host_skills(dest)
    if baseline is not None:
        _walk_and_apply(baseline, baseline, dest, config=config)
    if root is not None:
        _walk_and_apply(root, root, dest, config=config)
    # .envrc CASCADE (lowest → highest precedence): user-level shared baseline
    # → the spec's _shared baseline → the agent's workdir (the project's OWN
    # .envrc, e.g. ~/proj/<project>/.envrc) → the per-agent to_home. Each
    # layer's .envrc overrides the previous; the net is folded host-side into
    # dest/.env so build_run_argv's --env-file injects the resolved per-project
    # identity (e.g. this project's Telegram bot via CCT_BOT_TOKEN). No-op when
    # no layer ships a .envrc.
    workdir = (getattr(config, "workdir", "") or "").strip()
    workdir_dir = Path(workdir).expanduser() if workdir else None
    user_shared = _user_baseline_to_home_dir()
    envrc_cascade = [
        (user_shared / ".envrc") if user_shared is not None else None,
        (baseline / ".envrc") if baseline is not None else None,
        (workdir_dir / ".envrc") if workdir_dir is not None else None,
        dest / ".envrc",
    ]
    fold_envrc_cascade_into_env(dest, envrc_cascade)
    # settings.json CASCADE (same precedence order as .envrc): deep-merge each
    # layer's .claude/settings.json into dest, raising on a cross-layer scalar
    # conflict (ADR-0018). The walk SKIPS settings.json so this is the single
    # writer. setup_settings_json later folds SAC's managed keys on top.
    deploy_settings_cascade(dest, settings_layer_dirs(config))


def settings_layer_dirs(config: AgentConfig) -> "list[tuple[str, Path | None]]":
    """The ordered settings.json cascade layers (lowest precedence first).

    ``(name, dir)`` pairs for the user-level ``_shared`` baseline, the spec's
    ``_shared`` baseline, and the per-agent ``to_home`` — the inputs to
    :func:`_to_home_settings.deploy_settings_cascade` /
    :func:`_to_home_settings.settings_cascade_provenance`. Shared by
    ``deploy_to_home`` and ``sac agents explain`` so both resolve identically.
    """
    return [
        ("user-shared", _user_baseline_to_home_dir()),
        ("project-shared", resolve_baseline_to_home_dir(_spec_dir(config))),
        ("per-agent", resolve_to_home_dir(config)),
    ]


# --- traversal -------------------------------------------------------------


def _scan_for_credential_leak(src_root: Path) -> None:
    """Raise :class:`WorkspaceCredentialLeakError` on first
    ``.credentials.json`` found at any depth under ``src_root``.

    Runs BEFORE any deploy so a rejected tree cannot land partial
    sibling content next to a leaked credential. See the error class
    docstring for the lead-reported incident this guards against.
    """
    if not src_root.is_dir():
        return
    for leak in src_root.rglob(".credentials.json"):
        rel = leak.relative_to(src_root)
        raise WorkspaceCredentialLeakError(
            f"to_home/ contains a .credentials.json at {rel} — refused. "
            "Credentials must come from the auth-stage rw bind, not "
            f"a static workspace copy. Remove {leak} and rely on "
            "spec.claude.account or spec.claude.credentials_file."
        )


def _walk_and_apply(
    src_root: Path,
    src_dir: Path,
    dst_dir: Path,
    *,
    config: AgentConfig | None,
) -> None:
    """Recursively replicate ``src_dir`` under ``dst_dir``.

    ``src_root`` is the original ``to_home/`` root, retained so log
    messages can report a path relative to the spec. ``config`` is
    optional: when present, text-file interpolation (``${metadata.*}``
    and ``${ENV_VAR}``) runs over CLAUDE.md / state.md / .env / .mcp.json.

    The caller (``materialize_to_home`` / ``deploy_to_home``) runs
    :func:`_scan_for_credential_leak` first so a leaked
    ``.credentials.json`` aborts the deploy before any sibling
    content lands.
    """
    for child in sorted(src_dir.iterdir()):
        rel = child.relative_to(src_root)
        dst = dst_dir / child.name

        # Symlinks first — must not follow into ``is_dir()`` / ``is_file()``
        # decisions, because is_dir(follow=True) would route a symlink to
        # a directory through the mirror branch. The link is resolved to
        # its real target content (dereference-copy); a dangling target
        # hard-aborts via DanglingToHomeSymlinkError.
        if child.is_symlink():
            deref_copy_symlink(child, dst)
            continue

        if child.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
            _walk_and_apply(src_root, child, dst, config=config)
            continue

        # Regular file.
        # Cascade-deployed files are assembled post-walk by a deep-merge over
        # ALL layers (ADR-0018); skip the per-layer plain copy that would
        # clobber a lower layer.
        if child.name in _CASCADE_DEPLOYED_BASENAMES:
            continue
        if child.name in _MARKER_PROTECTED_BASENAMES:
            _deploy_marker_protected(child, dst, config=config, rel=rel)
        elif child.name in _MCP_MERGE_BASENAMES:
            _deploy_mcp_merge(child, dst, config=config, rel=rel)
        elif child.name in _TIGHT_PERM_BASENAMES:
            _deploy_tight_perm_file(child, dst, config=config, rel=rel)
        elif child.name in _VERBATIM_SECRET_BASENAMES:
            _deploy_verbatim_secret(child, dst, rel=rel)
        else:
            _deploy_plain_file(child, dst, config=config, rel=rel)


# --- per-entry deploy helpers ----------------------------------------------
# Extracted to :mod:`._to_home_deployers` (the file outgrew the line cap);
# re-imported above and re-exported below so legacy import paths still resolve.
# Symlinks are dereference-copied to real content via
# :func:`_symlink_resolve.deref_copy_symlink`, called directly by the traversal.


# --- internal helpers ------------------------------------------------------


def _spec_dir(config: AgentConfig) -> Path | None:
    if not getattr(config, "config_path", ""):
        return None
    return Path(config.config_path).parent


__all__ = [
    "DanglingToHomeSymlinkError",
    "WorkspaceCLAUDEMarkerError",
    "WorkspaceMcpMergeError",
    # Per-entry deploy helpers re-exported from _to_home_deployers for the
    # legacy ``from ...runtimes._to_home import _deploy_*`` import contract.
    "_clear_readonly_dst",
    "_deploy_marker_protected",
    "_deploy_mcp_merge",
    "_deploy_plain_file",
    "_deploy_tight_perm_file",
    "_deploy_verbatim_secret",
    "_read_and_interpolate",
    "deploy_to_home",
    "materialize_to_home",
    "resolve_baseline_to_home_dir",
    "resolve_to_home_dir",
]
