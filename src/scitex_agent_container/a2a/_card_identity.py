"""Spec → operator-facing identity projection (shared by both a2a surfaces).

An agent's ROLE (headline) + RESPONSIBILITIES (bullets), plus its groups /
purpose / owned-repo, are authored in its ``spec.yaml`` and must be
discoverable fleet-wide via a2a (operator directive 2026-07-06) so a peer
can see "who does what" without asking. Two surfaces expose them:

* the AgentCard — :func:`a2a._card.project_card` merges these fields into
  the card's ``x-scitex-agent-container`` block; and
* the ``a2a peers`` rows — :func:`_listen._registry_endpoints`
  ``.resolve_agent_identity`` adds them to each ``GET /agents`` row.

Both call :func:`spec_identity` here, so the two surfaces never drift.
This module is deliberately dependency-free (no a2a SDK proto import, no
fleet imports) so the row surface can reuse it without dragging in the
card's protobuf types.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["spec_identity", "as_str_list"]


def as_str_list(value: Any) -> list[str]:
    """Coerce a CSV string / list into a clean ``list[str]`` (``[]`` else).

    Pure. A ``str`` is split on commas; a ``list``/``tuple`` is filtered
    to its non-empty stringifiable scalars. Anything else (``None``,
    ``dict``, number) yields ``[]``. Whitespace is stripped and empties
    dropped so a discovery surface never advertises a blank bullet.
    """
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, (list, dict)) or item is None:
                continue
            text = str(item).strip()
            if text:
                out.append(text)
        return out
    return []


def spec_identity(v3: dict[str, Any]) -> dict[str, Any]:
    """Project the operator-facing identity fields from a v3 spec dict.

    Pure + best-effort. Returns ONLY the keys the spec actually declares
    (omit-if-missing), so a surface advertises real data and never a
    fabricated blank:

    * ``role`` — ``metadata.labels.role``. A plain string for the common
      single-role spec; a ``list[str]`` for a (future) multi-role spec
      (the operator wants multi-role shown as bullets downstream). A
      single-element list collapses to a bare string.
    * ``responsibilities`` — a bullet ``list[str]``. Prefers the
      top-level ``spec.responsibilities``; falls back to
      ``metadata.labels.responsibilities`` (list or CSV string).
    * ``groups`` — ``metadata.labels.groups`` (list) or the singular
      ``metadata.labels.group`` (string), normalised to a ``list[str]``.
    * ``purpose`` — ``metadata.labels.purpose`` (free-text string).
    * ``project`` — basename of an explicit ``spec.workdir`` (the repo
      the agent owns / works in). Absent when no workdir is declared.
    """
    if not isinstance(v3, dict):
        return {}
    metadata = v3.get("metadata") or {}
    labels = metadata.get("labels") if isinstance(metadata, dict) else {}
    spec = v3.get("spec") or {}
    if not isinstance(labels, dict):
        labels = {}
    if not isinstance(spec, dict):
        spec = {}

    out: dict[str, Any] = {}

    # role — headline. A non-empty string is kept verbatim; a list is
    # normalised to a clean list[str] (multi-role) and a single element
    # collapses to a bare string so common consumers stay simple.
    raw_role = labels.get("role")
    if isinstance(raw_role, str) and raw_role.strip():
        out["role"] = raw_role.strip()
    elif isinstance(raw_role, (list, tuple)):
        roles = as_str_list(raw_role)
        if len(roles) == 1:
            out["role"] = roles[0]
        elif roles:
            out["role"] = roles

    # responsibilities — prefer the top-level spec list; fall back to a
    # labels entry (list or CSV) for specs that carry it there instead.
    responsibilities = as_str_list(spec.get("responsibilities"))
    if not responsibilities:
        responsibilities = as_str_list(labels.get("responsibilities"))
    if responsibilities:
        out["responsibilities"] = responsibilities

    # groups — plural list wins; singular ``group`` string as fallback.
    groups = as_str_list(labels.get("groups"))
    if not groups:
        single = labels.get("group")
        if isinstance(single, str) and single.strip():
            groups = [single.strip()]
    if groups:
        out["groups"] = groups

    # purpose — free-text one-liner.
    purpose = labels.get("purpose")
    if isinstance(purpose, str) and purpose.strip():
        out["purpose"] = purpose.strip()

    # project — the repo the agent owns (basename of an explicit workdir).
    workdir = spec.get("workdir")
    if isinstance(workdir, str) and workdir.strip():
        base = Path(workdir.strip().rstrip("/")).name
        if base:
            out["project"] = base

    return out
