"""Auth-staging for the ``runtime: tui`` adapter.

Stages the TWO files the interactive ``claude`` TUI checks on launch
into a TUI agent's materialised ``$HOME``:

  - ``<home>/.claude/.credentials.json`` — live OAuth token. The TUI
    reads this on launch via its bundled ``$HOME/.claude/`` path; same
    file ``claude -p`` reads under ``CLAUDE_CONFIG_DIR``.
  - ``<home>/.claude.json`` — onboarding state with the ``oauthAccount``
    block. The interactive TUI reads this to decide whether to skip
    the login picker. ``claude -p`` does NOT touch this file, which is
    why the SDK runtime path worked while the TUI hedge stalled on the
    login picker before this staging existed (lead a2a
    ``020bd692bfb94c45b28181d58ae9b0e6``, 2026-06-14).

Sources are environment-configurable so a non-default container layout
can point at an alternative credential / onboarding location without
a code change. Defaults match the apptainer-runtime convention:

  ``/tmp/sac-claude/.credentials.json`` — the apptainer auth bind point
  (see ``_apptainer_auth.auth_argv``: the host's ``~/.claude/`` is
  dir-bound at ``/tmp/sac-claude``, so this file is the host live
  ``.credentials.json`` from inside the SAC process).

  ``${HOME}/.claude.json`` — the SAC process's own onboarding state
  (whatever ``claude /login`` wrote to the host user's ``$HOME``).
  When SAC runs inside apptainer this is the agent-user ``$HOME`` the
  operator pre-staged the file under.

Fail-loud doctrine: when either source is missing the staging raises
:class:`TuiAuthStageError` with the exact path AND the remedy
("set ``SAC_TUI_AUTH_CREDENTIALS_SRC`` or stage the file"). No silent
fallback to a hardcoded template — a TUI agent that authenticates
with the wrong identity is worse than one that fails to start.

Symlink semantics: source paths are followed (``cp -Lf``) so a
sym-linked source resolves to its real target before copy. The
destination is always a regular file with ``0600`` perms on the
credentials.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover -- import-time guard only
    from ..config import AgentConfig

__all__ = [
    "TuiAuthStageError",
    "StagedAuth",
    "stage_tui_auth",
    "CREDENTIALS_SRC_ENV",
    "CLAUDE_JSON_SRC_ENV",
    "SETTINGS_JSON_SRC_ENV",
    "DEFAULT_CREDENTIALS_SRC",
    "DEFAULT_CREDENTIALS_FALLBACK_CHAIN",
    "DEFAULT_CLAUDE_JSON_SRC_BASENAME",
    "DEFAULT_SETTINGS_FALLBACK",
]


# Environment variable names — public so the operator (and the
# documentation generator) can refer to them by symbol.
CREDENTIALS_SRC_ENV = "SAC_TUI_AUTH_CREDENTIALS_SRC"
CLAUDE_JSON_SRC_ENV = "SAC_TUI_AUTH_CLAUDE_JSON_SRC"
SETTINGS_JSON_SRC_ENV = "SAC_TUI_AUTH_SETTINGS_JSON_SRC"
# Colon-separated override for the credential fallback chain
# (:func:`_resolve_credentials_src`). Primarily used by the unit
# suite to neutralise the operator's real ``/tmp/sac-claude``
# bind without monkeypatch; also a deployment escape for
# air-gapped hosts that mount creds at a non-default path.
CREDENTIALS_FALLBACK_CHAIN_ENV = "SAC_TUI_AUTH_CREDENTIALS_FALLBACK_CHAIN"

# Default credential source: the apptainer-runtime convention. See
# ``_apptainer_auth.auth_argv`` — the host's ``~/.claude/`` dir is
# dir-bound at ``/tmp/sac-claude``, so this path resolves to the host
# live ``.credentials.json`` from inside an apptainer'd SAC process.
DEFAULT_CREDENTIALS_SRC = "/tmp/sac-claude/.credentials.json"

# Ordered fallback chain consulted when neither the env override nor
# a pinned account snapshot resolves a credentials file. Each entry
# is tried in turn; the first existing path wins. Lead a2a
# ``1781e82a23204fd9b821883d565f5a0d`` (2026-06-14): host starts
# (no apptainer bind) found the default ``/tmp/sac-claude`` source
# missing — for the host case the resolver must reach into the
# host's live ``~/.claude/.credentials.json``.
DEFAULT_CREDENTIALS_FALLBACK_CHAIN: tuple[str, ...] = (
    DEFAULT_CREDENTIALS_SRC,
    "~/.claude/.credentials.json",
)

# Default onboarding-state source basename, joined under ``$HOME`` at
# resolve time. The host user's ``~/.claude.json`` is what ``claude
# /login`` wrote and is the natural per-identity source.
DEFAULT_CLAUDE_JSON_SRC_BASENAME = ".claude.json"

# Minimal default written to ``<home>/.claude/settings.json`` when
# neither ``SAC_TUI_AUTH_SETTINGS_JSON_SRC`` is set NOR an existing
# ``settings.json`` is present (i.e. agent's to_home/ didn't ship
# one). The theme field defeats the first-launch theme picker the
# bundled claude TUI otherwise renders on every fresh HOME — the
# operator-facing dogfood (2026-06-14) hit this immediately after
# auth-skip and would otherwise wedge ``send_turn`` against the
# modal picker. The ``"dark"`` choice mirrors the operator's host
# preference; a per-agent override lands in to_home/ as the
# higher-priority overlay.
DEFAULT_SETTINGS_FALLBACK: dict = {"theme": "dark"}


class TuiAuthStageError(RuntimeError):
    """Raised when a required TUI-auth source is missing.

    Carries the exact source path plus the env var that can override
    it so the operator's remedy is unambiguous.
    """


@dataclass(frozen=True)
class StagedAuth:
    """Paths landed by :func:`stage_tui_auth` (one per source).

    Returned so the caller (the TUI runtime + e2e probes) can log the
    exact destination paths without re-deriving them.
    """

    credentials_dst: Path
    claude_json_dst: Path
    settings_json_dst: Path


def _resolve_credentials_src(config: "AgentConfig | None" = None) -> Path:
    """Pick the credentials source path for the TUI agent's HOME.

    Priority (highest wins):

      1. ``$SAC_TUI_AUTH_CREDENTIALS_SRC`` — explicit operator override.
      2. ``spec.claude.account`` snapshot — when the spec pins an
         account, the canonical creds live under
         ``~/.scitex/agent-container/accounts/<acct>/.credentials.json``.
         Lead a2a ``1781e82a`` (2026-06-14): without this, every
         pinned-account TUI agent on a host failed because the
         default container-bind path ``/tmp/sac-claude`` does not
         exist outside apptainer.
      3. :data:`DEFAULT_CREDENTIALS_FALLBACK_CHAIN` — tries the
         container bind first (works inside apptainer'd SAC), then
         the host live ``~/.claude/.credentials.json`` (works on
         bare-host SAC). First entry that exists on disk wins; if
         none exist the caller raises :class:`TuiAuthStageError`
         naming every path it tried.

    The fallback chain is NOT a silent fallback in the doctrine
    sense — each path is exhausted explicitly and the fail-loud
    error lists every one. Operator clarification (1781e82a):
    "Make the host-start path resolve creds from the spec's
    account store automatically, not just the container bind."
    """
    env_override = os.environ.get(CREDENTIALS_SRC_ENV, "")
    if env_override:
        return Path(env_override).expanduser()

    snapshot = _resolve_pinned_account_credentials(config)
    if snapshot is not None:
        return snapshot

    chain = _effective_credentials_chain()
    for candidate in chain:
        path = Path(candidate).expanduser()
        if path.exists():
            return path
    # No path exists — return the FIRST candidate so the caller's
    # error message points at the canonical default. The full chain
    # is listed in the error itself, separately.
    return Path(chain[0]).expanduser()


def _effective_credentials_chain() -> tuple[str, ...]:
    """Resolve the credentials fallback chain.

    Production callers get :data:`DEFAULT_CREDENTIALS_FALLBACK_CHAIN`
    unchanged. The colon-separated env override
    (:data:`CREDENTIALS_FALLBACK_CHAIN_ENV`) lets the unit suite
    neutralise the operator's real ``/tmp/sac-claude`` bind without
    monkeypatch — and also serves an air-gapped deployment that
    binds creds at a custom path.
    """
    raw = os.environ.get(CREDENTIALS_FALLBACK_CHAIN_ENV, "")
    if raw:
        return tuple(p for p in raw.split(":") if p)
    return DEFAULT_CREDENTIALS_FALLBACK_CHAIN


def _resolve_pinned_account_credentials(
    config: "AgentConfig | None",
) -> Path | None:
    """Return the per-account snapshot path when ``spec.claude.account``
    is set AND the snapshot exists. Returns ``None`` for unpinned
    specs or when the snapshot file is absent (the caller falls
    through to the chain; a hard pinned-account preflight lives in
    the SDK runtime under :mod:`_apptainer_creds`).

    Defensive against stub AgentConfig surfaces used in unit tests —
    a missing ``claude.account`` attribute degrades silently to
    "unpinned" without raising.
    """
    acct = ""
    try:
        acct = getattr(getattr(config, "claude", None), "account", "") or ""
    except AttributeError:
        acct = ""
    if not acct:
        return None
    snapshot = (
        Path.home()
        / ".scitex"
        / "agent-container"
        / "accounts"
        / acct
        / ".credentials.json"
    )
    return snapshot if snapshot.is_file() else None


def _resolve_claude_json_src() -> Path:
    raw = os.environ.get(CLAUDE_JSON_SRC_ENV, "")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_CLAUDE_JSON_SRC_BASENAME


def _resolve_settings_json_src() -> Path | None:
    """Resolve the ``.claude/settings.json`` source for the TUI.

    Resolution order:
      1. ``$SAC_TUI_AUTH_SETTINGS_JSON_SRC`` if set — overrides.
      2. ``${HOME}/.claude/settings.json`` — the host's setting if
         present.
      3. ``None`` — caller writes :data:`DEFAULT_SETTINGS_FALLBACK`.

    Unlike credentials / .claude.json, settings.json is OPTIONAL —
    its absence does not fail the start. The TUI works without it,
    but on a fresh HOME shows a theme picker that wedges
    ``send_turn``. The minimal fallback (`{"theme": "dark"}`)
    defeats the picker without coupling to any operator state.
    """
    raw = os.environ.get(SETTINGS_JSON_SRC_ENV, "")
    if raw:
        return Path(raw).expanduser()
    host_settings = Path.home() / ".claude" / "settings.json"
    return host_settings if host_settings.is_file() else None


def _follow_and_copy(src: Path, dst: Path) -> None:
    """``cp -Lf`` semantics: follow source symlinks, overwrite dst.

    ``shutil.copyfile`` already follows source symlinks by default; we
    resolve first so the error message in :class:`FileNotFoundError`
    names the real underlying path when the symlink dangles.
    """
    real_src = src.resolve(strict=True)
    dst.parent.mkdir(parents=True, exist_ok=True)
    # Remove any existing dst (regular file or symlink) so we never
    # write THROUGH a symlink the previous run may have left behind.
    if dst.is_symlink() or dst.exists():
        dst.unlink()
    shutil.copyfile(real_src, dst)


def stage_tui_auth(
    home_dir: Path,
    *,
    config: "AgentConfig | None" = None,
) -> StagedAuth:
    """Stage the credentials + ``.claude.json`` + ``.claude/settings.json``
    into ``home_dir``.

    Idempotent: re-running overwrites the destination files. The
    credentials destination is chmod 0600 after copy (matches the
    permission the bundled ``claude`` enforces on its own writes).

    Raises :class:`TuiAuthStageError` if either the credentials or
    .claude.json source path is absent — these are the fail-loud
    gates (no silent fallback to a different identity). The
    ``settings.json`` slot is OPTIONAL: if an existing settings.json
    is already present in ``<home>/.claude/`` (typically materialised
    by the agent's own ``to_home/`` overlay), we leave it alone.
    Otherwise we resolve a source via
    ``$SAC_TUI_AUTH_SETTINGS_JSON_SRC`` or ``${HOME}/.claude/settings.json``
    and copy it. If no source is available either, we write a
    minimal :data:`DEFAULT_SETTINGS_FALLBACK` — the theme field
    defeats the first-launch theme picker that would otherwise
    wedge ``send_turn`` against a modal claude TUI overlay.

    The ``config`` kwarg lets the resolver consult ``spec.claude.account``
    so a pinned-account TUI agent on a host (where the apptainer
    bind at ``/tmp/sac-claude`` doesn't exist) lands on its per-account
    snapshot instead of failing the start. See
    :func:`_resolve_credentials_src` for the full priority order.
    """
    home_dir.mkdir(parents=True, exist_ok=True)

    creds_src = _resolve_credentials_src(config)
    if not creds_src.exists():
        chain_listing = ", ".join(_effective_credentials_chain())
        raise TuiAuthStageError(
            f"TUI auth: credentials source missing at {creds_src}. "
            f"Tried (in priority order): ${CREDENTIALS_SRC_ENV} → "
            f"pinned-account snapshot (spec.claude.account) → "
            f"fallback chain [{chain_listing}]. Set "
            f"{CREDENTIALS_SRC_ENV} to an existing path, pin a saved "
            "account via spec.claude.account (snapshot under "
            "~/.scitex/agent-container/accounts/<acct>/), or stage "
            "the file at the canonical default."
        )

    claude_json_src = _resolve_claude_json_src()
    if not claude_json_src.exists():
        raise TuiAuthStageError(
            f"TUI auth: .claude.json source missing at {claude_json_src}. "
            f"Set {CLAUDE_JSON_SRC_ENV} to an existing path or stage the "
            "file (default expects ${HOME}/.claude.json — typically "
            "materialised by `claude /login` on the host)."
        )

    claude_dir = home_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    creds_dst = claude_dir / ".credentials.json"
    _follow_and_copy(creds_src, creds_dst)
    creds_dst.chmod(0o600)

    claude_json_dst = home_dir / ".claude.json"
    _follow_and_copy(claude_json_src, claude_json_dst)

    settings_json_dst = claude_dir / "settings.json"
    if not settings_json_dst.exists():
        settings_src = _resolve_settings_json_src()
        if settings_src is not None and settings_src.is_file():
            _follow_and_copy(settings_src, settings_json_dst)
        else:
            # Minimal fallback — defeats the first-launch theme picker
            # without coupling to operator-specific config. The to_home/
            # overlay is the higher-priority layer; this only fires when
            # nothing else wrote settings.json.
            settings_json_dst.write_text(
                json.dumps(DEFAULT_SETTINGS_FALLBACK), encoding="utf-8"
            )

    return StagedAuth(
        credentials_dst=creds_dst,
        claude_json_dst=claude_json_dst,
        settings_json_dst=settings_json_dst,
    )
