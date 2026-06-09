"""Resolved :class:`pathlib.Path` to each bundled Apptainer ``.def`` file.

Per-layer ``.def`` files (``base`` / ``proxy`` / ``scitex``) live
alongside this module as package data. Callers — the runtime
builders in :mod:`scitex_agent_container.runtimes._apptainer_build`
and the regression tests under
``tests/scitex_agent_container/containers/`` — read the resolved
path here instead of re-deriving the location from ``__file__``
at every call site.

Centralising the lookup also satisfies the
``src ↔ tests`` mirror discipline (PS-204): the test module
``tests/scitex_agent_container/containers/test_apptainer_*.py``
has a matching ``src`` counterpart via the descriptor-strip rule
(``apptainer_scitex_def_libxcb`` → ``apptainer``).
"""

from __future__ import annotations

from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent

BASE_DEF = _THIS_DIR / "apptainer-base.def"
PROXY_DEF = _THIS_DIR / "apptainer-proxy.def"
SCITEX_DEF = _THIS_DIR / "apptainer-scitex.def"

__all__ = ["BASE_DEF", "PROXY_DEF", "SCITEX_DEF"]
