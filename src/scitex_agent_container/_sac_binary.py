"""Single source of truth for resolving the ``sac`` executable path for
subprocess/argv construction (as opposed to the SDK MCP-sidecar resolver in
``runtimes/_sdk_channels.py::_resolve_sac_binary``, which has a different
candidate list — ``$SAC_BIN`` env override + a known container path — and is
not consulted here).

BACKGROUND (incident 2026-07, ``sac-listen`` PATH bug): ``sac listen`` runs
as a systemd ``--user`` service whose ``ExecStart`` uses an absolute path to
the venv's ``sac`` binary — that part is correct. But code INSIDE the
daemon that shells out to spawn/restart other agents used to resolve the
child's ``sac`` via ``shutil.which("sac")``, which searches the DAEMON
PROCESS's own ``PATH`` — i.e. systemd-user's default inherited PATH, which
does NOT include the venv's ``bin/`` directory (systemd ``--user`` units
never source ``~/.bashrc``/``~/.bash_profile``). When ``shutil.which("sac")``
returned ``None``, the old code fell back to the literal string ``"sac"``,
and the eventual ``subprocess.run(["sac", ...])`` died with
``FileNotFoundError: [Errno 2] No such file or directory: 'sac'`` —
uncaught, surfacing to HTTP callers as an opaque 500.

This module fixes both halves of that bug:

  1. Try ``shutil.which("sac")`` — the standard ``PATH`` lookup. In the
     buggy systemd scenario this already returns ``None`` (that's the bug),
     so this step is a no-op there; it exists so callers that legitimately
     rely on ``PATH`` (a shell that sourced the right venv, or a test that
     prepends a fake binary to ``PATH`` — see
     ``tests/scitex_agent_container/_helpers/subprocess_shim.py``, used
     throughout the ``_listen`` test suite) keep getting exactly what they
     asked for.
  2. Fall back to the executable NEXT TO ``sys.executable`` — this is
     guaranteed to be the SAME venv the running process itself is in
     (mirrors the pattern the systemd ``ExecStart=`` line already relies
     on) and sidesteps ``PATH`` entirely. This is what actually fixes the
     reported bug: under systemd ``--user``, step 1 fails, so this step
     resolves the daemon's own venv ``sac`` regardless of the inherited
     ``PATH``.
  3. If NEITHER resolves, raise ``SacBinaryNotFoundError`` — a clear,
     actionable error raised at RESOLUTION time, instead of silently
     producing an unresolvable ``"sac"`` argv that only fails later, deep
     inside a subprocess call, as an opaque ``FileNotFoundError``.

Every call site in ``src/`` that used to write
``shutil.which("sac") or "sac"`` (or an equivalent bare-``"sac"`` argv
literal) should import :func:`sac_binary` from here instead.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

__all__ = ["SacBinaryNotFoundError", "sac_binary"]


class SacBinaryNotFoundError(RuntimeError):
    """Raised when the ``sac`` executable cannot be resolved.

    Neither ``PATH`` nor a sibling of ``sys.executable`` found it — any
    subprocess argv built from the caller would be unresolvable. Fix:
    install ``sac`` into the current venv, or put it on ``PATH``.
    """


def sac_binary() -> str:
    """Resolve a path to the ``sac`` executable for use as ``argv[0]`` of a
    subprocess.

    Resolution order (first hit wins):

      1. ``shutil.which("sac")`` — standard ``PATH`` lookup.
      2. ``Path(sys.executable).with_name("sac")`` — the sibling of the
         CURRENT process's Python interpreter, i.e. the console script a
         venv install places next to its ``python``. Consulted only when
         step 1 misses, but this is exactly what makes the resolution
         PATH-independent for the systemd ``--user`` case: the daemon's
         inherited ``PATH`` lacks the venv's ``bin/``, ``shutil.which``
         returns ``None``, and this fallback still finds the daemon's own
         ``sac`` because it is colocated with ``sys.executable``
         regardless of ``PATH``.

    Raises
    ------
    SacBinaryNotFoundError
        Neither candidate resolved. Fail loud here rather than let a bare
        unresolvable ``"sac"`` string reach ``subprocess.run``/
        ``subprocess.exec`` and die later as an opaque ``FileNotFoundError``.
    """
    found = shutil.which("sac")
    if found:
        return found

    sibling = Path(sys.executable).with_name("sac")
    if sibling.exists():
        return str(sibling)

    raise SacBinaryNotFoundError(
        "cannot locate the 'sac' executable: not found next to "
        f"sys.executable ({sys.executable!r}) and not on PATH. Install sac "
        "into this interpreter's venv, or ensure it is on PATH."
    )
