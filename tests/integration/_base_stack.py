"""Shared reader for the recipes that compose the ``:base`` IMAGE.

Not a test module — a helper the ``:base`` contract tests import.

WHY THIS EXISTS. Until 2026-08-14 the ``:base`` image was one recipe,
``apptainer-base.def``, and a contract test asking "does :base install
libnss3?" could answer it by grepping that one file. The four-layer split
(``system-deps -> python-pkgs -> base``) moved the INSTALL SITES without
changing what a ``:base`` container contains — apt packages now land in
layer 1, the venv in layer 2, and ``apptainer-base.def`` keeps only the
manifest bake.

A test that kept grepping ``apptainer-base.def`` alone would now fail while
the property it protects is still true, and — worse in the other direction —
would go quiet about a package that silently vanished from layer 1. So the
contract tests read the CONCATENATION of the three recipes: that text is the
build instruction set for a ``:base`` container, which is exactly the scope
those assertions were always written against.

Deliberately NOT included: ``apptainer-scitex.def`` (layer 4 is a separate
image, not part of :base) and ``apptainer-proxy.def`` (a standalone sidecar).

Concatenation is safe for these greps because every assertion is a
substring/regex presence check over recipe text. If a future contract needs
to know WHICH layer supplies something, read that layer's file directly
rather than widening this helper.
"""

from __future__ import annotations

from pathlib import Path

import scitex_agent_container

_CONTAINERS = Path(scitex_agent_container.__file__).resolve().parent / "containers"

# Bottom-up, matching the build chain, so a concatenated read shows sections
# in the order apptainer actually executes them.
BASE_STACK_DEFS = (
    "apptainer-system-deps.def",
    "apptainer-python-pkgs.def",
    "apptainer-base.def",
)


def base_stack_paths() -> list[Path]:
    """Absolute paths of the three recipes composing ``:base``."""
    paths = [_CONTAINERS / name for name in BASE_STACK_DEFS]
    missing = [p for p in paths if not p.is_file()]
    assert not missing, f"shipped :base stack recipe(s) missing: {missing}"
    return paths


def base_stack_text() -> str:
    """The three ``:base`` recipes concatenated, bottom-up.

    Fails loudly if any recipe is missing rather than silently returning a
    short string — an absent layer would make every presence assertion below
    it report "not installed", which reads as a content regression instead of
    a packaging bug.
    """
    return "\n".join(path.read_text() for path in base_stack_paths())
