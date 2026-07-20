#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_freshness/_expectations.py

"""sac's symbol-expectation registry — the CONTENTS, which only sac knows.

The generic probe MECHANISM lives upstream in
``scitex_dev.versioning._symbols``. What cannot live upstream is this list:
a symbol expectation names a fix in *sac's own code*, and scitex-dev has no
way to know which of sac's symbols prove which of sac's fixes. So the
mechanism is imported and the registry stays here.

WHY A SYMBOL AND NOT A VERSION. Every entry below exists because a version
number failed to answer "is my fix deployed?" at least once on this fleet.
A fix that does not bump the version does not move the number; a number that
is bumped does not prove the code moved with it; and a fossil ``.dist-info``
reports a release whose code is gone. ``hasattr(module, symbol)`` is
evaluated against the module object actually loaded into this interpreter,
so it answers the question the number cannot.

PICKING AN ENTRY. The symbol must have been *introduced by* the fix. A name
that predates it proves nothing — it is present on both the fixed and the
broken code, so the check silently becomes a no-op that reports FRESH
forever. Every symbol here was verified present in this checkout when it was
added, and ``tests/scitex_agent_container/_freshness/`` re-verifies that on
every run — so a later rename turns into a red test rather than a permanent
false STALE aimed at the operator.

KEEP THIS SHORT. It is not a changelog. An expectation earns its place by
naming a fix whose ABSENCE would be actively dangerous *and* silent — not
merely one that mattered when it landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - kept off the CLI import path
    from scitex_dev.versioning import SymbolExpectation

__all__ = ["EXPECTATIONS", "build_expectations"]


# (module, symbol, since, why) — plain data on purpose, so this module never
# imports scitex-dev. scitex-dev is a `[dev]` extra, NOT a runtime
# dependency; a tuple of strings costs nothing and cannot fail to import.
_RAW: tuple[tuple[str, str, str, str], ...] = (
    (
        "scitex_agent_container._provenance._identity",
        "origin_mismatch",
        "0.21.14",
        "Without it, a pytest run that imported site-packages instead of the "
        "worktree reports PASS/FAIL for code nobody is editing. That is not a "
        "weaker signal than no run — it is a FALSE one, and it has already "
        "produced a green suite against the wrong package.",
    ),
    (
        "scitex_agent_container._lifecycle._restart_preflight",
        "probe_credential_usable",
        "0.21.16",
        "The non-mutating credential probe. Its predecessor 'checked' a "
        "credential BY REFRESHING it, rotating the shared token and killing "
        "every other running agent. A probe that mutates is not a probe — if "
        "this symbol is missing, restart preflight may still be the mutating "
        "one.",
    ),
    (
        "scitex_agent_container.runtimes._tui_liveness",
        "is_responsive_from_activity",
        "0.21.18",
        "Tri-state TUI liveness. Without it a wedged, auth-dead agent sitting "
        "in a healthy tmux session reads as LIVE — a PID-only probe cannot "
        "tell the difference — and the watchdog meant to be the sole safety "
        "net stays asleep.",
    ),
    (
        "scitex_agent_container._listen._deploy_freshness",
        "production_count_behind",
        "0.21.20",
        "The merged-is-not-deployed alarm. A host checkout can sit behind "
        "origin indefinitely while every version string on it looks correct; "
        "this is what makes that lag say so out loud.",
    ),
    (
        "scitex_agent_container._freshness",
        "check_currency",
        "0.21.25",
        "This wiring itself. If it is absent, `sac --version` is reading a "
        "frozen .dist-info again and the whole currency surface has silently "
        "reverted to the number that lied — the exact failure this package "
        "exists to end. Self-referential on purpose: the check that proves "
        "the checker shipped.",
    ),
)


def build_expectations() -> tuple["SymbolExpectation", ...]:
    """Adapt the raw registry into upstream ``SymbolExpectation`` objects.

    scitex-dev is imported lazily and only here, so importing this module
    stays free in the supported case where the primitive is not installed.
    """
    from scitex_dev.versioning import SymbolExpectation

    return tuple(
        SymbolExpectation(module=module, symbol=symbol, since=since, why=why)
        for module, symbol, since, why in _RAW
    )


#: The raw rows, exposed so tests can verify every symbol really exists in
#: THIS checkout without requiring scitex-dev to be present.
EXPECTATIONS = _RAW


# EOF
