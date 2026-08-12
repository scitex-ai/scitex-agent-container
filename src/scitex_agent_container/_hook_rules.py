#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_hook_rules.py
"""scitex-agent-container's own Claude Code hook DECLARATIONS.

sac declares the rules that protect sac's own surfaces; it does not install
them. scitex-dev discovers this provider through the ``scitex_dev.hooks``
entry-point group and applies the rows centrally -- the same split every
other ``scitex_dev.*`` group uses (``jobs``, ``system_deps``, ``gate``),
and the one ``cli_pkg/image_group.py`` already names in its own comment:
"each package owns its own surface; the aggregator never hard-codes
package names".

The point of a declaration is the REASON field, not the rule text. A rule
records WHAT is refused; only the reason records WHY, and "why" is what
lets a future reader decide whether the rule can be retired.

WHY THESE ARE MAPPINGS AND NOT ``scitex_dev.hooks.HookRule`` INSTANCES
----------------------------------------------------------------------
Each row's keys are exactly ``HookRule``'s field names, so the aggregator
gets an identical object from ``HookRule(**row)`` -- including that type's
validate-at-construction guarantee, which is the property worth keeping.
What is deliberately avoided is sac IMPORTING the type, because today that
import cannot work:

  * ``scitex_dev.hooks`` is not in any RELEASED scitex-dev -- it lives on
    an unmerged branch -- and this package pins ``scitex-dev<0.48``.
  * Referencing it anyway is not a soft failure. The PS-140 cross-package
    import gate recomputes sac's peer-module list from source and then
    HARD-IMPORTS every entry, so a lazy import inside a function is caught
    just the same. It was: adding ``from scitex_dev.hooks import HookRule``
    turned both pytest matrix jobs red on ``missing from gate:
    ['scitex_dev.hooks']``, and adding the entry made them red on
    ``ModuleNotFoundError``. That gate is correct -- a leaf must not
    reference a peer module nobody ships.

Declaring DATA has neither problem, and it removes a release-ordering
coupling that would otherwise apply to every leaf that adopts a new group
first: keystone releases, then leaf raises its pin, then leaf declares.

This mirrors the ``scitex_todo.hooks`` consumer sac already ships, whose
pyproject comment states the same discipline in one line: "Bus-only
contract: no import of scitex_todo."

KNOWN GAP, TRACKED: ``discover_hooks`` currently skips any row that is not
a ``HookRule`` instance (``isinstance`` check, warn-and-continue), so until
it coerces mappings -- or until scitex-dev releases ``hooks`` and sac's pin
moves past it, at which point this module constructs the type directly --
these rows are enumerable here but not yet picked up by the aggregator.
The hook itself is fully live regardless: it is a standalone script, wired
by the sibling ``settings.local.json.fragment.json``, and depends on none
of this.
"""

from __future__ import annotations

from pathlib import Path

_PROVIDER = "scitex-agent-container"

#: Bundle root for the hook scripts this module declares. Paths in the rows
#: below are PACKAGE-RELATIVE, resolved by the aggregator against the
#: ``owner_module`` it stamps from this entry point -- i.e.
#: ``importlib.resources.files("scitex_agent_container") / <script>``.
_BUNDLE = "_baseline_assets/image_build_hooks"

#: Absolute on-disk path to the same bundle, for callers that already have
#: the package imported and would rather not go through importlib.resources.
BUNDLE_DIR = Path(__file__).resolve().parent / "_baseline_assets" / "image_build_hooks"

DENY_RAW_APPTAINER_BUILD = f"{_BUNDLE}/deny_raw_apptainer_build.sh"


def provide() -> "tuple[dict[str, object], ...]":
    """Hook rules sac declares about its own surfaces.

    Keys are ``scitex_dev.hooks.HookRule`` field names; the aggregator
    constructs the dataclass. ``owner_module`` is deliberately omitted --
    discovery stamps it from the entry point, so a leaf never repeats its
    own module name.
    """
    return (
        {
            "id": "sac.no-raw-apptainer-build",
            "provider": _PROVIDER,
            "rule": (
                "Refuse a hand-run `apptainer build` / `singularity build` "
                "of a scitex-agent-container image; build it with "
                "`sac image build <layer>` instead."
            ),
            "reason": (
                "sac's image build is not `apptainer build` plus arguments. "
                "`sac image build` stages a build-context directory holding "
                "the .def alongside a `scitex-agent-container-src/` copy of "
                "the installed package, and the .def resolves its `%files` "
                "sources against that staging dir -- apptainer-base.def "
                "documents the contract in its own %files comment, bundling "
                "'the package's OWN source tree so the in-SIF sac is the "
                "source tree that shipped this .def, never a git+...@main "
                "snapshot'. A hand-run build skips the staging and does NOT "
                "fail loudly: it produces a SIF whose in-image sac is "
                "whatever happened to be lying around, and the mismatch "
                "surfaces weeks later as a version that makes no sense. The "
                "build is additionally becoming staged (base -> scitex -> "
                "...), where a hand-run build also bypasses parent-chain "
                "resolution and staleness checking, silently layering a "
                "child on a stale or missing parent. The existing heavy-job "
                "demotion hook does not cover this: it judges only whether "
                "the command was nice'd, so a fully demoted raw build "
                "passes it today."
            ),
            "event": "pre-tool-use",
            "severity": "deny",
            "matches": ("Bash",),
            "script": DENY_RAW_APPTAINER_BUILD,
            "bypass": "SAC_ALLOW_RAW_IMAGE_BUILD",
            "doctrine": f"{_BUNDLE}/README.md",
        },
    )


__all__ = ["provide", "BUNDLE_DIR", "DENY_RAW_APPTAINER_BUILD"]

# EOF
