#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Credential gating for ``sac agents start``.

Extracted from :mod:`._start` to keep the click entry point inside the
per-file 512-line cap; ``_start.py`` imports
:func:`make_preflight_runner` so the ``_run_preflight_once`` closure
keeps working unchanged.

The gate loads each target's spec ONCE and asks two questions of it:
does this target use Anthropic OAuth at all (a provider-backed spec does
not), and if so, is any credential IT declares usable? The second
question is answered by
:func:`_state._preflight_creds.check_spec_oauth_credentials` — the
lead's ``~/.claude/.credentials.json`` is only one possible answer, and
for the pool-backed fleet it is not the answer at all.
"""

from __future__ import annotations

from typing import Callable, Iterator

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
    (orchestrator-only — never talks to Anthropic), and skips any target
    whose spec is provider-backed (the SDK session routes through a
    non-Anthropic backend; the Anthropic creds are never read). On a real
    failure it exits 1 with the helper's message on stderr, no traceback.
    Shared by the single, bulk, and parallel dispatch paths so the gating
    is identical across all three.

    The check is PER TARGET, against the credentials THAT target's spec
    declares (:func:`_state._preflight_creds.check_spec_oauth_credentials`)
    — the pool in ``claude.credentials_files``, the singular
    ``claude.credentials_file``, or the ``claude.account`` snapshot. Only
    a spec that declares none of those is gated on the lead's
    ``~/.claude/.credentials.json``. Checking the lead file for EVERY
    agent is what took the whole fleet down on 2026-08-10: the lead token
    had lapsed, every declared pool credential was fresh, and every start
    on the host was refused anyway.

    A spec that will not load is REFUSED HERE, naming the load error. It
    is no longer gated on the lead's ``~/.claude/.credentials.json``, and
    nothing in this path reads that file any more (operator ruling,
    2026-08-19: 「勝手にデフォルトのクレデンシャルズを使わない」 — a silent
    fall back to a default is exactly what the constitution forbids).

    The old behaviour asked the lead file whenever a spec would not load,
    which meant an UNREGISTERED NAME was reported as an EXPIRED TOKEN: two
    unrelated faults printing the same sentence, and only one of them
    fixable by the caller. Refusing early with the load error is both the
    louder failure and the more honest one.
    """
    ran = {"done": False}

    def _run_preflight_once() -> None:
        if ran["done"] or no_redispatch:
            return
        ran["done"] = True
        if broker_self:
            return
        from ..._state._preflight_creds import check_spec_oauth_credentials

        try:
            for raw, cfg, err in _iter_target_configs(single_targets, bulk_yamls):
                if cfg is None:
                    raise RuntimeError(
                        f"cannot start {raw!r}: its spec could not be loaded "
                        f"({type(err).__name__}: {err}). Check that the agent "
                        "name is registered on this host and that its spec "
                        "parses. This is NOT a credential fault — sac does not "
                        "read ~/.claude/.credentials.json for agent starts."
                    )
                if getattr(getattr(cfg, "claude", None), "provider", None) is not None:
                    continue
                check_spec_oauth_credentials(cfg)
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            click.echo(f"Error: {exc}", err=True)
            raise SystemExit(1)

    return _run_preflight_once


def _iter_target_configs(
    single_targets: list[str], bulk_yamls: list[str]
) -> Iterator[tuple[str, object | None, Exception | None]]:
    """Yield ``(raw_target, AgentConfig | None, load error | None)`` per target.

    One resolve+load per target, shared by the preflight runner and
    :func:`any_target_needs_anthropic_oauth` so the two can never disagree
    about what a target's spec says.

    The load error is CARRIED rather than discarded. Discarding it is what
    made an unregistered agent name indistinguishable from a credential
    fault: the caller received a bare ``None``, had nothing to report but
    the lead credential's state, and so reported THAT. Measured 2026-08-19
    — ``POST /agents`` for a name with no spec answered 502 carrying
    "OAuth token in ~/.claude/.credentials.json expired 257594 seconds
    ago", which is true about the file and says nothing about the request.
    """
    from ...config import load_config
    from ...config._resolve import resolve_with_prefix

    for raw in list(single_targets) + list(bulk_yamls):
        try:
            cfg: object | None = load_config(resolve_with_prefix(raw))
        except Exception as exc:  # stx-allow: fallback (reason: the error is CARRIED to the caller, not swallowed; see docstring)
            yield raw, None, exc
            continue
        yield raw, cfg, None


def any_target_needs_anthropic_oauth(
    single_targets: list[str], bulk_yamls: list[str]
) -> bool:
    """Return True iff at least one target spec uses Anthropic OAuth.

    PR#314 (lead msg 24a8b27c): provider-backed specs (LiteLLM, vLLM,
    DeepSeek, gateway via ``ANTHROPIC_BASE_URL``) route the SDK session
    through a non-Anthropic backend; the Anthropic OAuth credentials are
    bind-mounted but never read. When EVERY target has
    ``spec.claude.provider`` non-None, the OAuth preflight is moot.

    Defensive default: if a spec fails to load (unresolvable name,
    unparseable YAML, schema error), assume it needs OAuth. Better to
    ask the operator for creds + surface the real spec error on the
    actual dispatch loop than to skip the gate silently on a broken
    spec the operator hasn't noticed yet.
    """
    for _raw, cfg, _err in _iter_target_configs(single_targets, bulk_yamls):
        if cfg is None:
            return True
        if getattr(getattr(cfg, "claude", None), "provider", None) is None:
            return True
    return False


__all__ = ["any_target_needs_anthropic_oauth", "make_preflight_runner"]
