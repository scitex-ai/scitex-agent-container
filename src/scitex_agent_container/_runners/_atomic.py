"""Atomic text writes for the claude-session runner state dir.

The runner persists small state files (``pid``, ``started_at``,
``heartbeat.json``, ``quota.json``, ``instance_id``, ``session_id``,
``session_id_history``) with the tmp + ``os.replace`` pattern so a
concurrent *reader* (``sac agent status``) never sees a half-formed file.

A **fixed** ``<name>.tmp`` sibling is not safe when two *writers* share the
same state dir — two xdist workers keyed on the same agent name, or two
runner processes racing on one agent's dir. Both open the SAME tmp path;
the first ``replace()`` renames it onto the target, and the loser's
``replace()`` then raises ``FileNotFoundError`` because the tmp it expected
is already gone. Measured on CI as::

    FileNotFoundError: '.../runtime/alpha/instance_id.tmp'
      -> '.../runtime/alpha/instance_id'

Giving every writer a UNIQUE tmp (via :func:`tempfile.mkstemp`) removes the
collision: only the final rename — which is atomic — contends, and it is
last-writer-wins with no spurious error. This mirrors the existing correct
helper in ``_account/codex_account._atomic_write``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def atomic_write_text(dst: Path, text: str) -> None:
    """Atomically write ``text`` to ``dst`` via a per-writer-unique temp file.

    The temp file is created in ``dst.parent`` (same filesystem, so the
    rename is atomic) with a unique name, so concurrent writers to the same
    directory never collide on the temp path. The parent dir is created if
    absent. On any failure the temp file is removed so a crashed write never
    leaves a stray ``.tmp`` behind.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".tmp", dir=dst.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
