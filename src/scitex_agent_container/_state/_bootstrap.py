"""Self-healing seeds for `~/.scitex/agent-container/`.

Materialises convenience artefacts that every sac install wants but
that we cannot ship via a pip post-install hook (per the SciTeX
local-state spec §3.5 — no install-time side effects, lazy mkdir on
first write only).

Currently writes one file:

* ``.gitignore`` at the agent-container root, so users who version
  `~/.scitex/` inside a dotfiles repo don't accidentally commit the
  multi-GB Apptainer SIF / sandbox / build-log binaries or per-host
  runtime state. The negation rules preserve the canonical
  `runtime/.gitkeep` + `runtime/README.md` seeds that
  `scitex_config._ecosystem.local_state.runtime_path` writes
  (`01_ecosystem_06_local-state-directories.md` §1).

Idempotent: re-running on an existing tree is a no-op. Safe to call
from any code path that creates a path under the agent-container
root.
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["ensure_root_gitignore"]


_GITIGNORE_FILENAME = ".gitignore"

# Pinned content: keep the marker line at the top so a future bump can
# detect and (optionally) refresh stale user copies without clobbering
# operator edits below.
_GITIGNORE_MARKER = "# scitex-agent-container — auto-seeded gitignore (safe to edit)"

_GITIGNORE_CONTENT = f"""{_GITIGNORE_MARKER}
#
# Excludes regenerable binary artefacts (Apptainer SIFs, sandboxes,
# build logs) and per-host runtime state from any dotfiles repo that
# tracks ~/.scitex/. The `runtime/` seed files (.gitkeep, README.md)
# are explicitly negated so an empty runtime/ still travels with the
# repo. See _skills/general/01_ecosystem_06_local-state-directories.md.

# Built Apptainer artefacts — large binary blobs, regenerable from .def
containers/*.sif
containers/**/*.sif
containers/**/*.sandbox/
containers/**/*.sandbox.tar
containers/**/*.build-*.log
containers/**/.def-hash
containers/**/*-lock.txt

# Per-host runtime state (regenerable from config + source)
runtime/*
!runtime/.gitkeep
!runtime/README.md

# Bearer tokens — never commit
tokens/

# Live Claude Code credentials — accounts/<name>/account.json is
# tracked metadata; .credentials.json holds OAuth tokens and must not
# leave the host.
accounts/*/.credentials.json
"""


def ensure_root_gitignore(root: Path) -> None:
    """Write ``<root>/.gitignore`` if absent.

    Best-effort: any I/O failure is swallowed so this never breaks the
    actual operation the caller is performing. Already-present files
    are left untouched — operator edits win.
    """
    if not root.is_dir():
        return
    gitignore = root / _GITIGNORE_FILENAME
    if gitignore.exists():
        return
    # stx-allow: fallback (reason: best-effort seed; a permission denied or
    # full-disk write here must not stop the caller's real work — e.g. the
    # operator's `sac image build` should still complete)
    try:
        gitignore.write_text(_GITIGNORE_CONTENT, encoding="utf-8")
    except OSError:
        pass


# EOF
