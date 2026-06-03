"""SAC-side bind translate for SAC-from-SAC inline-spec spawns.

PR-2 of the SAC-from-SAC hardening series. PR-1 (merged b9d97ff)
installed an always-on host-stat preflight on ``spec.apptainer.binds``
that hard-rejects any bind whose source isn't host-visible. That
preflight is the safety valve. THIS module is the **opt-in
convenience** that makes the common SAC-from-SAC case Just Work:
when a parent agent spawns a child via ``POST /agents`` and the
child spec carries verbatim in-SIF paths (e.g. ``/work/data/X``
which only resolves inside the parent's container view), the host
rewrites those sources to the parent's host-side equivalent BEFORE
the preflight runs. PR-1 still fires on any bind PR-2 can't
translate — there is no silent path back to the FATAL.

The motivating case is the same one PR-1 surfaces: the clew
``capsule-0201225`` incident on 2026-06-02. The clew launcher,
running inside its own SIF where ``/work`` was bound from
``$HOME/proj/paper-scitex-clew``, POSTed a child spec to SAC with
binds like ``/work/data/cohort_a_corebench/.../capsule-0201225``.
Inside the launcher's view that path exists; on the SAC host it
does not. PR-1 caught it (after this PR ships, anyway) — PR-2
*translates* it: the launcher's parent record carries
``$HOME/proj/paper-scitex-clew:/work``, so SAC reverse-maps
``/work`` → ``$HOME/proj/paper-scitex-clew`` and rewrites the
child's source to
``$HOME/proj/paper-scitex-clew/data/cohort_a_corebench/.../capsule-0201225``.
That stat()s. Spawn succeeds.

Design constraints (kept tight so PR-1 stays the SoT for "is this
bind safe?"):

  * **Read-only.** This module only computes; it never writes
    state, never logs to the runtime dir, never touches the disk.
    The translated spec dict is returned; the call site replaces
    the in-memory spec before passing it to PR-1.
  * **Best-effort.** Any failure to look up the parent (caller is
    unknown / has no apptainer.binds / config load raises) collapses
    to no-op. PR-1 then catches whatever leaked through.
  * **Caller-shape preserving.** A bind submitted as a string comes
    back as a string; a bind submitted as a ``{src, dst, mode}``
    dict comes back as a dict (with ``src`` rewritten). Reason: the
    inline-spec POST handler writes the raw input verbatim to disk
    via ``yaml.safe_dump``; preserving the operator's chosen form
    keeps the on-disk YAML legible.
  * **Longest-prefix match.** If the parent has multiple binds whose
    container destinations are nested (e.g. ``/work`` and
    ``/work/data``), the more specific one wins. This matches how
    apptainer itself resolves a path inside the container.
  * **Boundary-safe prefix.** ``/work`` matches ``/work/X`` and the
    bare path ``/work``; it does NOT match ``/workdir/X``. A naive
    ``str.startswith`` would silently mangle ``/workdir`` paths.

Bind syntax accepted matches the parser at
``config/_parsers/_apptainer``: string ``host:container[:mode]``
with ``~``/``$VAR`` expansion already applied (the inline-spec
handler reads the raw spec but the parent's persisted spec has
expansion done at load), and the dict form ``{src, dst, mode}``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindTranslation:
    """Per-bind translation result.

    * ``original`` — the bind exactly as submitted by the caller
      (string or dict). Echoed verbatim so tests + the lineage
      audit can see the input shape.
    * ``translated`` — the bind after translation, in the SAME
      shape as ``original``. Equal to ``original`` when no
      translation applied.
    * ``was_changed`` — ``True`` iff the host source was rewritten.
      Cheap predicate for tests that want to count translations
      without diffing.
    * ``matched_prefix`` — the container-side prefix the host
      source matched against (e.g. ``"/work"``), empty when no
      rule fired.
    """

    original: Any
    translated: Any
    was_changed: bool
    matched_prefix: str


@dataclass(frozen=True)
class TranslateResult:
    """Aggregated translate outcome.

    * ``binds`` — per-bind result in input order.
    * ``caller_known`` — ``True`` iff the parent's spec was
      resolved + loaded. When ``False`` the translate was a no-op
      across the board (PR-1 then enforces correctness).
    * ``skipped_reason`` — short tag explaining a no-op. Values:
      ``""`` (translation actually ran), ``"no_caller"`` (POST
      body carried no ``caller`` field), ``"caller_unknown"``
      (resolve_config raised), ``"no_parent_binds"`` (caller's
      spec has empty ``apptainer.binds``).
    """

    binds: tuple[BindTranslation, ...]
    caller_known: bool
    skipped_reason: str


# ---------------------------------------------------------------------------
# Bind parsing / formatting
# ---------------------------------------------------------------------------


def _parse_bind(bind: Any) -> tuple[str, str, str, str] | None:
    """Return ``(host_src, container_dst, mode, shape)`` or ``None``.

    ``shape`` is the literal string ``"str"`` or ``"dict"`` so the
    formatter can re-emit in the caller's chosen form.

    Accepts:
      * canonical string ``host:container[:mode]``
      * dict form ``{src, dst, mode?}`` (the alt keys ``source`` /
        ``destination`` / ``target`` from the preflight module are
        NOT supported here — parent specs always normalize to
        ``{src, dst, mode}`` at load time, and the inline-spec
        POST body is expected to follow the canonical YAML schema)
    """
    if isinstance(bind, dict):
        src = bind.get("src")
        dst = bind.get("dst")
        mode = bind.get("mode", "")
        if not isinstance(src, str) or not isinstance(dst, str):
            return None
        return src, dst, str(mode or ""), "dict"
    if not isinstance(bind, str):
        return None
    parts = bind.split(":", 2)
    if len(parts) < 2:
        return None
    host_src = parts[0]
    container_dst = parts[1]
    mode = parts[2] if len(parts) == 3 else ""
    return host_src, container_dst, mode, "str"


def _format_bind(
    host_src: str, container_dst: str, mode: str, shape: str, original: Any
) -> Any:
    """Re-emit a bind in the caller's chosen shape.

    For dict form, mutate a shallow copy of ``original`` so any
    extra keys (e.g. an annotation field) survive. For string form,
    re-stringify ``host:container[:mode]``.
    """
    if shape == "dict":
        out = dict(original)  # type: ignore[arg-type]
        out["src"] = host_src
        out["dst"] = container_dst
        if mode:
            out["mode"] = mode
        elif "mode" in out and not mode:
            # Caller submitted with empty mode; preserve absence
            # rather than re-injecting "".
            del out["mode"]
        return out
    if mode:
        return f"{host_src}:{container_dst}:{mode}"
    return f"{host_src}:{container_dst}"


# ---------------------------------------------------------------------------
# Reverse map from parent's binds
# ---------------------------------------------------------------------------


def _build_reverse_map(parent_binds: list[str]) -> list[tuple[str, str]]:
    """Return ``[(container_dst, host_src), ...]`` longest-prefix first.

    A list (not dict) is returned because longest-prefix order is
    semantically meaningful — when a child bind source matches
    multiple parent container destinations (nested binds), the
    deeper one wins. The list is sorted descending by destination
    length so :func:`_translate_one` can short-circuit on the first
    matching prefix.

    Parent binds with no ``:container`` separator are silently
    dropped (defensive — the parent's spec parser would have
    rejected them, but a hand-edited disk YAML could slip through).
    """
    rules: list[tuple[str, str]] = []
    for bind in parent_binds:
        parsed = _parse_bind(bind)
        if parsed is None:
            continue
        host_src, container_dst, _mode, _shape = parsed
        if not container_dst or not host_src:
            continue
        rules.append((container_dst, host_src))
    rules.sort(key=lambda x: len(x[0]), reverse=True)
    return rules


def _is_prefix_match(host_src: str, prefix: str) -> bool:
    """``True`` iff ``prefix`` is a path-component prefix of ``host_src``.

    Boundary-safe: ``/work`` matches ``/work`` and ``/work/X`` but
    NOT ``/workdir`` or ``/work-tmp``. A naive ``startswith`` would
    silently mangle the latter.
    """
    if host_src == prefix:
        return True
    if not prefix.endswith("/"):
        return host_src.startswith(prefix + "/")
    return host_src.startswith(prefix)


def _translate_one(host_src: str, rules: list[tuple[str, str]]) -> tuple[str, str]:
    """Return ``(new_host_src, matched_prefix)``.

    ``matched_prefix`` is the empty string when no rule applied
    (= no translation; ``new_host_src == host_src``). When a rule
    applies, the prefix is replaced verbatim — trailing components
    are preserved exactly so ``Path(new_host_src).resolve()`` is
    unsurprising.
    """
    for container_dst, parent_host_src in rules:
        if _is_prefix_match(host_src, container_dst):
            tail = host_src[len(container_dst) :]
            # Strip a single leading "/" from tail so the join uses
            # exactly one slash; both halves are already absolute
            # by construction.
            new_src = parent_host_src.rstrip("/") + tail
            return new_src, container_dst
    return host_src, ""


# ---------------------------------------------------------------------------
# Public translate entry point
# ---------------------------------------------------------------------------


def translate_binds_in_spec(
    spec: dict,
    caller: str | None,
    *,
    parent_binds_lookup,
) -> tuple[dict, TranslateResult]:
    """Rewrite in-SIF bind sources in ``spec`` using parent's host map.

    Args:
        spec: the v3 Agent spec dict the inline-spec POST handler
            received. NOT mutated; a shallow + nested-shallow copy
            of the relevant slice is returned.
        caller: the POST body's ``caller`` field (the parent agent's
            name). ``None`` triggers a no-op pass-through (admin /
            operator-submitted spec — let PR-1 enforce directly).
        parent_binds_lookup: ``Callable[[str], list[str] | None]``
            that returns the parent's persisted ``apptainer.binds``
            (canonical string list) given the parent's name, or
            ``None`` when the parent isn't a known SAC-managed
            agent. Injected (not imported) so tests can wire a fake
            lookup without monkeying the global ``resolve_config``
            path.

    Returns:
        ``(translated_spec, result)``. ``translated_spec`` is the
        spec with ``spec.apptainer.binds[*].src`` rewritten where
        applicable; on no-op it is shallow-equal to the input.
        ``result`` enumerates per-bind outcomes so the call site
        can log / audit / emit observability.

    The function never raises. Defensive: any unexpected shape
    (spec not a dict, no apptainer, no binds, lookup raises)
    collapses to ``skipped_reason``-tagged no-op.
    """
    binds = _extract_binds(spec)
    if caller is None:
        return spec, TranslateResult(
            binds=tuple(_passthrough(b) for b in binds),
            caller_known=False,
            skipped_reason="no_caller",
        )
    parent_binds = _safe_lookup(parent_binds_lookup, caller)
    if parent_binds is None:
        return spec, TranslateResult(
            binds=tuple(_passthrough(b) for b in binds),
            caller_known=False,
            skipped_reason="caller_unknown",
        )
    if not parent_binds:
        return spec, TranslateResult(
            binds=tuple(_passthrough(b) for b in binds),
            caller_known=True,
            skipped_reason="no_parent_binds",
        )
    rules = _build_reverse_map(parent_binds)
    if not rules:
        # Parent had binds but none were parseable; fail soft.
        return spec, TranslateResult(
            binds=tuple(_passthrough(b) for b in binds),
            caller_known=True,
            skipped_reason="no_parent_binds",
        )
    if not binds:
        # Child spec has no apptainer.binds at all (e.g. a docker
        # runtime or a workdir-only spec). Return the input
        # unchanged — must NOT inject an empty apptainer block
        # the child never declared.
        return spec, TranslateResult(binds=(), caller_known=True, skipped_reason="")
    new_binds: list[Any] = []
    outcomes: list[BindTranslation] = []
    for raw in binds:
        parsed = _parse_bind(raw)
        if parsed is None:
            new_binds.append(raw)
            outcomes.append(_passthrough(raw))
            continue
        host_src, container_dst, mode, shape = parsed
        # Expand ~/$VAR on the host source BEFORE matching the
        # reverse map — the parent's container destinations are
        # never expanded (apptainer rejects ~ / $VAR on the dst
        # side), so the comparison happens in fully-resolved form.
        expanded_host_src = os.path.expanduser(os.path.expandvars(host_src))
        new_host_src, matched = _translate_one(expanded_host_src, rules)
        if matched:
            translated = _format_bind(new_host_src, container_dst, mode, shape, raw)
            new_binds.append(translated)
            outcomes.append(
                BindTranslation(
                    original=raw,
                    translated=translated,
                    was_changed=True,
                    matched_prefix=matched,
                )
            )
        else:
            new_binds.append(raw)
            outcomes.append(_passthrough(raw))
    new_spec = _swap_binds(spec, new_binds)
    return new_spec, TranslateResult(
        binds=tuple(outcomes),
        caller_known=True,
        skipped_reason="",
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _extract_binds(spec: dict) -> list[Any]:
    """Best-effort ``spec.spec.apptainer.binds`` extractor.

    Mirrors ``_inline_spec_preflight._iter_binds``. Defensive: any
    unexpected shape collapses to empty list so the translate is
    a no-op rather than a crash.
    """
    if not isinstance(spec, dict):
        return []
    body = spec.get("spec")
    if not isinstance(body, dict):
        return []
    apt = body.get("apptainer")
    if not isinstance(apt, dict):
        return []
    binds = apt.get("binds")
    if not isinstance(binds, list):
        return []
    return binds


def _swap_binds(spec: dict, new_binds: list[Any]) -> dict:
    """Return a shallow-copy of ``spec`` with ``apptainer.binds`` replaced.

    Only the path ``spec → spec → apptainer → binds`` is copied;
    siblings are shared by reference (the inline-spec handler writes
    the result via ``yaml.safe_dump`` immediately, so a sibling
    aliasing the original is harmless and avoids a deep-copy spike
    on large specs).
    """
    out_top = dict(spec)
    body = dict(out_top.get("spec") or {})
    apt = dict(body.get("apptainer") or {})
    apt["binds"] = new_binds
    body["apptainer"] = apt
    out_top["spec"] = body
    return out_top


def _passthrough(raw: Any) -> BindTranslation:
    """Build a no-change ``BindTranslation`` for a single bind."""
    return BindTranslation(
        original=raw, translated=raw, was_changed=False, matched_prefix=""
    )


def _safe_lookup(lookup, caller: str) -> list[str] | None:
    """Call ``lookup(caller)``; swallow any exception → ``None``."""
    # stx-allow: fallback (reason: any failure in the parent lookup —
    # missing config, unreadable YAML, validation error in load_config
    # — must NOT break the spawn; collapse to None and let PR-1
    # enforce the post-translate spec)
    try:
        result = lookup(caller)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    if result is None:
        return None
    if not isinstance(result, list):
        return None
    return [b for b in result if isinstance(b, str)]


__all__ = [
    "BindTranslation",
    "TranslateResult",
    "translate_binds_in_spec",
]
