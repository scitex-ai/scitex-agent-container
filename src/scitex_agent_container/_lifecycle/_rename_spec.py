#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Rewrite an agent spec's SELF-REFERENCES when the agent is renamed.

A spec names itself in more places than the directory it lives in. Miss
one and the agent starts under the new name but keeps writing to the old
agent's overlay / state DB / board identity. This module owns the
EXHAUSTIVE list of those places and rewrites them by LOADING the document
and changing the known fields — never by running a regex over the file.

Touchpoints (the SSOT — :data:`SPEC_TOUCHPOINTS` documents them for
``--dry-run``):

  1. ``metadata.labels.project``            token
  2. ``metadata.labels.purpose``            token (``<name>-maintainer``)
  3. ``spec.workdir``                       path component
  4. ``spec.apptainer.overlay``             path component
  5. ``spec.apptainer.binds[i]``            path component (src + dst)
  6. ``spec.apptainer.raw_args`` ``--overlay <path>`` / ``--overlay=<path>``
  7. ``spec.apptainer.raw_args`` ``--env K=V`` for K in :data:`ENV_RULES`
  8. ``spec.apptainer.env.<K>`` for K in :data:`ENV_RULES`

``ENV_RULES`` is where the DAMAGING one lives: the agent's identity ON
THE BOARD. Change it without migrating the cards and every card the agent
owns is orphaned — see ``_rename_cards``. FAILING to change it is the
mirror-image damage: the renamed agent keeps writing under its former
name, and the board cannot say who those cards belong to.

That identity is spelled ``SCITEX_CARDS_AGENT_ID`` today and
``SCITEX_TODO_AGENT_ID`` in specs written before the rename. BOTH are in
``ENV_RULES`` because both are live in the fleet — 108 specs and 193
specs respectively, measured 2026-08-19. Only the old one was listed
until then, so renaming any of the 108 silently produced exactly the
mirror-image damage above.

Why ruamel round-trip and not PyYAML load+dump: specs carry load-bearing
operator commentary (the live ``scitex-todo`` spec is ~40% comments
explaining WHY each flag is set). ``ruamel.yaml>=0.18`` is already a hard
dependency of sac for exactly this reason — see ``pyproject.toml``:
"operator-authored comments + key order in config.yaml survive a sac-side
edit". The same rule holds, harder, for a spec.

Why that is still not enough, and what guards it: a round-trip dump can
in principle reformat content we never meant to touch. So
:func:`rewrite_spec` re-parses its own output and asserts that the
SEMANTIC diff against the input is exactly the set of changes it planned
(:func:`_assert_only_planned_changes`). A rewrite that moved anything
else raises rather than returning a subtly-corrupted spec.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterator

# Env vars whose VALUE identifies the agent, and how to rewrite each.
#
#   "identity" — the whole value IS the agent name.
#   "path"     — the value is a path with the agent name as a component
#                (e.g. SCITEX_AGENT_CONTAINER_STATE_DB=/state/<name>/state.db).
#   "scope"    — the value is the board scope string ``agent:<name>``.
#
# SCITEX_TODO_AGENT_ID is the board identity: the shared ``.mcp.json``
# expands ``${SCITEX_TODO_AGENT_ID}`` and every card the agent writes is
# attributed to it. SCITEX_TODO_AGENT is its deprecated alias
# (scitex_todo._store.ENV_AGENT_DEPRECATED) — still honoured, so still
# renamed.
ENV_RULES: dict[str, str] = {
    "SCITEX_TODO_AGENT_ID": "identity",
    # THE CURRENT SPELLING, and it was missing. Measured 2026-08-19 on
    # compute-04: 108 specs declare SCITEX_CARDS_AGENT_ID and 193 still
    # declare SCITEX_TODO_AGENT_ID. Only the old name was listed here, so
    # renaming any of those 108 agents left the board identity pointing at
    # the agent's FORMER name — and every card it then wrote was attributed
    # to an agent that no longer exists. Exactly the damage the module
    # docstring above warns this dict causes when it is wrong.
    #
    # BOTH are listed on purpose while both populations exist. This is not a
    # compatibility fallback to be tidied away: it is the rename tool having
    # to recognise what is ACTUALLY IN THE SPECS. Drop the old key only when
    # no spec declares it — the same condition _board_identity_env.py states
    # for dropping the legacy injection, and it is the same 193 specs.
    "SCITEX_CARDS_AGENT_ID": "identity",
    "SCITEX_TODO_AGENT": "identity",
    "SAC_NAME": "identity",
    "SCITEX_AGENT_CONTAINER_NAME": "identity",
    "SCITEX_AGENT_CONTAINER_AGENT": "identity",
    "SAC_AGENT": "identity",
    "SCITEX_AGENT_CONTAINER_STATE_DB": "path",
    "SCITEX_TODO_SCOPE": "scope",
}

# Human-readable touchpoint list, surfaced by ``sac agents rename --help``
# and the --dry-run header so the operator can see WHAT is in scope
# without reading this module.
SPEC_TOUCHPOINTS: tuple[str, ...] = (
    "metadata.labels.project",
    "metadata.labels.purpose",
    "spec.workdir",
    "spec.apptainer.overlay",
    "spec.apptainer.binds[]",
    "spec.apptainer.raw_args[] --overlay <path>",
    f"spec.apptainer.raw_args[] --env <K>= for K in {sorted(ENV_RULES)}",
    "spec.apptainer.env.<K> for the same K",
)

_OVERLAY_FLAG = "--overlay"
_ENV_FLAG = "--env"


class SpecRewriteError(RuntimeError):
    """The spec could not be rewritten safely — nothing was written."""


@dataclass(frozen=True)
class SpecChange:
    """One field the rename touches, with its before/after values."""

    path: str
    before: str
    after: str

    def render(self) -> str:
        return f"{self.path}: {self.before} -> {self.after}"


# ---------------------------------------------------------------------------
# Value-level rewrite rules (precise, boundary-aware — NOT substring sed)
# ---------------------------------------------------------------------------


def sub_token(value: str, old: str, new: str) -> str:
    """Replace ``old`` with ``new`` at alphanumeric token boundaries.

    ``scitex-todo-maintainer`` -> ``scitex-cards-maintainer`` (the ``-``
    after the match is a boundary), while ``xscitex-todo`` is left alone
    (the ``x`` before the match is not). Used for free-form label values.
    """
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(old)}(?![A-Za-z0-9])")
    return pattern.sub(new, value)


def sub_path(value: str, old: str, new: str) -> str:
    """Replace whole PATH COMPONENTS equal to ``old``.

    ``/home/u/proj/scitex-todo`` -> ``/home/u/proj/scitex-cards``, and
    ``/state/scitex-todo/state.db`` -> ``/state/scitex-cards/state.db``.
    A component that merely CONTAINS ``old`` is never touched — this is
    what keeps the rewrite from mangling ``…/scitex-todo-archive/…``.
    """
    return "/".join(new if part == old else part for part in value.split("/"))


def sub_bind(value: str, old: str, new: str) -> str:
    """Rewrite an apptainer bind ``src[:dst[:opts]]`` path-component-wise.

    Only the path fields are touched; a trailing ``:ro`` / ``:rw`` option
    is passed through untouched (it can never be a path).
    """
    parts = value.split(":")
    opts = ""
    if len(parts) == 3:
        parts, opts = parts[:2], f":{parts[2]}"
    return ":".join(sub_path(p, old, new) for p in parts) + opts


def sub_env_value(key: str, value: str, old: str, new: str) -> str:
    """Rewrite an identity-bearing env VALUE per :data:`ENV_RULES`."""
    rule = ENV_RULES.get(key)
    if rule == "identity":
        return new if value == old else value
    if rule == "path":
        return sub_path(value, old, new)
    if rule == "scope":
        return f"agent:{new}" if value == f"agent:{old}" else value
    return value


def _split_env_arg(arg: str) -> tuple[str, str] | None:
    """Split a ``KEY=VALUE`` raw_args env payload, or None if not one."""
    if "=" not in arg:
        return None
    key, value = arg.split("=", 1)
    return key, value


# ---------------------------------------------------------------------------
# Document-level rewrite
# ---------------------------------------------------------------------------


def _round_trip_yaml():
    """ruamel YAML configured for comment/order-preserving round-trip.

    Mirrors ``cli_pkg/_host_crud._round_trip_yaml`` — one shape for every
    sac-side YAML write.
    """
    from ruamel.yaml import YAML

    yaml_rt = YAML()
    yaml_rt.preserve_quotes = True
    yaml_rt.indent(mapping=2, sequence=4, offset=2)
    yaml_rt.width = 4096  # never re-wrap a long operator comment or value
    return yaml_rt


def _get(node: Any, *keys: str) -> Any:
    """Walk a nested mapping, returning None at the first missing key."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _rewrite_labels(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    labels = _get(doc, "metadata", "labels")
    if not isinstance(labels, dict):
        return
    for key in ("project", "purpose"):
        before = labels.get(key)
        if not isinstance(before, str):
            continue
        after = sub_token(before, old, new)
        if after != before:
            labels[key] = after
            yield SpecChange(f"metadata.labels.{key}", before, after)


def _rewrite_workdir(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    spec = _get(doc, "spec")
    if not isinstance(spec, dict):
        return
    before = spec.get("workdir")
    if not isinstance(before, str):
        return
    after = sub_path(before, old, new)
    if after != before:
        spec["workdir"] = after
        yield SpecChange("spec.workdir", before, after)


def _rewrite_overlay_field(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    ap = _get(doc, "spec", "apptainer")
    if not isinstance(ap, dict):
        return
    before = ap.get("overlay")
    if not isinstance(before, str):
        return
    after = sub_path(before, old, new)
    if after != before:
        ap["overlay"] = after
        yield SpecChange("spec.apptainer.overlay", before, after)


def _rewrite_binds(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    ap = _get(doc, "spec", "apptainer")
    if not isinstance(ap, dict):
        return
    binds = ap.get("binds")
    if not isinstance(binds, list):
        return
    for idx, before in enumerate(binds):
        if not isinstance(before, str):
            continue
        after = sub_bind(before, old, new)
        if after != before:
            binds[idx] = after
            yield SpecChange(f"spec.apptainer.binds[{idx}]", before, after)


def _rewrite_env_map(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    env = _get(doc, "spec", "apptainer", "env")
    if not isinstance(env, dict):
        return
    for key in list(env):
        if key not in ENV_RULES:
            continue
        before = env[key]
        if not isinstance(before, str):
            continue
        after = sub_env_value(key, before, old, new)
        if after != before:
            env[key] = after
            yield SpecChange(f"spec.apptainer.env.{key}", before, after)


def _rewrite_raw_args(doc: Any, old: str, new: str) -> Iterator[SpecChange]:
    """Rewrite ``--overlay`` paths and identity ``--env`` payloads.

    ``raw_args`` is a FLAT list of argv tokens, so a value is identified
    by the flag that precedes it (``["--env", "K=V"]``) or by the
    ``=``-joined spelling (``["--overlay=/path"]``). Both spellings are
    live in the fleet — see ``runtimes/_apptainer_overlay``.
    """
    ap = _get(doc, "spec", "apptainer")
    if not isinstance(ap, dict):
        return
    args = ap.get("raw_args")
    if not isinstance(args, list):
        return

    for idx, token in enumerate(args):
        if not isinstance(token, str):
            continue
        prev = args[idx - 1] if idx > 0 else ""

        # `--overlay <path>` (space-separated) — this token is the value.
        if prev == _OVERLAY_FLAG:
            after = sub_path(token, old, new)
            if after != token:
                args[idx] = after
                yield SpecChange(
                    f"spec.apptainer.raw_args[{idx}] (--overlay)", token, after
                )
            continue

        # `--overlay=<path>` (=-joined) — flag and value in one token.
        if token.startswith(f"{_OVERLAY_FLAG}="):
            value = token.split("=", 1)[1]
            after_value = sub_path(value, old, new)
            if after_value != value:
                after = f"{_OVERLAY_FLAG}={after_value}"
                args[idx] = after
                yield SpecChange(
                    f"spec.apptainer.raw_args[{idx}] (--overlay=)", token, after
                )
            continue

        # `--env K=V` — this token is the payload.
        if prev == _ENV_FLAG:
            split = _split_env_arg(token)
            if split is None:
                continue
            key, value = split
            after_value = sub_env_value(key, value, old, new)
            if after_value != value:
                after = f"{key}={after_value}"
                args[idx] = after
                yield SpecChange(
                    f"spec.apptainer.raw_args[{idx}] (--env {key})", token, after
                )


_REWRITERS = (
    _rewrite_labels,
    _rewrite_workdir,
    _rewrite_overlay_field,
    _rewrite_binds,
    _rewrite_env_map,
    _rewrite_raw_args,
)


def plan_spec_changes(text: str, old: str, new: str) -> list[SpecChange]:
    """Return the changes :func:`rewrite_spec` WOULD make. Mutates nothing.

    Drives ``--dry-run``: it loads a throwaway copy of the document, runs
    the same rewriters, and reports what they touched.
    """
    return rewrite_spec(text, old, new)[1]


def rewrite_spec(text: str, old: str, new: str) -> tuple[str, list[SpecChange]]:
    """Rewrite ``text``'s self-references from ``old`` to ``new``.

    Returns ``(new_text, changes)``. ``changes`` is empty (and
    ``new_text is text``) when the spec names itself nowhere — a valid,
    if unusual, outcome that the caller surfaces rather than treats as a
    failure.

    Raises:
        SpecRewriteError: The document does not parse, or the round-trip
            moved something we did not plan to move. Nothing is written.
    """
    yaml_rt = _round_trip_yaml()
    try:
        doc = yaml_rt.load(text)
    except Exception as exc:  # noqa: BLE001 - any ruamel parse failure
        raise SpecRewriteError(f"spec does not parse as YAML: {exc}") from exc
    if not isinstance(doc, dict):
        raise SpecRewriteError("spec is not a YAML mapping")

    changes: list[SpecChange] = []
    for rewriter in _REWRITERS:
        changes.extend(rewriter(doc, old, new))

    if not changes:
        return text, []

    import io

    buf = io.StringIO()
    yaml_rt.dump(doc, buf)
    new_text = buf.getvalue()

    _assert_only_planned_changes(text, new_text, changes)
    return new_text, changes


# ---------------------------------------------------------------------------
# The guard: prove the round-trip moved ONLY what we planned to move
# ---------------------------------------------------------------------------


def _flatten(node: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten a parsed YAML doc to ``{dotted.path: scalar}``."""
    flat: dict[str, Any] = {}
    if isinstance(node, dict):
        for key, value in node.items():
            flat.update(_flatten(value, f"{prefix}.{key}" if prefix else str(key)))
    elif isinstance(node, list):
        for idx, value in enumerate(node):
            flat.update(_flatten(value, f"{prefix}[{idx}]"))
    else:
        flat[prefix] = node
    return flat


def _assert_only_planned_changes(
    before_text: str, after_text: str, changes: list[SpecChange]
) -> None:
    """Fail loud if the rewrite changed any value we did not plan to change.

    This is the safety net that makes a full load+dump round-trip
    acceptable on a comment-heavy, operator-authored spec. We compare the
    two documents SEMANTICALLY (flattened scalar maps, so comments and
    formatting are out of scope by construction) and require that the set
    of differing leaves is exactly the set of leaves our rewriters
    reported touching.

    A key that appears or disappears is also a failure — a rewrite must
    never add or drop a field.
    """
    import yaml as _pyyaml

    try:
        before = _flatten(_pyyaml.safe_load(before_text))
        after = _flatten(_pyyaml.safe_load(after_text))
    except _pyyaml.YAMLError as exc:
        raise SpecRewriteError(
            f"rewritten spec no longer parses: {exc}"
        ) from exc

    added = set(after) - set(before)
    dropped = set(before) - set(after)
    if added or dropped:
        raise SpecRewriteError(
            "rewrite changed the spec's SHAPE (this is a bug, not your spec) — "
            f"added={sorted(added)} dropped={sorted(dropped)}"
        )

    differing = {k for k in before if before[k] != after[k]}
    # The rewriters report ruamel paths (`spec.apptainer.raw_args[3]
    # (--env FOO)`); reduce to the leaf address to compare with _flatten.
    planned_values = {(c.before, c.after) for c in changes}
    for key in sorted(differing):
        if (before[key], after[key]) not in planned_values:
            raise SpecRewriteError(
                "rewrite changed a value it did not plan to change "
                f"(this is a bug, not your spec) — {key}: "
                f"{before[key]!r} -> {after[key]!r}"
            )
    if len(differing) != len(changes):
        raise SpecRewriteError(
            f"rewrite planned {len(changes)} change(s) but the document "
            f"shows {len(differing)} — refusing to write a spec we cannot "
            "fully account for"
        )


__all__ = [
    "ENV_RULES",
    "SPEC_TOUCHPOINTS",
    "SpecChange",
    "SpecRewriteError",
    "plan_spec_changes",
    "rewrite_spec",
    "sub_bind",
    "sub_env_value",
    "sub_path",
    "sub_token",
]
