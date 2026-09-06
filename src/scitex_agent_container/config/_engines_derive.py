"""What a migrated ``spec.engines`` block SAYS, and how it is rendered.

Pure. No YAML parsing, no line numbers, no I/O — it takes the backend a spec
already declares, as primitives, and returns the entries plus the exact lines
to write. :mod:`._engines_line` owns the text surgery that puts them in a file.

ONE ENTRY, AND IT INVENTS NOTHING. A swept spec gets exactly one engine: its
own backend, restated. The ``model`` is the spec's own ``spec.claude.model``
text copied verbatim and the ``provider`` is ``spec.claude.provider`` copied
verbatim, so a spec whose provider points at a local endpoint keeps pointing
there. ``provider: anthropic`` is written ONLY when the spec declared no
provider at all — that is the registry's own sentinel for "the default
Anthropic OAuth backend", it is what the spec already resolves to, and
writing it is how the entry avoids the other failure: an unstated provider
that silently defaults to a vendor.

NO SECOND ENTRY IS COPIED IN. The sweep used to append a whole ``qwen38-27b``
entry to every spec it touched. That definition now lives ONCE in the fleet
engine library (:mod:`._engine_library`), where ``--engine qwen38-27b``
reaches it through the merged namespace and where one ``engine:`` line moves
the fleet. 119 identical copies of a row is the shape a library exists to
delete, and a copy also stops being the definition the moment the library's
row changes.

TWO FIELDS THIS DELIBERATELY DOES NOT WRITE, both deprecated by the
harness/engine split (see :data:`._engine_types.ENGINE_ENTRY_KEYS`):

* **``harness:``** — an entry that states one claims the HARNESS axis, which
  is the coupling the split removes. The spec's own ``spec.harness`` line is
  left stated and untouched, so the harness still resolves exactly as before;
  the entry simply states no opinion about it.
* **``default: true``** — a spec-local ``default:`` OUTRANKS the fleet
  library in :mod:`._engine_precedence`, so writing it on every swept spec
  would trade 119 legacy pins for 119 engine pins and leave a fleet-wide flip
  still a 119-file edit. A block with exactly ONE entry needs no marker:
  precedence step 3 starts on it because it cannot be ambiguous.

THE ENTRY KEY. ``claude`` when the backend is the plain Anthropic OAuth path
AND the harness is the Anthropic one — the name the operator's own
already-migrated ``business`` spec uses — and a slug of the model otherwise,
because "claude" is a false name for an entry holding Qwen, a vendor name is
a claim about scope, and a Codex session is not the vendor that name states.

A KEY THE FLEET LIBRARY ALSO USES IS NOT A COLLISION. A spec already running
``qwen38-27b`` derives that key for its own entry, and
:func:`._engine_library.resolve_engine_namespace` gives the SPEC-LOCAL entry
precedence — which is the right answer: the 8 handymen reach the gateway over
a loopback forward, and repointing them at the fleet address would be a
behaviour change dressed as a migration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._harness_types import DEFAULT_AGENT_HARNESS

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


def _is_anthropic_oauth(provider_declared: object) -> bool:
    """Does this declared provider mean "the plain Anthropic OAuth path"?"""
    if provider_declared is None:
        return True
    return (
        isinstance(provider_declared, str)
        and provider_declared.strip().lower() == ANTHROPIC_PROVIDER
    )


def engine_key_for(
    model: str,
    provider_declared: object,
    harness: str = DEFAULT_AGENT_HARNESS,
) -> str:
    """The key the DEFAULT entry gets. ``""`` when the model yields no slug.

    HARNESS IS PART OF THE QUESTION. ``claude`` is the right name only for
    the Anthropic OAuth path; on a ``harness: codex`` spec it is a vendor
    name attached to something that is not that vendor, which is exactly the
    claim-about-scope failure this axis exists to remove. Such a spec gets a
    slug of its model, like every other non-Anthropic backend.
    """
    if harness == DEFAULT_AGENT_HARNESS and _is_anthropic_oauth(provider_declared):
        return CLAUDE_ENGINE_KEY
    return slug_engine_key(model)


@dataclass(frozen=True)
class EngineEntry:
    """ONE rendered engine entry. ``provider_lines`` holds the nested form.

    THE FIELDS ARE EXACTLY WHAT A MIGRATION CAN DERIVE, and no more. There is
    no ``harness`` and no ``is_default``, because both are deprecated inside
    an entry and a renderer that could HOLD them would be a renderer that
    could WRITE them (see the module docstring). There is no
    ``reasoning_effort`` and no ``max_context_tokens`` either, for a quieter
    reason: the legacy single-backend surface has no such fields, so a
    restatement of it cannot honestly carry them. They were here only to
    render the copied ``qwen38-27b`` entry, and that entry now lives in the
    fleet engine library — which declares them in YAML, where the operator
    can change them without a release.
    """

    key: str
    model: str
    #: The scalar written after ``provider:``. None when the provider is a
    #: nested block, in which case ``provider_lines`` carries its children
    #: with the block's own first-child indent removed and everything
    #: DEEPER than that kept — a nested mapping under the provider survives
    #: as a nested mapping rather than being flattened into siblings.
    provider_scalar: "str | None" = None
    provider_lines: "tuple[str, ...]" = ()


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

    EXACTLY ONE ENTRY today — the spec's own backend. The plural shape is not
    hedging: ``engines:`` is a MAPPING, the renderer below writes N of them,
    and every alternate a spec could name now comes from the fleet engine
    library rather than from a copy this function makes.

    ``harness`` is an INPUT and not an output. It decides the entry's KEY
    (``claude`` is only right on the Anthropic path) and whether stating the
    ``anthropic`` provider sentinel would be a true claim; the entry itself
    stays silent about the harness axis.
    """
    key = engine_key_for(model, provider_declared, harness)
    if not key:
        return (), "the model yields no usable engine key"

    scalar = provider_scalar
    if (
        provider_declared is None
        and not provider_lines
        and harness == DEFAULT_AGENT_HARNESS
    ):
        # Stated explicitly rather than left empty: an omitted provider reads
        # as "no opinion", and an entry with no opinion about its backend is
        # the ambiguity this axis exists to remove.
        #
        # ONLY ON THE ANTHROPIC HARNESS. ``anthropic`` is this registry's
        # sentinel for the Anthropic OAuth backend; writing it under a
        # ``harness: codex`` entry states the WRONG vendor, which is worse
        # than stating none — and the entry then reads as a claim nobody
        # made. A non-Anthropic harness with no declared provider keeps
        # exactly what the spec said: nothing.
        scalar = ANTHROPIC_PROVIDER

    return (
        EngineEntry(
            key=key,
            model=model,
            provider_scalar=scalar,
            provider_lines=tuple(provider_lines),
        ),
    ), ""


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
        out.append(f"{field_indent}model: {entry.model}")
        if entry.provider_lines:
            out.append(f"{field_indent}provider:")
            # ``rstrip`` so a blank line copied out of the original block
            # comes back blank rather than as an indent-only line, and each
            # child keeps whatever extra indentation it carried RELATIVE to
            # the block's first child — that relative depth is the nesting.
            out.extend(
                f"{child_indent}{line}".rstrip() for line in entry.provider_lines
            )
        elif entry.provider_scalar is not None:
            out.append(f"{field_indent}provider: {entry.provider_scalar}")
    return out
