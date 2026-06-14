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

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "TuiAuthStageError",
    "StagedAuth",
    "stage_tui_auth",
    "CREDENTIALS_SRC_ENV",
    "CLAUDE_JSON_SRC_ENV",
    "DEFAULT_CREDENTIALS_SRC",
    "DEFAULT_CLAUDE_JSON_SRC_BASENAME",
]


# Environment variable names — public so the operator (and the
# documentation generator) can refer to them by symbol.
CREDENTIALS_SRC_ENV = "SAC_TUI_AUTH_CREDENTIALS_SRC"
CLAUDE_JSON_SRC_ENV = "SAC_TUI_AUTH_CLAUDE_JSON_SRC"

# Default credential source: the apptainer-runtime convention. See
# ``_apptainer_auth.auth_argv`` — the host's ``~/.claude/`` dir is
# dir-bound at ``/tmp/sac-claude``, so this path resolves to the host
# live ``.credentials.json`` from inside an apptainer'd SAC process.
DEFAULT_CREDENTIALS_SRC = "/tmp/sac-claude/.credentials.json"

# Default onboarding-state source basename, joined under ``$HOME`` at
# resolve time. The host user's ``~/.claude.json`` is what ``claude
# /login`` wrote and is the natural per-identity source.
DEFAULT_CLAUDE_JSON_SRC_BASENAME = ".claude.json"


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


def _resolve_credentials_src() -> Path:
    raw = os.environ.get(CREDENTIALS_SRC_ENV, "") or DEFAULT_CREDENTIALS_SRC
    return Path(raw).expanduser()


def _resolve_claude_json_src() -> Path:
    raw = os.environ.get(CLAUDE_JSON_SRC_ENV, "")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / DEFAULT_CLAUDE_JSON_SRC_BASENAME


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


def stage_tui_auth(home_dir: Path) -> StagedAuth:
    """Stage the credentials + ``.claude.json`` into ``home_dir``.

    Idempotent: re-running overwrites the destination files. The
    credentials destination is chmod 0600 after copy (matches the
    permission the bundled ``claude`` enforces on its own writes).

    Raises :class:`TuiAuthStageError` if either source path is absent
    on the filesystem. See module docstring for the env-var override
    contract.
    """
    home_dir.mkdir(parents=True, exist_ok=True)

    creds_src = _resolve_credentials_src()
    if not creds_src.exists():
        raise TuiAuthStageError(
            f"TUI auth: credentials source missing at {creds_src}. "
            f"Set {CREDENTIALS_SRC_ENV} to an existing path or stage the "
            "file (default expects the apptainer auth bind at "
            f"{DEFAULT_CREDENTIALS_SRC})."
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

    return StagedAuth(credentials_dst=creds_dst, claude_json_dst=claude_json_dst)
