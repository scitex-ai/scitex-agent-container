#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WHICH ENV VAR CARRIES THE CONTEXT WINDOW — asked of the harness, per launch.

One question, split into its own module under the per-file cap
``_apptainer_provider`` reached. The answer used to be a vendor test:

    if resolve_agent_harness(config) == DEFAULT_AGENT_HARNESS:
        flags += ["--env", f"CLAUDE_CODE_MAX_CONTEXT_TOKENS={max_ctx}"]

which handed an engine's declared ``max_context_tokens`` to ONE program
and silently dropped it for every other — not because anyone measured
that the others need nothing, but because the condition named a vendor.
That is one of the three concrete privileges the harness/engine split
removes. Each harness descriptor now spells its own
``context_window_env``; a harness that takes its window another way
answers ``None``, which is an explicit answer rather than an omission.
"""

from __future__ import annotations

from typing import Any

__all__ = ["context_window_env"]


class _HarnessProbe:
    """The two axes ``resolve_harness_key`` reads, and nothing else.

    ``resolve_agent_harness`` honours the ``$SAC_PROVIDER`` ops override,
    which ``config.harness`` does not carry; handing the config straight
    to the resolver would ignore that override here while honouring it
    everywhere else in the launch path.
    """

    def __init__(self, harness: str, runtime: str) -> None:
        self.harness = harness
        self.runtime = runtime


def context_window_env(config: Any, harness: str) -> str | None:
    """The context-window env var name for THIS launch, or ``None``.

    ``None`` when the resolved harness takes its window some other way
    (codex renders ``-c model_context_window`` onto its argv), or when
    the harness x runtime pair maps to no registry entry — an unmappable
    pair is refused elsewhere, loudly, and manufacturing a vendor's
    variable for it here would be a guess dressed as a default.
    """
    from ..config._harness_lookup import context_window_env_for
    from ..config._harness_registry import (
        UnmappableHarnessError,
        resolve_harness_key,
    )

    probe = _HarnessProbe(harness, str(getattr(config, "runtime", "") or ""))
    # stx-allow: fallback (reason: an unmappable harness x runtime pair is the
    # launch guard's refusal to raise, not this lookup's -- it answers "no env
    # var" and lets the guard say why)
    try:
        key = resolve_harness_key(probe)
    except UnmappableHarnessError:
        return None
    return context_window_env_for(key)
