#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""THE FLEET ENGINE LIBRARY — ``$SCITEX_DIR/agent-container/engines.yaml``.

WHY THIS FILE EXISTS. The operator asked (Telegram 2026-08-29, again
2026-09-06) for TWO things that the per-spec ``engines:`` block alone
cannot give: a ONE-LINE switch to move ONE agent onto Qwen, and a way to
make QWEN THE DEFAULT for the fleet. The first is ``spec.engine: <key>``.
The second is this file: a sibling of ``agents/`` holding the engine
definitions once and naming, in ONE line, which of them an agent that
pins nothing runs.

    apiVersion: scitex-agent-container/v3
    kind: EngineLibrary

    engine: claude-opus          # THE FLEET DEFAULT. One line.

    engines:
      claude-opus:
        model: opus[1m]
        provider: anthropic
      qwen38-27b:
        model: qwen38-27b
        provider:
          base_url: http://100.64.0.1:18772
          auth_token_env: SCITEX_GENAI_GATEWAY_API_KEY
        reasoning_effort: low
        max_context_tokens: 1048576

NO SECOND VOCABULARY. The ``engines:`` block here is parsed by the SAME
:func:`_engine_types.parse_engines` an agent spec's own block goes
through — same keys, same folding, same validator. An engine entry is an
engine entry wherever it is written; the only thing this file adds is
the fleet-wide ``engine:`` default.

NO VENDOR IS PRIVILEGED BY THIS. The fleet default is DATA. Writing
``engine: qwen38-27b`` costs exactly what ``engine: claude-opus`` costs —
one line in a YAML file — and neither is the unmarked case, because
there is no code branch for either. That is the whole answer to "how
does Qwen become the default without privileging it": it does not become
a branch, it becomes a row addressed by key.

A MISSING FILE IS LEGAL, and is the state of every host until the
operator writes one: :func:`load_fleet_library` returns an empty
library with no default, every spec keeps resolving exactly as it did,
and nothing warns. An UNREADABLE or MALFORMED file is NOT legal and does
not evaporate — it is reported through :attr:`FleetLibrary.errors`,
which the spec validator surfaces as a load error naming this path.

WHERE IT LIVES, AND WHERE IT IS EDITED. Resolved through
``_state.state_paths`` exactly like ``agents/`` is (so ``$SCITEX_DIR``
is honoured and a relocated state root takes its library with it). The
SOURCE OF TRUTH is ``.scitex/agent-container/engines.yaml`` IN THIS
REPO, tracked beside :mod:`._provider_registry`, which defines the
provider names the library's entries use — one diff reviews both, where
a copy in another repo would let the two drift. It is deployed from
there; ``$SCITEX_DIR/agent-container`` is a synced copy, never
hand-edited, the same standing rule agent specs follow.

NOTHING SILENTLY DEPENDS ON AN UNSET VARIABLE. Measured on the fleet
hosts 2026-09-06: ``$SAC_ENGINES_FILE`` and ``$SCITEX_DIR`` are both
unset, so :func:`fleet_engines_path` takes ``scitex_dir()``'s DOCUMENTED
``~/.scitex`` default and lands on ``~/.scitex/agent-container/
engines.yaml`` — the directory that already holds ``agents/``,
``accounts/``, ``containers/`` and ``runtime/``. Unset is the ordinary
case here, not a hole: the default is written down in
``_state.state_paths``, and a host that exports ``$SCITEX_DIR``
(Spartan) moves the library with the rest of its state.

``$SAC_ENGINES_FILE`` overrides the path for one process. It is an
OPS/TEST surface, not a spec surface: a spec must not be able to point
sac at a different library, because that would let one agent redefine
what every other agent's key means.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ._engine_types import (
    ENGINE_PIN_KEY,
    ENGINES_KEY,
    EngineSpec,
    parse_engines,
)

__all__ = [
    "ENGINE_KEY",
    "FLEET_ENGINES_ENV",
    "FLEET_ENGINES_FILENAME",
    "FleetLibrary",
    "fleet_default_key",
    "fleet_engines",
    "fleet_engines_path",
    "load_fleet_library",
    "local_engine_keys",
    "resolve_engine_namespace",
    "spec_engine_key",
]

#: The key that PINS one engine, re-exported so this module reads in one
#: vocabulary. It is spelled the same in an agent spec (``spec.engine``)
#: and at the top of this library, because it means the same thing in
#: both: "the engine to run when nothing more specific is stated".
ENGINE_KEY = ENGINE_PIN_KEY

#: The library's filename, a sibling of ``agents/``.
FLEET_ENGINES_FILENAME = "engines.yaml"

#: OPS/TEST-only path override. NOT a spec surface — see the module docstring.
FLEET_ENGINES_ENV = "SAC_ENGINES_FILE"


@dataclass(frozen=True)
class FleetLibrary:
    """The parsed fleet library, or the honest absence of one.

    ``exists`` is three-state-adjacent on purpose: ``False`` with no
    errors means "no library on this host", which is legal and silent;
    ``True`` with errors means "a library that could not be read", which
    is a load error, never an empty default.
    """

    path: Path
    exists: bool = False
    engines: dict[str, EngineSpec] = field(default_factory=dict)
    default_key: str = ""
    errors: tuple[str, ...] = ()


def fleet_engines_path() -> Path:
    """Where the fleet library lives on THIS host.

    ``$SAC_ENGINES_FILE`` wins; otherwise ``$SCITEX_DIR/agent-container/
    engines.yaml``, resolved through the same SSOT ``agents/`` uses so a
    relocated state root cannot leave the library behind.
    """
    override = os.environ.get(FLEET_ENGINES_ENV, "").strip()
    if override:
        return Path(os.path.expanduser(override))
    from .._state.state_paths import agent_container_root

    return agent_container_root() / FLEET_ENGINES_FILENAME


# In-process memo keyed by (path, size, mtime_ns) — the same correctness
# rule ``_spec_cache`` states: any doubt whatsoever is a MISS that falls
# through to a real parse, so the cache can make a command faster and can
# never make it answer differently.
_MEMO: dict[tuple[str, int, int], FleetLibrary] = {}


def _stat_key(path: Path) -> tuple[int, int] | None:
    # stx-allow: fallback (reason: an unstattable library is simply not
    # cacheable; the caller still parses it)
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except OSError:
        return None


def _parse_document(path: Path, raw: Any) -> FleetLibrary:
    """Fold a loaded YAML document into a :class:`FleetLibrary`."""
    errors: list[str] = []
    if raw is None:
        # An empty file is a written "I declare no fleet engines" — the
        # explicit posture, not a fault.
        return FleetLibrary(path=path, exists=True)
    if not isinstance(raw, Mapping):
        return FleetLibrary(
            path=path,
            exists=True,
            errors=(
                f"{path} must be a mapping with an `{ENGINES_KEY}:` block "
                f"and an optional `{ENGINE_KEY}: <key>` fleet default, got "
                f"{type(raw).__name__}.",
            ),
        )

    kind = str(raw.get("kind") or "").strip()
    if kind and kind != "EngineLibrary":
        errors.append(
            f"{path} declares kind={kind!r}; the fleet engine library is "
            "`kind: EngineLibrary`. Nothing else is read from this path."
        )

    block = raw.get(ENGINES_KEY)
    engines: dict[str, EngineSpec] = {}
    if block is None:
        pass
    elif not isinstance(block, Mapping):
        errors.append(
            f"{path}: `{ENGINES_KEY}:` must be a mapping of "
            "`<engine-key>: {model, provider, ...}` entries, got "
            f"{type(block).__name__}."
        )
    else:
        engines = parse_engines(raw)

    default_key = str(raw.get(ENGINE_KEY) or "").strip()
    if default_key and default_key not in engines:
        declared = ", ".join(repr(key) for key in engines) or "(none)"
        errors.append(
            f"{path}: `{ENGINE_KEY}: {default_key!r}` names the FLEET "
            f"DEFAULT engine, but this library declares no engine by that "
            f"name. Declared engines: {declared}. Every agent that pins "
            "nothing follows this line, so a typo here would repoint the "
            "whole fleet — sac refuses to guess which entry was meant."
        )
        default_key = ""

    return FleetLibrary(
        path=path,
        exists=True,
        engines=engines,
        default_key=default_key,
        errors=tuple(errors),
    )


def load_fleet_library(path: Path | None = None) -> FleetLibrary:
    """Read and parse the fleet library; a missing file is legal and empty.

    Errors are CARRIED, not raised: this is called from the loader, which
    walks every spec on the host, and a raise here would make one broken
    library break ``sac agents list`` for agents that never referenced
    it. The validator turns :attr:`FleetLibrary.errors` into a load error
    for the specs that actually depend on the library.
    """
    target = Path(path) if path is not None else fleet_engines_path()
    stat_key = _stat_key(target)
    if stat_key is None:
        return FleetLibrary(path=target)
    memo_key = (str(target), *stat_key)
    cached = _MEMO.get(memo_key)
    if cached is not None:
        return cached

    import yaml

    # stx-allow: fallback (reason: an unreadable/malformed library is
    # REPORTED through .errors, never silently treated as absent — the
    # raise is the validator's, once, for the spec that depends on it)
    try:
        with open(target, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle)
    except (OSError, yaml.YAMLError) as exc:
        library = FleetLibrary(
            path=target,
            exists=True,
            errors=(
                f"{target} could not be read as YAML ({exc}). The fleet "
                "engine library decides which backend every unpinned agent "
                "starts on, so sac reports this rather than proceeding as "
                "if no library existed.",
            ),
        )
    else:
        library = _parse_document(target, document)
    _MEMO[memo_key] = library
    return library


def fleet_engines(path: Path | None = None) -> dict[str, EngineSpec]:
    """The engines the fleet library declares (``{}`` when there is none)."""
    return dict(load_fleet_library(path).engines)


def fleet_default_key(path: Path | None = None) -> str:
    """The fleet-wide default engine key (``""`` when none is declared)."""
    return load_fleet_library(path).default_key


def spec_engine_key(spec: Mapping) -> str:
    """The engine this SPEC pins, or ``""`` when it pins none.

    Absent, null and empty all mean "states no opinion" — the same
    three-cases-are-one rule every other axis in this package uses.
    """
    if not isinstance(spec, Mapping):
        return ""
    return str(spec.get(ENGINE_KEY) or "").strip()


def resolve_engine_namespace(
    spec: Mapping, *, path: Path | None = None
) -> dict[str, EngineSpec]:
    """Every engine key THIS spec can name: fleet ∪ spec-local.

    SPEC-LOCAL WINS A COLLISION, and the direction is deliberate: a spec
    that spells out an engine under a name the fleet also uses is making
    a statement about ITSELF, and a fleet-file edit must never silently
    change what a spec's own written entry means. The reverse would make
    a local declaration decorative.

    The merged namespace is what ``--engine`` selects from, so an
    operator can start any agent on any fleet engine without editing
    the spec first.
    """
    merged = dict(load_fleet_library(path).engines)
    merged.update(parse_engines(spec))
    return merged


def local_engine_keys(spec: Mapping) -> frozenset[str]:
    """The keys the SPEC ITSELF declares under ``spec.engines``."""
    return frozenset(parse_engines(spec))
