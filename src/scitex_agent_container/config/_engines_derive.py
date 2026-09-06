"""What a migrated ``spec.engines`` block SAYS, and how it is rendered.

Pure. No YAML parsing, no line numbers, no I/O — it takes the backend a spec
already declares, as primitives, and returns the entries plus the exact lines
to write. :mod:`._engines_line` owns the text surgery that puts them in a file.

THE DERIVATION, and what each half is allowed to invent (nothing):

* **The DEFAULT entry is the spec's own backend, restated.** Its ``harness``
  is what :func:`._harness_types.resolve_spec_harness` already resolves, its
  ``model`` is the spec's own ``spec.claude.model`` text copied verbatim, and
  its ``provider`` is ``spec.claude.provider`` copied verbatim. A spec whose
  provider points at a local endpoint keeps pointing there. ``provider:
  anthropic`` is written ONLY when the spec declared no provider at all —
  that is the registry's own sentinel for "the default Anthropic OAuth
  backend", it is what the spec already resolves to, and writing it is how
  the entry avoids the other failure: an unstated provider that silently
  defaults to a vendor.

* **The ALTERNATE entry is the fleet Qwen gateway**, named by
  :data:`._qwen_gateway.QWEN_GATEWAY_PROVIDER` rather than by address, so the
  address stays in one module instead of in 119 spec files.

THE ENTRY KEY. ``claude`` when the backend is the plain Anthropic OAuth path
— the name the operator's own already-migrated ``business`` spec uses — and
a slug of the model otherwise, because "claude" is a false name for an entry
holding Qwen and a vendor name is a claim about scope.

WHEN THE TWO COLLIDE. A spec that ALREADY runs ``qwen38-27b`` derives the
alternate's own key, and one mapping cannot hold two entries under one key.
Such a spec gets a single-engine block naming the backend it already has, and
the migration reports it as its own outcome rather than pretending it wrote
two. Repointing that spec's inline loopback URL at the fleet gateway would be
a behaviour change dressed as a migration, so it is not done here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._qwen_gateway import (
    QWEN_ENGINE_HARNESS,
    QWEN_ENGINE_KEY,
    QWEN_ENGINE_MAX_CONTEXT_TOKENS,
    QWEN_ENGINE_MODEL,
    QWEN_ENGINE_REASONING_EFFORT,
    QWEN_GATEWAY_PROVIDER,
)

__all__ = [
    "CLAUDE_ENGINE_KEY",
    "EngineEntry",
    "derive_entries",
    "engine_key_for",
    "render_engines_block",
    "slug_engine_key",
]

#: The name the Anthropic OAuth entry gets, matching the fleet's one
#: hand-migrated spec.
CLAUDE_ENGINE_KEY = "claude"

#: The registry sentinel meaning "the default Anthropic OAuth backend,
#: stated".
ANTHROPIC_PROVIDER = "anthropic"

_ILLEGAL = re.compile(r"[^A-Za-z0-9._-]+")
_RUNS = re.compile(r"-{2,}")


def slug_engine_key(model: str) -> str:
    """A model id turned into something typable after ``--engine``.

    ``_engine_validation`` requires an engine key to start alphanumeric and to
    hold only ``[A-Za-z0-9._-]`` thereafter, because it is typed on a command
    line. ``opus[1m]`` is a legal model and an illegal key; this is the one
    place that gap is bridged. Returns ``""`` when nothing survives, which the
    caller treats as a refusal rather than inventing a name.
    """
    slug = _RUNS.sub("-", _ILLEGAL.sub("-", str(model).strip().lower()))
    slug = slug.strip("-._")
    while slug and not slug[0].isalnum():
        slug = slug[1:]
    return slug


def engine_key_for(model: str, provider_declared: object) -> str:
    """The key the DEFAULT entry gets. ``""`` when the model yields no slug."""
    if provider_declared is None:
        return CLAUDE_ENGINE_KEY
    if (
        isinstance(provider_declared, str)
        and provider_declared.strip().lower() == ANTHROPIC_PROVIDER
    ):
        return CLAUDE_ENGINE_KEY
    return slug_engine_key(model)


@dataclass(frozen=True)
class EngineEntry:
    """ONE rendered engine entry. ``provider_lines`` holds the nested form."""

    key: str
    harness: str
    model: str
    #: The scalar written after ``provider:``. None when the provider is a
    #: nested block, in which case ``provider_lines`` carries its children
    #: with their original indentation already stripped.
    provider_scalar: "str | None" = None
    provider_lines: "tuple[str, ...]" = ()
    reasoning_effort: str = ""
    max_context_tokens: "int | None" = None
    is_default: bool = False


def qwen_entry() -> EngineEntry:
    """The fleet-gateway alternate, identical in every migrated spec."""
    return EngineEntry(
        key=QWEN_ENGINE_KEY,
        harness=QWEN_ENGINE_HARNESS,
        model=QWEN_ENGINE_MODEL,
        provider_scalar=QWEN_GATEWAY_PROVIDER,
        reasoning_effort=QWEN_ENGINE_REASONING_EFFORT,
        max_context_tokens=QWEN_ENGINE_MAX_CONTEXT_TOKENS,
    )


def derive_entries(
    *,
    harness: str,
    model: str,
    provider_declared: object,
    provider_scalar: "str | None",
    provider_lines: "tuple[str, ...]" = (),
) -> "tuple[tuple[EngineEntry, ...], str]":
    """The entries for one spec, in write order, plus a refusal reason.

    Returns ``((), reason)`` when the spec cannot be given a key; otherwise
    ``(entries, "")``. ``provider_scalar`` / ``provider_lines`` are the
    VERBATIM text the caller lifted out of the spec, so the restated backend
    is byte-for-byte the one that was already there.
    """
    key = engine_key_for(model, provider_declared)
    if not key:
        return (), "the model yields no usable engine key"

    scalar = provider_scalar
    if provider_declared is None and not provider_lines:
        # Stated explicitly rather than left empty: an omitted provider reads
        # as "no opinion", and an entry with no opinion about its backend is
        # the ambiguity this axis exists to remove.
        scalar = ANTHROPIC_PROVIDER

    default = EngineEntry(
        key=key,
        harness=harness,
        model=model,
        provider_scalar=scalar,
        provider_lines=tuple(provider_lines),
        is_default=True,
    )
    if key == QWEN_ENGINE_KEY:
        # This spec already IS the Qwen engine. See the module docstring.
        return (default,), ""
    return (default, qwen_entry()), ""


def render_engines_block(
    entries: "tuple[EngineEntry, ...]",
    *,
    indent: str,
    step: int,
    header: "tuple[str, ...]" = (),
) -> "list[str]":
    """The block's line BODIES (no line endings), ready to splice into a file.

    ``indent`` is the indentation of the sibling keys the block joins (that of
    ``spec.claude``); ``step`` is the file's own nesting increment, taken from
    the file rather than assumed, so an oddly-indented spec stays consistent
    with itself.
    """
    pad = " " * max(step, 1)
    key_indent = indent + pad
    field_indent = key_indent + pad
    child_indent = field_indent + pad

    out: list[str] = [f"{indent}# {line}".rstrip() for line in header]
    out.append(f"{indent}engines:")
    for entry in entries:
        out.append(f"{key_indent}{entry.key}:")
        out.append(f"{field_indent}harness: {entry.harness}")
        out.append(f"{field_indent}model: {entry.model}")
        if entry.provider_lines:
            out.append(f"{field_indent}provider:")
            out.extend(f"{child_indent}{line}" for line in entry.provider_lines)
        elif entry.provider_scalar is not None:
            out.append(f"{field_indent}provider: {entry.provider_scalar}")
        if entry.reasoning_effort:
            out.append(f"{field_indent}reasoning_effort: {entry.reasoning_effort}")
        if entry.max_context_tokens:
            out.append(f"{field_indent}max_context_tokens: {entry.max_context_tokens}")
        if entry.is_default:
            out.append(f"{field_indent}default: true")
    return out
