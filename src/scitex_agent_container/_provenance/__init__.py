#!/usr/bin/env python3
# File: src/scitex_agent_container/_provenance/__init__.py

"""Identity of the code that is ACTUALLY LOADED — not the version it claims.

``sac --version`` used to print a declared version string and nothing
else. That string reads identically on a machine where a fix shipped and
one where it did not, so it could never answer the only question anyone
asks it: *is my fix actually deployed?*

Two tiers:

* :func:`identity` — the ``--version`` fast path (~0.5 ms). Version +
  commit + where the module was loaded from.
* :func:`audit` — ``sac provenance``. Adds a content hash of the loaded
  tree, duplicate/fossil ``.dist-info`` detection, and shadowed-import
  detection. Costs ~35 ms, so it is deliberately NOT on ``--version``.

:func:`audit` is imported lazily: the fast path must not pay for the
heavy one.
"""

from __future__ import annotations

from ._git import head_sha, repo_root_for_package
from ._hash import code_hash
from ._identity import (
    DIST_NAME,
    baked,
    format_terse,
    identity,
    origin_mismatch,
    package_dir,
    short_id,
)

__all__ = [
    "DIST_NAME",
    "audit",
    "baked",
    "code_hash",
    "format_terse",
    "head_sha",
    "identity",
    "origin_mismatch",
    "package_dir",
    "repo_root_for_package",
    "short_id",
]


def __getattr__(name: str):
    """Defer the heavy audit module until something actually asks for it."""
    if name == "audit":
        from ._audit import audit as _audit

        return _audit
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# EOF
