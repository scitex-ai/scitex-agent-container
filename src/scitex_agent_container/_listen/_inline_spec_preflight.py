"""Pre-flight validation for inline ``POST /agents`` specs.

The historical SAC-from-SAC inline-spec spawn path (PR #261) accepted
the spec verbatim, materialised it to disk, and handed off to
``sac agents start <name>``. The handoff returned HTTP 200 immediately
on subprocess success, but the **apptainer container creation itself**
could still FATAL minutes later if any bind source did not exist on
the host filesystem — and that FATAL was silent at the HTTP layer.

This was the clew-cohort-a-capsule-0201225 failure on 2026-06-02:
the inline spec's ``apptainer.binds`` referenced ``/work/...`` paths
that exist inside the caller's SIF view but NOT on the host. POST
/agents returned 200, the agent appeared in ``/agents``, but no
session was ever started:

    WARNING: skipping mount of /work/.../capsule-0201225: stat ...
             no such file or directory
    FATAL:   container creation failed: mount hook function failure:
             mount source /work/.../capsule-0201225 doesn't exist

The operator burnt 50 minutes waiting for a session that never came.

This module adds the missing **pre-flight** that catches this BEFORE
the spec is materialised — every bind source is host-``stat()``-checked,
and any unresolved source aborts the spawn with HTTP 400 carrying a
structured ``kind="bind_unresolvable"`` body the caller (clew launcher
and future SAC-from-SAC clients) can branch on without parsing prose.

The validator is deliberately read-only. It does NOT translate paths
(that's PR-2's job — SAC-side ``/work``-prefix rewriting). The split
keeps two concerns testable in isolation:

  * THIS PR — fail-loud rejection (always-on safety valve)
  * PR-2   — bind translate (an opt-in convenience that reduces the
            burden on callers; if it ever has a gap, this validator
            still catches the leak)

Never raises. Walks the spec dict defensively (any unexpected shape
becomes a structured error). The bind syntax accepted matches
``runtimes/_apptainer_runtime``: ``host_path:container_path[:mode]``
strings, with ``~`` and ``$VAR`` expansion on the host side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BindCheck:
    """Per-bind preflight result entry.

    * ``bind`` — the raw bind string as it appeared in the spec
      (``host_path:container_path[:mode]``). Echoed verbatim so the
      caller can spot which line failed without parsing.
    * ``host_resolved`` — the resolved host-side source after ``~``/
      ``$VAR`` expansion; what the validator actually stat()ed.
    * ``exists_on_host`` — ``True`` iff the resolved path exists.
    * ``container_dest`` — the container-side mount point. Useful for
      remediation hints (which dir inside the SIF this was supposed
      to land at).
    """

    bind: str
    host_resolved: str
    exists_on_host: bool
    container_dest: str


@dataclass(frozen=True)
class PreflightResult:
    """Aggregated preflight outcome.

    * ``ok`` — ``True`` iff every checked bind has ``exists_on_host=True``.
    * ``checks`` — every per-bind result (in input order; useful when
      multiple binds fail and the caller wants the full list).
    * ``unresolvable`` — convenience subset of ``checks`` filtered to
      the failing entries, so a JSON response can carry just the bad
      ones without recomputation.
    """

    ok: bool
    checks: tuple[BindCheck, ...]
    unresolvable: tuple[BindCheck, ...]


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _expand_host_path(raw_src: str) -> str:
    """Apply ``~``/``$VAR`` expansion on the host side only.

    Matches ``config/_parsers/_apptainer._expand_bind_src`` so a bind
    that the spec parser accepts is the same string this validator
    stats. The container destination is *not* expanded (env on the
    in-SIF side is irrelevant for host filesystem visibility).
    """
    expanded = os.path.expanduser(raw_src)
    expanded = os.path.expandvars(expanded)
    return expanded


def _parse_bind(bind: Any) -> tuple[str, str] | None:
    """Return ``(host_src, container_dst)`` or ``None`` if unparseable.

    Accepts the apptainer-runtime canonical string form
    ``host:container[:mode]``. Dict forms (``{src, dst, mode}``) are
    normalised to this shape by the spec parser at materialise time;
    we accept both here so the preflight survives a caller passing
    the dict form directly.
    """
    if isinstance(bind, dict):
        src = bind.get("src") or bind.get("source")
        dst = bind.get("dst") or bind.get("destination") or bind.get("target")
        if not isinstance(src, str) or not isinstance(dst, str):
            return None
        return src, dst
    if not isinstance(bind, str):
        return None
    parts = bind.split(":", 2)
    if len(parts) < 2:
        return None  # malformed; need at least host:container
    return parts[0], parts[1]


def _iter_binds(spec: dict) -> list[Any]:
    """Best-effort extraction of ``spec.apptainer.binds`` from a v3 spec.

    Defensive: any unexpected shape collapses to an empty list. The
    caller can still preflight ``volumes`` separately if needed (a
    follow-up); the current threat model is the apptainer binds path
    which is what the clew failure exercised.
    """
    if not isinstance(spec, dict):
        return []
    spec_body = spec.get("spec")
    if not isinstance(spec_body, dict):
        return []
    apt = spec_body.get("apptainer")
    if not isinstance(apt, dict):
        return []
    binds = apt.get("binds")
    if not isinstance(binds, list):
        return []
    return binds


# ---------------------------------------------------------------------------
# Public preflight
# ---------------------------------------------------------------------------


def preflight_bind_sources(spec: dict) -> PreflightResult:
    """Validate ``spec.apptainer.binds`` against the host filesystem.

    Returns a :class:`PreflightResult` enumerating every bind, with
    each entry's ``exists_on_host`` flag set from ``Path(host_src).exists()``.
    The aggregate ``ok`` is ``True`` only when every checked bind
    resolved.

    Specs with no binds (or unparseable shape) collapse to ``ok=True``
    with an empty ``checks`` tuple — there is nothing to reject.

    Path expansion: ``~`` and ``$VAR`` are applied to the host side
    only (mirrors ``config/_parsers/_apptainer._expand_bind_src``).
    Symlinks are followed (the spec parser does the same; broken
    symlinks count as non-existent here too).
    """
    binds = _iter_binds(spec)
    checks: list[BindCheck] = []
    unresolvable: list[BindCheck] = []
    for raw in binds:
        parsed = _parse_bind(raw)
        if parsed is None:
            # An un-parseable entry is loud — the host can't even tell
            # what to check. Record it with an empty resolved path
            # and exists=False so the caller sees the malformed line.
            entry = BindCheck(
                bind=str(raw),
                host_resolved="",
                exists_on_host=False,
                container_dest="",
            )
            checks.append(entry)
            unresolvable.append(entry)
            continue
        host_src, container_dst = parsed
        resolved = _expand_host_path(host_src)
        exists = Path(resolved).exists()
        entry = BindCheck(
            bind=str(raw),
            host_resolved=resolved,
            exists_on_host=exists,
            container_dest=container_dst,
        )
        checks.append(entry)
        if not exists:
            unresolvable.append(entry)
    return PreflightResult(
        ok=not unresolvable,
        checks=tuple(checks),
        unresolvable=tuple(unresolvable),
    )


def preflight_failure_response_body(result: PreflightResult) -> dict:
    """Build the HTTP 400 JSON body for a failed preflight.

    Stable wire shape (clew launcher + future SAC-from-SAC clients
    branch on ``kind`` not prose):

    .. code-block:: json

       {
         "error": "bind source(s) not visible from host",
         "kind": "bind_unresolvable",
         "details": {
           "binds": [
             {
               "source":          "/work/x",
               "host_normalized": "/work/x",        // omitted when == source
               "container_path":  "/x",
               "exists_on_host":  false
             }
           ],
           "translation_hint": "rewrite source(s) to host-visible paths ..."
         }
       }

    Field naming per clew review (#287 WIP):
      * ``binds`` (was ``unresolvable``) — always a LIST so 49-capsule
        callers see EVERY miss in one round-trip, not just the first.
      * ``source`` (was ``bind``) — the raw spec entry the operator
        wrote.
      * ``host_normalized`` (was always-emitted ``host_resolved``) —
        ONLY emitted when ``~``/``$VAR`` expansion changed the source.
        Saves wire bytes + makes "no normalisation happened" cleanly
        distinguishable from "normalisation produced the same path".
      * ``container_path`` (was ``container_dest``) — name aligned with
        the rest of the apptainer-bind nomenclature (config parsers
        already call it ``container_path``).
      * ``translation_hint`` (was ``remediation_hint``) — names what
        the operator actually needs to DO (translate the path), not a
        generic "remediation".

    Callers MUST treat ``kind`` as the branch key; the prose ``error``
    is for humans only.
    """
    entries: list[dict] = []
    for c in result.unresolvable:
        entry: dict = {
            "source": c.bind,
            "container_path": c.container_dest,
            "exists_on_host": c.exists_on_host,
        }
        # Emit ``host_normalized`` ONLY when the host-side path was
        # actually rewritten by ``~``/``$VAR`` expansion. Compare
        # against the RAW HOST PORTION of the bind (not the whole
        # ``host:container[:mode]`` string, which would always differ
        # from the bare host_resolved path). Parsing here keeps
        # BindCheck minimal — no extra field for callers to know
        # about.
        parsed = _parse_bind(c.bind)
        raw_host = parsed[0] if parsed is not None else ""
        if c.host_resolved and c.host_resolved != raw_host:
            entry["host_normalized"] = c.host_resolved
        entries.append(entry)
    return {
        "error": "bind source(s) not visible from host",
        "kind": "bind_unresolvable",
        "details": {
            "binds": entries,
            "translation_hint": (
                "rewrite each bind source to a path that exists on the "
                "host (not the caller's in-SIF /work view), or wait for "
                "PR-2 (SAC-side bind translate) to ship and re-spawn."
            ),
        },
    }


__all__ = [
    "BindCheck",
    "PreflightResult",
    "preflight_bind_sources",
    "preflight_failure_response_body",
]
