"""Parser for ``spec.comms`` and ``spec.lineage`` (Phase-3 ACL).

Both blocks are optional. Their absence yields the default-construct of
:class:`CommsSpec` / :class:`LineageSpec`, which preserves pre-Phase-3
behaviour byte-identically. Unknown keys raise :class:`ValueError` so a
typo at the YAML surface fails loudly at boot rather than silently
degrading the per-spec ACL into "allow everything".
"""

from __future__ import annotations

from .._acl_types import (
    A2ACommsToggle,
    CommsSpec,
    InboundCommsSpec,
    LineageSpec,
    OutboundCommsSpec,
)

__all__ = ["parse_comms", "parse_lineage"]


_ALLOW_DENY = ("allow", "deny")
_LINEAGE_GROUPS = ("", "solitary")


def _allow_deny(value: object, *, key: str) -> str:
    """Coerce a YAML scalar into a strict ``"allow" | "deny"`` string."""
    if value is None:
        return "allow"
    if not isinstance(value, str):
        raise ValueError(
            f"{key} must be a string ('allow' | 'deny'), got "
            f"{type(value).__name__}: {value!r}"
        )
    if value not in _ALLOW_DENY:
        raise ValueError(
            f"{key} must be one of {_ALLOW_DENY}, got {value!r}"
        )
    return value


def _direction(raw: object, *, key: str) -> dict[str, str]:
    """Parse one direction (outbound | inbound) block into dict."""
    if raw is None:
        return {"siblings": "allow", "parent": "allow"}
    if not isinstance(raw, dict):
        raise ValueError(
            f"{key} must be a mapping, got {type(raw).__name__}: {raw!r}"
        )
    unknown = set(raw.keys()) - {"siblings", "parent"}
    if unknown:
        raise ValueError(
            f"{key} contains unknown keys {sorted(unknown)}; "
            "valid keys are 'siblings' and 'parent'."
        )
    return {
        "siblings": _allow_deny(raw.get("siblings"), key=f"{key}.siblings"),
        "parent": _allow_deny(raw.get("parent"), key=f"{key}.parent"),
    }


def _a2a_toggle(raw: object) -> A2ACommsToggle:
    """Parse ``spec.comms.a2a`` (currently only ``listen: bool``)."""
    if raw is None:
        return A2ACommsToggle()
    if not isinstance(raw, dict):
        raise ValueError(
            "spec.comms.a2a must be a mapping, got "
            f"{type(raw).__name__}: {raw!r}"
        )
    unknown = set(raw.keys()) - {"listen"}
    if unknown:
        raise ValueError(
            f"spec.comms.a2a contains unknown keys {sorted(unknown)}; "
            "valid key is 'listen'."
        )
    listen = raw.get("listen", True)
    if not isinstance(listen, bool):
        raise ValueError(
            "spec.comms.a2a.listen must be a boolean, got "
            f"{type(listen).__name__}: {listen!r}"
        )
    return A2ACommsToggle(listen=listen)


def parse_comms(spec: dict) -> CommsSpec:
    """Parse ``spec.comms`` into a :class:`CommsSpec`.

    Absence yields the default construct (everything ``"allow"``,
    ``a2a.listen=True``) so existing YAMLs behave byte-identically.
    """
    raw = spec.get("comms")
    if raw is None:
        return CommsSpec()
    if not isinstance(raw, dict):
        raise ValueError(
            f"spec.comms must be a mapping, got {type(raw).__name__}: {raw!r}"
        )
    unknown = set(raw.keys()) - {"outbound", "inbound", "a2a"}
    if unknown:
        raise ValueError(
            f"spec.comms contains unknown keys {sorted(unknown)}; "
            "valid keys are 'outbound', 'inbound', 'a2a'."
        )
    outbound = _direction(raw.get("outbound"), key="spec.comms.outbound")
    inbound = _direction(raw.get("inbound"), key="spec.comms.inbound")
    return CommsSpec(
        outbound=OutboundCommsSpec(**outbound),
        inbound=InboundCommsSpec(**inbound),
        a2a=_a2a_toggle(raw.get("a2a")),
    )


def parse_lineage(spec: dict) -> LineageSpec:
    """Parse ``spec.lineage`` into a :class:`LineageSpec`.

    Absence yields ``LineageSpec()`` (group derived from runtime
    lineage; ``may_spawn=True``) so existing behaviour is preserved.
    """
    raw = spec.get("lineage")
    if raw is None:
        return LineageSpec()
    if not isinstance(raw, dict):
        raise ValueError(
            f"spec.lineage must be a mapping, got {type(raw).__name__}: {raw!r}"
        )
    unknown = set(raw.keys()) - {"group", "may_spawn"}
    if unknown:
        raise ValueError(
            f"spec.lineage contains unknown keys {sorted(unknown)}; "
            "valid keys are 'group' and 'may_spawn'."
        )
    group = raw.get("group", "")
    if group is None:
        group = ""
    if not isinstance(group, str):
        raise ValueError(
            "spec.lineage.group must be a string, got "
            f"{type(group).__name__}: {group!r}"
        )
    if group not in _LINEAGE_GROUPS:
        raise ValueError(
            f"spec.lineage.group must be one of {_LINEAGE_GROUPS}, "
            f"got {group!r}"
        )
    may_spawn = raw.get("may_spawn", True)
    if not isinstance(may_spawn, bool):
        raise ValueError(
            "spec.lineage.may_spawn must be a boolean, got "
            f"{type(may_spawn).__name__}: {may_spawn!r}"
        )
    return LineageSpec(group=group, may_spawn=may_spawn)
