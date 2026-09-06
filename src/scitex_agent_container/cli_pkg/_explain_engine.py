#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""``sac agents explain`` — WHICH ENGINE, AND WHO DECIDED.

The engine an agent starts on can now be decided in four different
places (its own ``engine:`` pin, its own ``engines:`` block, its legacy
``claude:`` block, or a fleet-wide file it does not mention), and three
of those are invisible from the spec you are reading. A resolution
whose SOURCE cannot be inspected is a resolution nobody can debug — so
this renders the answer AND the step that produced it.

IT ALSO PRINTS THE LIBRARY'S IDENTITY — path, mtime, content hash. The
fleet engine library is a SYNCED COPY on each host, and a synced copy
can silently diverge; two agents disagreeing about what ``qwen38-27b``
means is otherwise indistinguishable from a bug in sac. The hash is the
cheapest way to compare two hosts without reading either file.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from ..config._engine_library import (
    ENGINE_KEY,
    load_fleet_library,
    local_engine_keys,
)
from ..config._engine_types import ENGINES_KEY

__all__ = ["describe_engine_resolution", "engine_lines"]

#: The precedence steps, in the order :func:`resolve_default` applies
#: them. Rendered verbatim so the printed provenance and the resolver
#: cannot drift into two different stories.
STEP_CLI = "--engine (start-time argument, overrides everything below)"
STEP_PIN = f"spec.{ENGINE_KEY} (this spec pins itself)"
STEP_MARKED = f"spec.{ENGINES_KEY}.<key>.default: true (DEPRECATED spelling of spec.{ENGINE_KEY})"
STEP_SOLE = f"spec.{ENGINES_KEY} (its only entry)"
STEP_LEGACY = "spec.claude.model / spec.claude.provider (the legacy backend block — also a pin)"
STEP_FLEET = f"the fleet engine library's `{ENGINE_KEY}:` line"
STEP_NONE = "nothing declared — the harness's own built-in backend"


def _raw_spec(spec_path: Path | None) -> Mapping[str, Any]:
    if spec_path is None:
        return {}
    import yaml

    # stx-allow: fallback (reason: explain must still render the rest of the
    # plan for a spec whose text cannot be re-read here; the loader has
    # already reported any real parse fault)
    try:
        with open(spec_path, "r", encoding="utf-8") as handle:
            document = yaml.safe_load(handle) or {}
    except (OSError, ValueError):
        return {}
    block = document.get("spec") if isinstance(document, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def _library_identity() -> list[str]:
    library = load_fleet_library()
    if not library.exists:
        return [
            f"  library: {library.path} (absent — no fleet default on this host)"
        ]
    # stx-allow: fallback (reason: a library that just became unreadable must
    # not crash the explain that is trying to tell you about it)
    try:
        blob = library.path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()[:12]
        stamp = datetime.fromtimestamp(
            library.path.stat().st_mtime, tz=timezone.utc
        ).isoformat(timespec="seconds")
    except OSError:
        digest, stamp = "?", "?"
    lines = [f"  library: {library.path}", f"    mtime: {stamp}  sha256: {digest}"]
    if library.default_key:
        lines.append(f"    fleet default: {library.default_key}")
    if library.errors:
        lines += [f"    ERROR: {message}" for message in library.errors]
    return lines


def describe_engine_resolution(
    config: Any, spec_path: Path | None = None
) -> tuple[str, str]:
    """``(engine_key, precedence_step)`` for the agent behind ``config``.

    ``engine_key`` is ``""`` when no engine resolved, which is the honest
    state of a spec that declares none — not an error, and not a
    pretended default.
    """
    spec = _raw_spec(spec_path)
    key = str(getattr(config, "engine_key", "") or "").strip()
    library = load_fleet_library()

    if str(spec.get(ENGINE_KEY) or "").strip():
        return key, STEP_PIN
    local = local_engine_keys(spec)
    if key and key in local:
        engines = getattr(config, "engines", {}) or {}
        entry = engines.get(key)
        if getattr(entry, "is_default", False):
            return key, STEP_MARKED
        if len(local) == 1:
            return key, STEP_SOLE
        return key, STEP_MARKED
    if key and key == library.default_key:
        return key, STEP_FLEET
    if not key:
        claude = spec.get("claude") if isinstance(spec, Mapping) else None
        claude_block = claude if isinstance(claude, Mapping) else {}
        if str(claude_block.get("model") or "").strip() or claude_block.get("provider"):
            return "", STEP_LEGACY
        return "", STEP_NONE
    return key, STEP_FLEET


def engine_lines(config: Any, spec_path: Path | None = None) -> list[str]:
    """The ``Engine:`` block of the explain plan."""
    key, step = describe_engine_resolution(config, spec_path)
    harness = str(getattr(config, "harness", "") or "?")
    claude = getattr(config, "claude", None)
    model = str(getattr(claude, "model", "") or getattr(config, "model", "") or "-")
    provider = getattr(claude, "provider", None)
    endpoint = str(getattr(provider, "base_url", "") or "") or "(harness default)"

    lines = [
        f"Engine: {key or '(none resolved)'}",
        f"  chosen by: {step}",
        f"  harness: {harness}   model: {model}",
        f"  endpoint: {endpoint}",
    ]
    effort = str(getattr(config, "reasoning_effort", "") or "")
    max_ctx = getattr(config, "max_context_tokens", None)
    if effort or max_ctx:
        lines.append(
            f"  reasoning_effort: {effort or '-'}   "
            f"max_context_tokens: {max_ctx or '-'}"
        )
    lines += _library_identity()
    return lines
