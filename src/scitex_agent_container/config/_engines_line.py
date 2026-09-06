"""Give ONE spec an ``engines:`` block, as a TEXT edit that keeps its comments.

Operator, Telegram 2026-09-05:「一気に書き換えるコマンドめちゃくちゃ怖いので、
ちゃんと git で管理してくださいね。大体置換のコードっていつも失敗するんですよね。」
— a command that rewrites everything at once is frightening, keep it in git,
bulk-replacement code always fails. Two properties answer that, and both are
in this module rather than in the CLI that calls it.

**COMMENTS SURVIVE.** 29 of the 119 fleet specs carry ``#`` comments;
``handyman-08`` alone carries 168. They are operator rulings and measured
numbers — the only record of WHY a value is what it is. A
``yaml.safe_load`` + ``yaml.dump`` round-trip destroys every one, so there is
none: the edit is line surgery through :mod:`._yaml_line_edit`, the same
convention ``_a2a_host_line`` and ``_to_home_layers_line`` follow, and every
line the migration does not deliberately rewrite comes out byte-identical.
:func:`_verify` re-counts the comments afterwards and refuses the edit if one
went missing, so the property is checked per spec rather than argued once.

**THE EDIT CHECKS ITSELF.** Before returning, the new text is re-parsed and
put through the production readers: :func:`parse_engines`,
:func:`default_engine`, :func:`validate_engines` (which includes
``legacy_conflict_messages``), and a before/after ``validate_raw`` comparison.
The default engine's effective backend triple must equal the triple the spec
resolved BEFORE the edit. An edit that fails any of those is a REFUSAL naming
what failed — never a written file.

WHAT IS REWRITTEN, and why only these two lines. The engines block supersedes
the legacy single-backend reading of ``spec.claude.model`` and
``spec.claude.provider``, so both are EMPTIED — the explicit-spec ruling
(2026-07-21) requires the keys to stay written, so "removed" means "present
and stating nothing", exactly as the operator's own migrated ``business``
spec spells it. Leaving a value in either one builds a wall: the day
``default: true`` moves to the Qwen entry, a stated legacy model becomes a
hard load error from ``legacy_conflict_messages``.

``spec.harness`` is NOT emptied. It is the one legacy field the engine entry
restates verbatim, so the two AGREE — the case ``_engine_types`` blesses
explicitly ("BOTH, agreeing → accepted, silently") — and it stays readable by
every reader that asks a raw spec what harness it declares.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

import yaml

from ._engine_types import (
    ENGINES_KEY,
    EngineError,
    default_engine,
    parse_engines,
    select_engine,
)
from ._engine_validation import validate_engines
from ._engines_derive import derive_entries, render_engines_block
from ._harness_types import LEGACY_HARNESS_KEY, resolve_spec_harness
from ._provider_parse import provider_identity
from ._qwen_gateway import QWEN_ENGINE_KEY, QWEN_GATEWAY_URL_ENV
from ._yaml_line_edit import (
    find_block,
    find_key,
    is_skippable,
    last_content_line,
    parse_key_line,
    split_ending,
)

__all__ = [
    "EnginesEdit",
    "REFUSED_ALREADY_DECLARED",
    "REFUSED_EMPTY_CLAUDE",
    "REFUSED_EMPTY_MODEL",
    "REFUSED_INLINE_CLAUDE",
    "REFUSED_LEGACY_HARNESS_ALIAS",
    "REFUSED_NO_CLAUDE_BLOCK",
    "REFUSED_NO_MODEL",
    "REFUSED_NO_SPEC_BLOCK",
    "REFUSED_PROVIDER_COMMENT",
    "REFUSED_PROVIDER_SHAPE",
    "REFUSED_PROXY",
    "REFUSED_TRAILING_COMMENT",
    "REFUSED_UNNAMEABLE",
    "REFUSED_UNPARSABLE",
    "REFUSED_VERIFY_FAILED",
    "lost_comment_lines",
    "migrate_engines_block",
]

# Refusal reasons. CONSTANT strings with nothing interpolated, so a 119-spec
# sweep can group by reason and print one line per KIND of refusal instead of
# one per spec. The variable half travels in ``EnginesEdit.detail``.
REFUSED_ALREADY_DECLARED = "already declares spec.engines"
REFUSED_UNPARSABLE = "the spec is not parsable YAML"
REFUSED_NO_SPEC_BLOCK = "no spec: mapping to migrate"
REFUSED_PROXY = "kind: AgentProxy — spec.engines is forbidden on a proxy"
REFUSED_LEGACY_HARNESS_ALIAS = (
    "declares the deprecated spec.provider harness alias; rename it to "
    "spec.harness before migrating"
)
REFUSED_NO_CLAUDE_BLOCK = "no spec.claude block to derive the backend from"
REFUSED_INLINE_CLAUDE = "spec.claude has an inline value, not a block"
REFUSED_EMPTY_CLAUDE = "spec.claude has no child keys"
REFUSED_NO_MODEL = "spec.claude has no model: line"
REFUSED_EMPTY_MODEL = (
    "spec.claude.model states no model, so there is no backend to name as "
    "the default engine"
)
REFUSED_TRAILING_COMMENT = (
    "spec.claude.model carries a trailing comment the edit would destroy"
)
REFUSED_PROVIDER_COMMENT = (
    "spec.claude.provider carries a comment the edit would destroy"
)
REFUSED_PROVIDER_SHAPE = (
    "spec.claude.provider has a child line indented less than its first "
    "child, a shape this edit will not re-indent blind"
)
REFUSED_UNNAMEABLE = "the model yields no usable engine key"
REFUSED_VERIFY_FAILED = "the migrated text failed its own verification"

_MODEL_MARK = "# explicit-empty (2026-07-21 rule): the engines carry the models"
_PROVIDER_MARK = "# explicit-empty (2026-07-21 rule): the engines carry the providers"

_HEADER_PAIR = (
    "spec.engines (written by `sac agents migrate-engines`). HARNESS and ENGINE",
    "are separate axes. The DEFAULT entry below is this spec's own backend,",
    f"restated verbatim; `{QWEN_ENGINE_KEY}` is the fleet-gateway alternate, picked",
    f"at start with `--engine {QWEN_ENGINE_KEY}`. The gateway ADDRESS is NOT written",
    "here: the provider NAME resolves through config/_provider_registry, and is",
    f"overridable per host with ${QWEN_GATEWAY_URL_ENV}.",
)
_HEADER_SOLO = (
    "spec.engines (written by `sac agents migrate-engines`). HARNESS and ENGINE",
    "are separate axes. This spec ALREADY runs the gateway model, so its own",
    "backend is the engine, restated verbatim — no second entry could carry the",
    "same key, and repointing this one at another endpoint would be a behaviour",
    "change rather than a migration.",
)


@dataclass(frozen=True)
class EnginesEdit:
    """The outcome of one spec's edit — the ``LineEdit`` shape, plus provenance.

    ``text`` / ``changed`` / ``reason`` are the contract
    ``_maintenance._spec_sweep_plan`` consumes. ``reason`` is None exactly
    when ``changed`` is True. ``detail`` carries the per-spec half of a
    refusal that ``reason`` deliberately does not, so refusals stay groupable
    while nothing is lost.
    """

    text: str
    changed: bool
    reason: "str | None" = None
    detail: str = ""
    engine_keys: "tuple[str, ...]" = ()
    default_key: str = ""


def _refuse(text: str, reason: str, detail: str = "") -> EnginesEdit:
    return EnginesEdit(text, False, reason, detail)


def _split_value_comment(value: str) -> "tuple[str, str]":
    """Split a key line's value into ``(value, trailing comment)``.

    Quote-aware, because a ``#`` inside a quoted scalar is data. Anything this
    cannot read confidently is returned as a comment, which makes the caller
    REFUSE rather than silently discard text.
    """
    value = value.strip()
    if not value or value.startswith("#"):
        return "", value
    if value[0] in ("'", '"'):
        quote = value[0]
        i = 1
        while i < len(value):
            char = value[i]
            if char == "\\" and quote == '"':
                i += 2
                continue
            if char == quote:
                if quote == "'" and value[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                i += 1
                break
            i += 1
        return value[:i], value[i:].strip()
    cut = value.find(" #")
    if cut == -1:
        return value, ""
    return value[:cut].rstrip(), value[cut:].strip()


def _dominant_ending(lines: "list[str]") -> str:
    for raw in lines:
        ending = split_ending(raw)[1]
        if ending:
            return ending
    return "\n"


def _insertion_point(bodies: "list[str]", key_line: int, floor: int) -> int:
    """Where the block goes: above the comment run that introduces ``key_line``.

    Inserting directly above the key would wedge the new block between a
    comment and the key it explains. Walking back over the contiguous blank
    and comment run keeps every comment attached to what it describes, and
    lands the block after the previous sibling — the placement the operator's
    own migrated spec uses.

    A COMMENT GLUED TO THE PREVIOUS CONTENT LINE IS NOT AN INTRODUCTION. It
    is that block's own trailing note (``# ^ the overlay above is per-agent
    ...``), and hoisting it above 20 lines of engines block makes it a note
    about the wrong key. Nothing downstream would catch that: ``_verify``
    compares MULTISETS of comment text, so a comment that MOVED is invisible
    to it — only a DELETED one is caught. The blank line is the tell: a
    comment separated from what precedes it introduces what follows, and a
    comment touching it belongs to it.
    """
    index = key_line
    while index - 1 >= floor and is_skippable(bodies[index - 1]):
        index -= 1
    if index > floor and not is_skippable(bodies[index - 1]):
        while index < key_line and bodies[index].strip().startswith("#"):
            index += 1
    return index


def _provider_text(bodies, claude_doc):
    """Lift ``spec.claude.provider`` verbatim: ``(scalar, lines, span, reason)``.

    ``span`` is the half-open line range the legacy value occupies, or None
    when there is nothing to empty. ``reason`` is non-empty on a refusal.
    """
    declared = claude_doc.get("provider")
    block = find_block(bodies, ("spec", "claude", "provider"))
    if block is None:
        return None, (), None, ""
    if block.inline_value is not None:
        value, comment = _split_value_comment(block.inline_value)
        if comment:
            return None, (), None, REFUSED_PROVIDER_COMMENT
        if declared is None:
            return None, (), None, ""
        span = (block.key_line, block.key_line + 1)
        return value, (), span, ""
    end = last_content_line(bodies, block.start, block.stop)
    if end is None or not isinstance(declared, dict):
        return None, (), None, ""
    children: list[str] = []
    base: "str | None" = None
    for i in range(block.start, end + 1):
        body = bodies[i]
        if body.strip().startswith("#"):
            return None, (), None, REFUSED_PROVIDER_COMMENT
        if not body.strip():
            # A blank line INSIDE the block, kept so the restated block is
            # what "verbatim" claims. ``last_content_line`` already excluded
            # the trailing run, so this is only ever an interior one.
            children.append("")
            continue
        indent = body[: len(body) - len(body.lstrip(" \t"))]
        if base is None:
            base = indent
        if not indent.startswith(base):
            return None, (), None, REFUSED_PROVIDER_SHAPE
        # RELATIVE to the block's first child, not stripped bare. Stripping
        # every child FLATTENED a nested mapping — `extra_headers:` with two
        # keys under it came out as three siblings, one of them null — while
        # the original block was deleted and `_backend_drift` (which compares
        # only base_url and auth_token_env through ``provider_identity``)
        # reported no change.
        children.append(body[len(base) :])
    return None, tuple(children), (block.key_line, end + 1), ""


def migrate_engines_block(text: str, *, path: str = "<spec>") -> EnginesEdit:
    """Add ``spec.engines`` to one spec's TEXT. Returns the edit or a refusal.

    When ``changed`` is False the text comes back BYTE-IDENTICAL and
    ``reason`` names which case applies. Idempotent: a spec that already
    declares the block is refused as ``already declares spec.engines``, so a
    second run over a migrated fleet writes nothing.
    """
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return _refuse(text, REFUSED_UNPARSABLE, str(exc))
    if not isinstance(doc, dict) or not isinstance(doc.get("spec"), dict):
        return _refuse(text, REFUSED_NO_SPEC_BLOCK)
    spec, kind = doc["spec"], str(doc.get("kind") or "Agent")
    if kind == "AgentProxy":
        return _refuse(text, REFUSED_PROXY)
    if ENGINES_KEY in spec:
        return _refuse(text, REFUSED_ALREADY_DECLARED)
    if LEGACY_HARNESS_KEY in spec:
        return _refuse(text, REFUSED_LEGACY_HARNESS_ALIAS)
    claude_doc = spec.get("claude")
    if not isinstance(claude_doc, dict):
        return _refuse(text, REFUSED_NO_CLAUDE_BLOCK)

    lines = text.splitlines(keepends=True)
    bodies = [split_ending(raw)[0] for raw in lines]
    spec_block = find_block(bodies, ("spec",))
    claude = find_block(bodies, ("spec", "claude"))
    if spec_block is None or claude is None:
        return _refuse(text, REFUSED_NO_CLAUDE_BLOCK)
    if claude.inline_value is not None:
        return _refuse(text, REFUSED_INLINE_CLAUDE)
    if claude.child_indent is None:
        return _refuse(text, REFUSED_EMPTY_CLAUDE)

    model_line = find_key(
        bodies, claude.start, claude.stop, claude.child_indent, "model"
    )
    if model_line is None:
        return _refuse(text, REFUSED_NO_MODEL)
    parsed_model = parse_key_line(bodies[model_line])
    model_text, model_comment = _split_value_comment(
        parsed_model.value if parsed_model else ""
    )
    if model_comment:
        return _refuse(text, REFUSED_TRAILING_COMMENT)
    if not str(claude_doc.get("model") or "").strip():
        return _refuse(text, REFUSED_EMPTY_MODEL)

    scalar, child_lines, provider_span, reason = _provider_text(bodies, claude_doc)
    if reason:
        return _refuse(text, reason)

    entries, why = derive_entries(
        harness=resolve_spec_harness(spec),
        model=model_text,
        provider_declared=claude_doc.get("provider"),
        provider_scalar=scalar,
        provider_lines=child_lines,
    )
    if why:
        return _refuse(text, REFUSED_UNNAMEABLE, why)

    step = len(claude.child_indent) - len(claude.indent)
    header = _HEADER_PAIR if len(entries) > 1 else _HEADER_SOLO
    block = render_engines_block(
        entries, indent=claude.indent, step=step, header=header
    )
    ending = _dominant_ending(lines)

    new_lines = list(lines)
    edits = [
        (
            model_line,
            model_line + 1,
            f"{claude.child_indent}model: ''   {_MODEL_MARK}{ending}",
        )
    ]
    if provider_span is not None:
        edits.append(
            (
                provider_span[0],
                provider_span[1],
                f"{claude.child_indent}provider: null   {_PROVIDER_MARK}{ending}",
            )
        )
    # Highest index FIRST. `provider:` sits after `model:` in most specs and
    # before it in some, and its block is several lines long — applying them
    # in file order would have the first edit silently shift the second one's
    # span onto whatever line happened to move into it.
    for start, stop, replacement in sorted(edits, key=lambda edit: -edit[0]):
        new_lines[start:stop] = [replacement]
    at = _insertion_point(bodies, claude.key_line, spec_block.key_line + 1)
    if at > 0 and bodies[at - 1].strip():
        # One blank line so the block reads as its own section rather than as
        # a continuation of whatever key happens to precede it.
        block = [""] + block
    new_lines[at:at] = [f"{line}{ending}" for line in block]
    new_text = "".join(new_lines)

    problem = _verify(text, new_text, doc, kind, entries, path)
    if problem:
        return _refuse(text, REFUSED_VERIFY_FAILED, problem)
    return EnginesEdit(
        new_text,
        True,
        engine_keys=tuple(entry.key for entry in entries),
        default_key=entries[0].key,
    )


def _comment_counts(text: str) -> Counter:
    return Counter(
        line.strip() for line in text.splitlines() if line.strip().startswith("#")
    )


def lost_comment_lines(before: str, after: str) -> "list[str]":
    """Comment lines present in ``before`` and missing from ``after``.

    Public so the guard is testable as a unit. It compares MULTISETS of
    comment TEXT, which catches a deleted comment and — deliberately — not a
    moved one: keeping a comment attached to the key it describes is
    :func:`_insertion_point`'s job, and a check that cannot tell the two
    apart would report a false loss on every legitimate reflow.
    """
    return sorted(_comment_counts(before) - _comment_counts(after))


def _verify(before, after, old_doc, kind, entries, path) -> str:
    """Everything that must hold. Returns "" when it all does.

    Each check answers a way this edit could be wrong that reading the diff
    would not reveal: the block might not parse, it might parse into an
    engine set the loader rejects, it might change what the agent starts on,
    or it might have eaten a comment.
    """
    try:
        new_doc = yaml.safe_load(after)
    except yaml.YAMLError as exc:
        return f"the migrated text does not parse as YAML: {exc}"
    if not isinstance(new_doc, dict) or not isinstance(new_doc.get("spec"), dict):
        return "the migrated text has no spec: mapping"
    new_spec = new_doc["spec"]

    engines = parse_engines(new_spec)
    expected = [entry.key for entry in entries]
    if list(engines) != expected:
        return f"parse_engines returned {list(engines)}, expected {expected}"
    errors = validate_engines(new_spec, kind)
    if errors:
        return "validate_engines rejected the block: " + "; ".join(errors)
    try:
        chosen = default_engine(engines)
    except EngineError as exc:
        return f"default_engine refused the block: {exc}"
    if chosen is None or chosen.key != expected[0]:
        return f"the default engine is {chosen and chosen.key!r}, not {expected[0]!r}"
    if QWEN_ENGINE_KEY in expected:
        picked = select_engine(engines, QWEN_ENGINE_KEY)
        if picked is None or picked.key != QWEN_ENGINE_KEY:
            return f"select_engine could not pick {QWEN_ENGINE_KEY!r}"
        if picked.provider is None or not picked.provider.base_url:
            return f"the {QWEN_ENGINE_KEY!r} entry resolves to no endpoint"

    drift = _backend_drift(old_doc["spec"], chosen)
    if drift:
        return drift

    lost = lost_comment_lines(before, after)
    if lost:
        return "comment line(s) lost: " + "; ".join(lost)

    from ._validation import validate_raw

    added = set(validate_raw(new_doc, path)) - set(validate_raw(old_doc, path))
    if added:
        return "the edit introduced validation error(s): " + "; ".join(sorted(added))
    return ""


def _backend_drift(old_spec, engine) -> str:
    """Does the default engine start the SAME backend the spec started before?

    The one property that makes this migration safe to run over 119 files: it
    changes what a spec SAYS, never what an agent RUNS. Compared through the
    production readers, and the provider through ``provider_identity`` so a
    registry name and the dict it stands for are correctly equal.
    """
    old_harness = resolve_spec_harness(old_spec)
    if engine.harness != old_harness:
        return f"harness would change: {old_harness!r} -> {engine.harness!r}"
    old_claude = old_spec.get("claude") or {}
    old_model = str(old_claude.get("model") or "").strip()
    if engine.model != old_model:
        return f"model would change: {old_model!r} -> {engine.model!r}"
    old_provider = provider_identity(old_claude.get("provider"))
    new_provider = provider_identity(engine.provider_declared)
    if old_provider != new_provider:
        return f"provider would change: {old_provider!r} -> {new_provider!r}"
    return ""
