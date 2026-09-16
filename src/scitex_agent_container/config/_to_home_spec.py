"""Explicit, ordered declarations for materialising an agent home.

``managed-overlay-v1`` names the existing, versioned materializer semantics:
ordinary same-path files are replaced by the higher-precedence import;
``.mcp.json`` and ``.claude/settings*.json`` are deep-merged;
``CLAUDE.md`` and ``state.md`` are marker-composed; unrelated/stale files are
preserved.  Naming that policy in the spec makes these non-uniform rules part
of the launch contract instead of hidden path-based discovery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ToHomeSpecError(ValueError):
    """The authored ``spec.to_home`` contract is incomplete or ambiguous."""


@dataclass(frozen=True)
class ToHomeImportSpec:
    id: str
    source: str
    precedence: int
    apply: str
    destination: str
    mode: str
    conflict: str
    stale: str
    required: bool


@dataclass(frozen=True)
class ToHomeSpec:
    imports: tuple[ToHomeImportSpec, ...] = field(default_factory=tuple)


_IMPORT_KEYS = frozenset(
    {
        "id",
        "source",
        "precedence",
        "apply",
        "destination",
        "mode",
        "conflict",
        "stale",
        "required",
    }
)


def parse_to_home(value: Any) -> ToHomeSpec:
    """Parse the self-contained home contract; legacy spellings are refused."""
    if not isinstance(value, dict) or set(value) != {"imports"}:
        raise ToHomeSpecError(
            "spec.to_home must be exactly a mapping with an ordered 'imports' list; "
            "the legacy path scalar and spec.to_home_layers are not executable."
        )
    raw_imports = value["imports"]
    if not isinstance(raw_imports, list):
        raise ToHomeSpecError("spec.to_home.imports must be a list (use [] for no imports)")

    imports: list[ToHomeImportSpec] = []
    seen: set[str] = set()
    previous_precedence: int | None = None
    for index, raw in enumerate(raw_imports):
        where = f"spec.to_home.imports[{index}]"
        if not isinstance(raw, dict):
            raise ToHomeSpecError(f"{where} must be a mapping")
        missing = _IMPORT_KEYS - set(raw)
        extra = set(raw) - _IMPORT_KEYS
        if missing or extra:
            raise ToHomeSpecError(
                f"{where} must contain exactly {sorted(_IMPORT_KEYS)!r}; "
                f"missing={sorted(missing)!r}, unknown={sorted(extra)!r}"
            )
        layer_id = raw["id"]
        source = raw["source"]
        destination = raw["destination"]
        if not isinstance(layer_id, str) or not layer_id.strip():
            raise ToHomeSpecError(f"{where}.id must be a non-empty string")
        if layer_id in seen:
            raise ToHomeSpecError(f"{where}.id duplicates {layer_id!r}")
        seen.add(layer_id)
        if not isinstance(source, str) or not source.strip():
            raise ToHomeSpecError(f"{where}.source must be a non-empty path string")
        if source.startswith("~") or "${" in source:
            raise ToHomeSpecError(
                f"{where}.source must be absolute or spec-relative, without '~' or environment expansion"
            )
        precedence = raw["precedence"]
        if type(precedence) is not int:
            raise ToHomeSpecError(f"{where}.precedence must be an integer")
        if previous_precedence is not None and precedence <= previous_precedence:
            raise ToHomeSpecError(
                f"{where}.precedence must be greater than {previous_precedence}; list order is precedence order"
            )
        previous_precedence = precedence
        if raw["apply"] != "pre-launch-on-start-and-restart":
            raise ToHomeSpecError(
                f"{where}.apply must be 'pre-launch-on-start-and-restart'"
            )
        if not isinstance(destination, str) or not destination.strip():
            raise ToHomeSpecError(f"{where}.destination must be '.' or a relative home path")
        from pathlib import PurePosixPath

        target_path = PurePosixPath(destination)
        if target_path.is_absolute() or ".." in target_path.parts:
            raise ToHomeSpecError(f"{where}.destination must stay beneath the agent home")
        if str(target_path) != ".":
            raise ToHomeSpecError(
                f"{where}.destination must currently be '.'; managed-overlay-v1 materializes a complete home tree"
            )
        if raw["mode"] != "managed-overlay-v1":
            raise ToHomeSpecError(
                f"{where}.mode must be 'managed-overlay-v1', got {raw['mode']!r}"
            )
        if raw["conflict"] not in {"error", "higher-precedence-wins"}:
            raise ToHomeSpecError(
                f"{where}.conflict must be 'error' or 'higher-precedence-wins', got {raw['conflict']!r}"
            )
        if raw["stale"] != "preserve":
            raise ToHomeSpecError(
                f"{where}.stale must be 'preserve'; pruning unmanaged runtime-home files is not supported"
            )
        if type(raw["required"]) is not bool:
            raise ToHomeSpecError(f"{where}.required must be a boolean")
        imports.append(
            ToHomeImportSpec(
                id=layer_id.strip(),
                source=source.strip(),
                precedence=precedence,
                apply=raw["apply"],
                destination=str(target_path),
                mode="managed-overlay-v1",
                conflict=raw["conflict"],
                stale=raw["stale"],
                required=raw["required"],
            )
        )
    return ToHomeSpec(tuple(imports))
