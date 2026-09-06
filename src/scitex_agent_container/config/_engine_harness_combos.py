#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HARNESS × ENGINE — which pairings sac can actually run.

THE TWO AXES ARE ORTHOGONAL, BUT NOT EVERY CELL IS FILLED. Splitting the
harness from the engine makes both freely combinable in the GRAMMAR, and
a grammar that lets you write a combination sac cannot run owes you a
refusal that names the combination — not a ``ValueError`` thrown three
layers down in argv construction, after the operator has already stopped
the agent.

That is exactly what the combinations below used to do. ``harness: codex``
with an engine that declares no provider failed in
``runtimes/_apptainer_inner_argv_codex`` (``ProviderEnvError``, and a
second one for a missing model); ``harness: openai`` failed with a
generic "Unsupported runtime" at launch. Both are decidable from the
declaration alone, so both move HERE, to start time, named as pairings.

THE DEEP REFUSALS ARE KEPT. ``_apptainer_codex_env`` and
``_apptainer_inner_argv_codex`` still raise; they simply stop being the
first thing an operator sees. Defence in depth is not redundancy when
the two guards protect different entry points — a caller that builds an
argv without going through start selection still needs the deep one.

THREE-VALUED, like every other verdict on this axis. ``could-not-tell``
is returned when the harness could not be determined at all, and it is
NEVER rendered as "fine": the caller warns loudly and the start is
recorded as unverified.

VENDOR NAMES APPEAR IN THE TABLE, and only in the table. That is the
correct place for them: a row keyed by a harness family is a statement
about that specific program's measured capability, not a branch that
privileges one. Every family appears; none is the unmarked default; a
family with no entry is ``could-not-tell``, not "assume it works".
"""

from __future__ import annotations

from ._engine_honour import (
    VERDICT_HONOURABLE,
    VERDICT_NOT_HONOURABLE,
    VERDICT_UNKNOWN,
    EngineVerdict,
)
from ._engine_types import ENGINE_PIN_KEY, ENGINES_KEY, EngineSpec
from ._harness_types import V4_HARNESS_DISPATCH_CARD, is_known_harness, list_harnesses

__all__ = ["combination_verdict", "describe_combinations"]

#: The inference-provider name that collides with the ``codex`` HARNESS
#: name. ``spec.claude.provider: codex`` is the local scitex-genai
#: gateway translating Anthropic Messages to a ChatGPT Codex
#: subscription — with CLAUDE CODE still running the loop. Composing it
#: with ``harness: codex`` asks the codex program to drive a gateway that
#: exists to let a non-codex program borrow a codex subscription.
_CODEX_PROVIDER_NAME = "codex"


def _provider_name(engine: EngineSpec) -> str:
    declared = engine.provider_declared
    if isinstance(declared, str):
        return declared.strip().lower()
    return ""


def _no(engine: EngineSpec, reason: str, fix: str) -> EngineVerdict:
    return EngineVerdict(engine.key, VERDICT_NOT_HONOURABLE, reason, fix)


def combination_verdict(engine: EngineSpec, harness: str | None) -> EngineVerdict:
    """Can ``harness`` run ``engine``? — the pairing question, on its own.

    ``harness`` is the EFFECTIVE harness for this start: the engine's own
    ``harness`` when it states one, else the spec's. ``None`` (or empty)
    means it could not be determined, which is ``could-not-tell`` — not
    an assumption that it is the usual one.
    """
    name = (harness or "").strip().lower()
    if not name:
        return EngineVerdict(
            engine.key,
            VERDICT_UNKNOWN,
            "the harness for this start could not be determined, so whether "
            f"engine {engine.key!r} can be run by it is unknown",
            f"state `harness: <program>` at the top of spec: (one of "
            f"{list_harnesses()})",
        )
    if not is_known_harness(name):
        return _no(
            engine,
            f"harness={name!r} is not a harness sac can run",
            f"set spec.harness (or spec.{ENGINES_KEY}.{engine.key}.harness) "
            f"to one of {list_harnesses()}",
        )

    if name == "openai":
        return _no(
            engine,
            f"harness={name!r} has no lifecycle launch adapter, so NO engine "
            f"— including {engine.key!r} — can be started through it",
            "run the openai-agents SDK through `a2a.handler: openai_session` "
            "(the grant-agent pattern), or pick a harness with a launch "
            f"path (anthropic, codex). Tracked on card "
            f"{V4_HARNESS_DISPATCH_CARD}",
        )

    if name == "codex":
        if engine.provider is None:
            return _no(
                engine,
                f"harness={name!r} needs an explicit model endpoint, and "
                f"engine {engine.key!r} declares no provider — the codex "
                "program has no OAuth path of its own to fall back on, so "
                "there is nothing to point it at",
                f"give spec.{ENGINES_KEY}.{engine.key}.provider a registered "
                "name or an inline {base_url, auth_token_env} pair, or pin a "
                f"provider-bearing engine with `{ENGINE_PIN_KEY}: <key>`",
            )
        if _provider_name(engine) == _CODEX_PROVIDER_NAME:
            return _no(
                engine,
                f"harness={name!r} composed with provider={_CODEX_PROVIDER_NAME!r} "
                "is the two-axis word collision: the PROVIDER of that name is "
                "the gateway that lets CLAUDE CODE borrow a Codex "
                "subscription, and the HARNESS of that name is the codex "
                "program itself. Running one through the other is not a "
                "configuration, it is a name clash",
                f"either set spec.harness: anthropic (Claude Code through the "
                f"{_CODEX_PROVIDER_NAME} gateway — the combination that "
                f"works), or give spec.{ENGINES_KEY}.{engine.key}.provider "
                "the endpoint codex should talk to directly",
            )

    return EngineVerdict(engine.key, VERDICT_HONOURABLE)


def describe_combinations() -> list[tuple[str, str, str]]:
    """The table as data — ``(harness, engine shape, verdict)`` rows.

    Exists so the docs and the tests read the SAME table the resolver
    consults, rather than a hand-copied second one that can drift.
    """
    return [
        ("anthropic", "provider-bearing engine", VERDICT_HONOURABLE),
        ("anthropic", "engine with no provider (OAuth)", VERDICT_HONOURABLE),
        ("codex", "provider-bearing engine", VERDICT_HONOURABLE),
        ("codex", "engine with no provider", VERDICT_NOT_HONOURABLE),
        (
            "codex",
            f"provider literally named {_CODEX_PROVIDER_NAME!r}",
            VERDICT_NOT_HONOURABLE,
        ),
        ("openai", "any engine", VERDICT_NOT_HONOURABLE),
        ("(undetermined)", "any engine", VERDICT_UNKNOWN),
    ]
