#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""The ``--engine`` / ``--probe-engine`` option group (``spec.engines``).

Sibling of ``_start_gate_options`` / ``_start_session_options``: one
cohesive option group per module so the click entry stays a thin
orchestrator under the per-file line cap.

``--engine <key>`` selects one of the backends the spec declares under
``spec.engines`` for THIS start (operator answer Q2 — start time only;
there is no per-turn hatch and nothing rebinds mid-session). An unknown
key is a hard error listing the declared keys; it never degrades to the
default.

``--probe-engine`` is the OPT-IN live reachability probe, and its help
text states the choice explicitly because the operator has to be able to
predict which failures refuse a start:

  * WITHOUT it (the default) the refusal surface is STATIC resolution
    only — the provider name resolves in the registry, the inline dict
    is complete, ``$AUTH_TOKEN_ENV`` is set on this host, the harness is
    known. No sockets, so a flapping link cannot ground the fleet.
  * WITH it, sac additionally makes ONE bounded TCP connect to the
    engine's ``base_url``. Only an ACTIVE connection refusal (a definite
    answer) refuses the start; a timeout or DNS failure is reported as
    "could not tell" with a LOUD warning and the start proceeds — never
    silently treated as honourable.
"""

from __future__ import annotations

import click

from ..._lifecycle._engine_select import ENGINE_PROBE_ENV


def engine_options(func):
    """Apply the engine-selection flags to a click command, in help order."""
    options = (
        click.option(
            "--engine",
            "engine",
            type=str,
            default=None,
            metavar="KEY",
            help="Start on the named engine from the spec's `engines:` block "
            "(e.g. --engine qwen38-27b) instead of its declared default. "
            "START TIME ONLY: the choice is folded in before the runtime is "
            "built and never rebinds mid-session. An unknown key fails loud "
            "listing the declared engines — sac never falls back to the "
            "default. An engine that cannot be honoured (unregistered "
            "provider name, incomplete inline provider, unset auth env var, "
            "unknown harness) REFUSES the start naming the engine and the "
            "fix; it never falls back to another engine. Reachability is NOT "
            "checked unless --probe-engine is passed.",
        ),
        click.option(
            "--probe-engine",
            "probe_engine",
            is_flag=True,
            default=None,
            help="Additionally make ONE short bounded TCP connect to the "
            "selected engine's provider base_url before starting. OFF by "
            "default on purpose: making every start depend on a "
            "possibly-remote endpoint answering is how a refusal grounds a "
            "fleet. Even when on, only an ACTIVE connection refusal refuses "
            "the start — a timeout or DNS failure is reported as "
            "'could not tell' with a loud warning and the start proceeds. "
            f"Env: {ENGINE_PROBE_ENV}=1.",
        ),
    )
    for option in reversed(options):
        func = option(func)
    return func


__all__ = ["engine_options"]
