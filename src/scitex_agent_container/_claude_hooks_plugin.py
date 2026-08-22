#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_claude_hooks_plugin.py
"""scitex-agent-container's own Claude Code hook DECLARATIONS.

sac declares the rules that protect sac's own surfaces; it does not install
them. scitex-dev discovers this provider through the ``scitex_dev.hooks``
entry-point group and applies the rows centrally -- the same split every
other ``scitex_dev.*`` group uses (``jobs``, ``system_deps``, ``gate``,
``linter.plugins``), and the same one ``cli_pkg/image_group.py`` already
names in its own comment: "each package owns its own surface; the
aggregator never hard-codes package names".

The point of a declaration is the REASON field, not the rule text. A rule
records WHAT is refused; only the reason records WHY, and "why" is what
lets a future reader decide whether the rule can be dropped. Each row below
names the mechanism that breaks without it.

The ``scitex_dev.hooks`` import is LAZY (inside ``provide_hooks``) so a
scitex-dev that predates the hooks contract -- which is every released
scitex-dev at the time this module landed -- does not break the entry
point's import-time metadata. This mirrors ``_jobs/_jobs_plugin.py``, whose
docstring gives the same reason for the same idiom. The entry point can
therefore ship and stay inert until the contract exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.hooks import HookRule

_PROVIDER = "scitex-agent-container"

#: Bundle root for the hook scripts this module declares. Package-relative
#: paths in the rows below are resolved against the package directory --
#: i.e. ``importlib.resources.files("scitex_agent_container") / <script>``.
_BUNDLE = "_baseline_assets/image_build_hooks"

#: Absolute on-disk path to the same bundle, for callers that already have
#: the package imported and do not want to go through importlib.resources.
BUNDLE_DIR = Path(__file__).resolve().parent / "_baseline_assets" / "image_build_hooks"

DENY_RAW_APPTAINER_BUILD = f"{_BUNDLE}/deny_raw_apptainer_build.sh"


def provide_hooks() -> "tuple[HookRule, ...]":
    """Hook rules sac declares about its own surfaces."""
    from scitex_dev.hooks import HookRule

    return (
        HookRule(
            id="sac.no-raw-apptainer-build",
            rule=(
                "Refuse a hand-run `apptainer build` / `singularity build` of "
                "a scitex-agent-container image; build it with `sac image "
                "build <layer>` instead."
            ),
            reason=(
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
                "the command was nice'd, so a fully demoted raw build passes "
                "it today."
            ),
            event="pre-tool-use",
            severity="deny",
            matches=("Bash",),
            provider=_PROVIDER,
            script=DENY_RAW_APPTAINER_BUILD,
        ),
    )


__all__ = ["provide_hooks", "BUNDLE_DIR", "DENY_RAW_APPTAINER_BUILD"]

# EOF
