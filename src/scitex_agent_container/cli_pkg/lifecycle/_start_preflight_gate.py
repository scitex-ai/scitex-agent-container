#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OAuth-need predicate for ``sac agents start`` preflight gating.

Extracted from :mod:`._start` to keep the click entry point inside the
per-file 512-line cap. The predicate is a self-contained spec-loading
helper — it decides whether the parent's Anthropic OAuth preflight is
relevant for the targets at hand. ``_start.py`` re-imports it so the
``_run_preflight_once`` closure keeps working unchanged.
"""

from __future__ import annotations

from typing import Callable

import click


def make_preflight_runner(
    *,
    single_targets: list[str],
    bulk_yamls: list[str],
    no_redispatch: bool,
    broker_self: bool,
) -> Callable[[], None]:
    """Build the idempotent ("once per invocation") OAuth preflight runner.

    Lazy / one-shot: the returned callable runs the credential-expiry
    check only on the FIRST call (subsequent calls are no-ops), skips on
    ``--no-redispatch`` (peer-side invocation) and on ``--broker-self``
    (orchestrator-only — never talks to Anthropic), and skips when EVERY
    target's spec is provider-backed (the SDK session routes through a
    non-Anthropic backend; the bind-mounted Anthropic creds are never
    read). On a real failure it exits 1 with the helper's message on
    stderr, no traceback. Shared by the single, bulk, and parallel
    dispatch paths so the gating is identical across all three.
    """
    ran = {"done": False}

    def _run_preflight_once() -> None:
        if ran["done"] or no_redispatch:
            return
        ran["done"] = True
        if broker_self:
            return
        if not any_target_needs_anthropic_oauth(single_targets, bulk_yamls):
            return
        from ..._state._preflight_creds import check_oauth_token_expiry

        try:
            check_oauth_token_expiry()
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)

    return _run_preflight_once


def any_target_needs_anthropic_oauth(
    single_targets: list[str], bulk_yamls: list[str]
) -> bool:
    """Return True iff at least one target spec uses Anthropic OAuth.

    PR#314 (lead msg 24a8b27c): provider-backed specs (LiteLLM, vLLM,
    DeepSeek, gateway via ``ANTHROPIC_BASE_URL``) route the SDK session
    through a non-Anthropic backend; the parent's
    ``~/.claude/.credentials.json`` is bind-mounted but never read.
    When EVERY target has ``spec.claude.provider`` non-None, the
    parent's OAuth preflight is moot.

    Defensive default: if a spec fails to load (unresolvable name,
    unparseable YAML, schema error), assume it needs OAuth. Better to
    ask the operator for creds + surface the real spec error on the
    actual dispatch loop than to skip the gate silently on a broken
    spec the operator hasn't noticed yet.
    """
    from ...config import load_config
    from ...config._resolve import resolve_with_prefix

    for raw in list(single_targets) + list(bulk_yamls):
        try:
            cfg_path = resolve_with_prefix(raw)
            cfg = load_config(cfg_path)
        except Exception:  # stx-allow: fallback (reason: defensive — see docstring)
            return True
        provider = getattr(getattr(cfg, "claude", None), "provider", None)
        if provider is None:
            return True
    return False


__all__ = ["any_target_needs_anthropic_oauth", "make_preflight_runner"]
