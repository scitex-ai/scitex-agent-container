"""Internal submodules for ``agent_meta``.

This package exists to keep individual files under the 512-line hook
ceiling. ``agent_meta.py`` re-exports every helper here so the public
import surface (``from .._state import agent_meta as am``;
``am._foo`` access) is preserved.

No new public API lives here — all entries are re-exported by
``agent_meta``.
"""
