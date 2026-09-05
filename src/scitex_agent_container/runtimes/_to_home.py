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
from pathlib import Path

from ..config import AgentConfig
from ._cct_token_pool import ensure_cct_bot_token, prune_tokenless_telegrammer_mcp
from ._envrc import fold_envrc_cascade_into_env, fold_envrc_into_env
from ._github_token import ensure_github_token
from ._hook_origin_manifest import write_hook_manifest
from ._host_commands import (
    deploy_host_claude_commands,
    host_claude_commands_dir,
    snapshot_drift,
)
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
from ._to_home_resolve import (
    _spec_dir,
    _user_baseline_to_home_dir,
    resolve_baseline_to_home_dir,
    resolve_to_home_dir,
    settings_layer_dirs,
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
# (sac / scitex-cards / claude-code-telegrammer); a per-agent ``.mcp.json`` must
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

# Path-resolution helpers (``resolve_to_home_dir`` /
# ``resolve_baseline_to_home_dir`` / ``_user_baseline_to_home_dir`` /
# ``settings_layer_dirs`` / ``_spec_dir``) + their env/dir-name constants live
# in :mod:`._to_home_resolve` and are imported + re-exported above, so the
# legacy ``from ...runtimes._to_home import resolve_baseline_to_home_dir``
# contract keeps resolving. This module owns the materialize/deploy side only.


# --- public API ------------------------------------------------------------


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
    # Run-scoped, SHARED across both layers: marker-protected files (CLAUDE.md
    # / state.md) compose onto the earlier layer instead of replacing it. The
    # baseline pass still resets the section, so nothing grows across runs.
    composed_dsts: set[Path] = set()
    if baseline is not None:
        _walk_and_apply(
            baseline, baseline, workspace_home, config=None, composed_dsts=composed_dsts
        )
    if root.is_dir():
        _walk_and_apply(
            root, root, workspace_home, config=None, composed_dsts=composed_dsts
        )
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
    # Run-scoped and SHARED across both layers — see materialize_to_home.
    composed_dsts: set[Path] = set()
    if baseline is not None:
        _walk_and_apply(
            baseline, baseline, dest, config=config, composed_dsts=composed_dsts
        )
    if root is not None:
        _walk_and_apply(root, root, dest, config=config, composed_dsts=composed_dsts)
    # Launch-time snapshot drift (card sac-launch-compares-the-copied-
    # commands-snapshot-to-the-dotfiles-ref-and-logs-divergence-20260905):
    # the commands the agent will read from the runtime home may differ
    # from the operator's live host (dotfiles) commands of the same name
    # — the cop copies once at launch and nothing refreshes until a
    # restart. Name each divergent file in the start log, through the
    # log the launcher already uses; print NOTHING when there is no
    # drift. No re-copy into a running session and no periodic check:
    # a live rewrite under a session that already read the file is
    # worse than a stale one — the honest fix is "restart to pick up",
    # said out loud.
    for _drift_line in snapshot_drift(_command_source_pairs(dest)):
        logger.warning("to_home: %s", _drift_line)

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
    # DETERMINISTIC CCT BOT-TOKEN INJECTION (card sac-fleet-ux-misc-2026-06-24,
    # last item): when the spec requests server:claude-code-telegrammer and the
    # cascade above did NOT provide CCT_BOT_TOKEN (no per-project .envrc), sac
    # resolves the agent/project -> CCT_BOT_TOKEN_<SLOT> from the fleet pool
    # (launching env + SAC_SECRETS_ENVRC secret files) and appends it to
    # dest/.env itself — per-agent identity must never depend on .envrc
    # goodwill (SCITEX_TODO_AGENT incident doctrine). Missing token => LOUD
    # scitex-logging ERROR naming the pool path + fix; never silent, never
    # fatal, token value never logged. Runs AFTER the fold so an explicit
    # .envrc mapping stays authoritative.
    ensure_cct_bot_token(config, dest)
    # ...and the counterpart: when NO token resolved above, drop the
    # claude-code-telegrammer entry from the materialised .mcp.json instead of
    # shipping one that starts, finds an empty token and fails on every boot.
    # MUST run after ensure_cct_bot_token — that call is what populates the
    # token this reads. See prune_tokenless_telegrammer_mcp (card
    # sac-omit-telegram-mcp-when-no-cct-bot-token-20260702). `config` is passed
    # so the prune can tell a DESIGNED bot-less agent (INFO) from one whose
    # spec DECLARES a CCT_BOT_TOKEN_SLOT that does not resolve (ERROR — the
    # removed entry leaves it mute AND deaf; card
    # sac-cct-prune-hides-misconfigured-telegram-agent-20260810).
    prune_tokenless_telegrammer_mcp(dest, config=config)
    # GITHUB_TOKEN, same pool and the same ordering rationale: the fleet
    # secrets live in ~/.bash.d/secrets, which only a LOGIN shell sources,
    # and sac starts containers without one — so the token was present and
    # correct on the host and simply never crossed the boundary. Measured
    # 2026-08-09: `gh` inside every container reported "not logged into any
    # GitHub hosts", three agents finished tested work, and NOT ONE could
    # open a pull request. Unlike the CCT rail this is not gated on a spec
    # opt-in: any agent that can push can need to open a PR, and an opt-in
    # would reproduce the failure (you find out at PR time). Missing token
    # => LOUD warning naming `gh pr create`; never fatal, value never logged.
    ensure_github_token(config, dest)
    # settings.json CASCADE (same precedence order as .envrc): deep-merge each
    # layer's .claude/settings.json into dest, raising on a cross-layer scalar
    # conflict (ADR-0018). The walk SKIPS settings.json so this is the single
    # writer. setup_settings_json later folds SAC's managed keys on top.
    settings_provenance = deploy_settings_cascade(dest, settings_layer_dirs(config))
    # ...then record WHICH layer armed each hook, to runtime (not to the home
    # we just wrote). The deployed settings.json is the flattened result, so it
    # cannot answer "where is this hook coming from?" — the origin only exists
    # here, while the cascade is still un-flattened. Best-effort by design: an
    # observability file must never be the reason a deploy fails.
    write_hook_manifest(getattr(config, "name", "") or "unknown", settings_provenance)
    # HOST DEEP-MERGE (developer agents only). For a FULL-DEVELOPER agent
    # (metadata.labels.group==developer, or group-unset + a developer role),
    # overlay the host operator's ~/.claude/{commands,skills,hooks} as per-file
    # ABSOLUTE symlinks ON TOP of the agent layers just materialized — union,
    # agent layer wins, host-session hooks deny-listed. Runs LAST so the walk's
    # symlink-deref has already happened (our links are kept as symlinks) and
    # the agent-layer real files exist for the agent-layer-wins check. A
    # capsule/solitary agent is a no-op (no host bleed). The boot self-check
    # re-materializes from scratch and fails loud on residual drift — never
    # serves a stale/partial host view. See :mod:`_host_merge`.
    _apply_host_merge_with_drift_guard(config, dest)


def _apply_host_merge_with_drift_guard(config: AgentConfig, dest: Path) -> None:
    """Materialize the host deep-merge then assert it matches host+agent layers.

    Boot-time fail-loud (operator requirement): :func:`apply_host_merge`
    re-derives every host-merge symlink from scratch (self-healing — a host
    file added/removed since last start is reflected). Then
    :func:`verify_host_merge` recomputes the expected set and, if ANYTHING is
    still off (e.g. a host file vanished mid-deploy, leaving a dangling link),
    we raise rather than launch on a partial view. No silent fallback.
    """
    from ._host_merge import HostMergeDriftError, apply_host_merge, verify_host_merge

    apply_host_merge(config, dest)
    findings = verify_host_merge(config, dest)
    if findings:
        bullet = "\n  - ".join(findings)
        raise HostMergeDriftError(
            f"host deep-merge still drifted after re-materialize for agent "
            f"{config.name!r} at {dest}/.claude:\n  - {bullet}"
        )


def _command_source_pairs(dest: Path) -> list[tuple[Path, Path]]:
    """(snapshot, source) pairs for the launch-time snapshot-drift check.

    Every ``*.md`` under the materialized ``.claude/commands/`` that has a
    same-name file in the host ``~/.claude/commands/`` (the operator's live
    dotfiles ref — the ``deploy_host_claude_commands`` source) yields one
    pair. A command with no same-name host counterpart was copied from a
    to_home layer and is byte-identical to that fresh copy, so it has no
    host ref to name against and yields no pair.
    """
    cmd = dest / ".claude" / "commands"
    host = host_claude_commands_dir()
    if host is None or not cmd.is_dir():
        return []
    return [
        (p, host / p.name)
        for p in sorted(cmd.iterdir())
        if p.suffix == ".md" and p.is_file() and (host / p.name).is_file()
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
    composed_dsts: set[Path] | None = None,
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
            _walk_and_apply(
                src_root, child, dst, config=config, composed_dsts=composed_dsts
            )
            continue

        # Regular file.
        # Cascade-deployed files are assembled post-walk by a deep-merge over
        # ALL layers (ADR-0018); skip the per-layer plain copy that would
        # clobber a lower layer.
        if child.name in _CASCADE_DEPLOYED_BASENAMES:
            continue
        if child.name in _MARKER_PROTECTED_BASENAMES:
            _deploy_marker_protected(
                child, dst, config=config, rel=rel, composed_dsts=composed_dsts
            )
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
