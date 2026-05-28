"""Validator for Phase-3 capsule-isolation YAML blocks.

Kept in a sibling module so :mod:`._validation` stays under the per-file
line cap. The validator runs FIRST (sac doctor / yaml-validate use it
without loading via :mod:`._parsers`) so the structural checks need to
live independently of the parser even though both enforce the same
schema.

Schema mirrors :mod:`._acl_types`:

* ``spec.comms.outbound.{siblings,parent}`` — ``"allow" | "deny"``
* ``spec.comms.inbound.{siblings,parent}``  — ``"allow" | "deny"``
* ``spec.comms.a2a.listen``                  — ``bool``
* ``spec.lineage.group``                     — ``"" | "solitary"``
* ``spec.lineage.may_spawn``                 — ``bool``

Absence of either block yields zero errors — defaults preserve
pre-Phase-3 behaviour.
"""

from __future__ import annotations

__all__ = ["validate_phase3_acl"]


_ALLOW_DENY = ("allow", "deny")
_LINEAGE_GROUPS = ("", "solitary")


def _validate_comms(comms: object) -> list[str]:
    errs: list[str] = []
    if comms is None:
        return errs
    if not isinstance(comms, dict):
        errs.append(
            f"spec.comms must be a mapping, got {type(comms).__name__}"
        )
        return errs
    unknown = set(comms.keys()) - {"outbound", "inbound", "a2a"}
    for k in sorted(unknown):
        errs.append(
            f"spec.comms.{k} is not a valid key; "
            "use 'outbound', 'inbound', or 'a2a'."
        )
    for dir_key in ("outbound", "inbound"):
        block = comms.get(dir_key)
        if block is None:
            continue
        if not isinstance(block, dict):
            errs.append(
                f"spec.comms.{dir_key} must be a mapping, got "
                f"{type(block).__name__}"
            )
            continue
        for sub in ("siblings", "parent"):
            val = block.get(sub)
            if val is None:
                continue
            if not isinstance(val, str) or val not in _ALLOW_DENY:
                errs.append(
                    f"spec.comms.{dir_key}.{sub} must be one of "
                    f"{_ALLOW_DENY}, got {val!r}"
                )
        extra_dir = set(block.keys()) - {"siblings", "parent"}
        for k in sorted(extra_dir):
            errs.append(
                f"spec.comms.{dir_key}.{k} is not a valid key; "
                "use 'siblings' or 'parent'."
            )
    a2a_block = comms.get("a2a")
    if a2a_block is not None:
        if not isinstance(a2a_block, dict):
            errs.append(
                "spec.comms.a2a must be a mapping, got "
                f"{type(a2a_block).__name__}"
            )
        else:
            listen = a2a_block.get("listen")
            if listen is not None and not isinstance(listen, bool):
                errs.append(
                    "spec.comms.a2a.listen must be a boolean, got "
                    f"{type(listen).__name__}"
                )
            extra_a2a = set(a2a_block.keys()) - {"listen"}
            for k in sorted(extra_a2a):
                errs.append(
                    f"spec.comms.a2a.{k} is not a valid key; "
                    "use 'listen'."
                )
    return errs


def _validate_lineage(lineage: object) -> list[str]:
    errs: list[str] = []
    if lineage is None:
        return errs
    if not isinstance(lineage, dict):
        errs.append(
            f"spec.lineage must be a mapping, got {type(lineage).__name__}"
        )
        return errs
    unknown = set(lineage.keys()) - {"group", "may_spawn"}
    for k in sorted(unknown):
        errs.append(
            f"spec.lineage.{k} is not a valid key; "
            "use 'group' or 'may_spawn'."
        )
    group = lineage.get("group")
    if group is not None:
        if not isinstance(group, str) or group not in _LINEAGE_GROUPS:
            errs.append(
                f"spec.lineage.group must be one of {_LINEAGE_GROUPS}, "
                f"got {group!r}"
            )
    may_spawn = lineage.get("may_spawn")
    if may_spawn is not None and not isinstance(may_spawn, bool):
        errs.append(
            "spec.lineage.may_spawn must be a boolean, got "
            f"{type(may_spawn).__name__}"
        )
    return errs


def validate_phase3_acl(spec: dict) -> list[str]:
    """Validate ``spec.comms`` + ``spec.lineage`` shapes.

    Returns a list of human-readable error strings (empty = valid).
    """
    return _validate_comms(spec.get("comms")) + _validate_lineage(
        spec.get("lineage")
    )
