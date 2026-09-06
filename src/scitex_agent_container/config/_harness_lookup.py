#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HARNESS LOOKUPS — spelling resolution and per-harness column reads.

Three questions that are ABOUT the harness registry rather than part of
it, split out under the per-file cap the registry reached when the
harness/engine split gave it two new columns:

  * :func:`canonical_harness` — which family does this SPELLING name?
  * :func:`accepted_harness_spellings` — what may a spec write?
  * :func:`context_window_env_for` — what does THIS harness call its
    context-window knob?

The third is the one that removes a privilege. Before it, the launch
path asked "is this the default harness?" and emitted one vendor's
context-window variable if so; every other harness got nothing, not
because anyone had measured that it needed nothing, but because the
condition named a vendor. Asking the registry instead means each
harness answers for itself and ``None`` is an explicit answer.

Imports the registry LAZILY inside each function: ``_harness_registry``
calls back into this module during resolution, and a module-level import
either way round would be a cycle.
"""

from __future__ import annotations

__all__ = [
    "accepted_harness_spellings",
    "canonical_harness",
    "context_window_env_for",
]


def _descriptors():
    from ._harness_registry import HARNESS_DESCRIPTORS

    return HARNESS_DESCRIPTORS


def canonical_harness(name: str) -> str | None:
    """The registry's own spelling of ``name``, or ``None`` if unknown.

    Accepts a canonical ``spec_harness`` value or any of an entry's
    :attr:`HarnessDescriptor.spec_harness_aliases` — so ``claude-code``
    and ``anthropic`` resolve to the same family without either becoming
    a second source of truth. Case- and whitespace-insensitive, matching
    ``_harness_types._stated``.
    """
    wanted = str(name or "").strip().lower()
    if not wanted:
        return None
    for descriptor in _descriptors().values():
        if wanted == descriptor.spec_harness or wanted in descriptor.spec_harness_aliases:
            return descriptor.spec_harness
    return None


def accepted_harness_spellings() -> frozenset[str]:
    """Every ``spec.harness`` spelling the registry accepts, aliases included."""
    spellings: set[str] = set()
    for descriptor in _descriptors().values():
        spellings.add(descriptor.spec_harness)
        spellings |= descriptor.spec_harness_aliases
    return frozenset(spellings)


def context_window_env_for(key: str) -> str | None:
    """The context-window env var name for the harness at ``key``.

    ``None`` means this harness does not take its window through the
    environment — which is a real answer, not a missing one, and is why
    the caller must ask the registry rather than test for one vendor.
    """
    descriptor = _descriptors().get(key)
    return descriptor.context_window_env if descriptor is not None else None
