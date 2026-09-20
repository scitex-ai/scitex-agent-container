"""Strict authored home-import contract and frozen runtime representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from ._pydantic_errors import format_validation_error


class ToHomeSpecError(ValueError):
    """The authored ``spec.to_home`` contract is invalid."""


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


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ImportModel(_StrictModel):
    id: str
    source: str
    precedence: int
    apply: Literal["pre-launch-on-start-and-restart"]
    destination: str
    mode: Literal["managed-overlay-v1"]
    conflict: Literal["error", "higher-precedence-wins"]
    stale: Literal["preserve"]
    required: bool

    @field_validator("id", "source")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty string")
        return value.strip()

    @field_validator("source")
    @classmethod
    def _portable_source(cls, value: str) -> str:
        if value.startswith("~") or "${" in value:
            raise ValueError(
                "must be absolute or spec-relative, without '~' or environment expansion"
            )
        return value

    @field_validator("destination")
    @classmethod
    def _destination_beneath_home(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("must stay beneath the agent home")
        if str(path) != ".":
            raise ValueError(
                "must currently be '.'; managed-overlay-v1 materializes a complete home tree"
            )
        return str(path)


class _ToHomeModel(_StrictModel):
    imports: list[_ImportModel]

    @model_validator(mode="after")
    def _ordered_unique_imports(self) -> "_ToHomeModel":
        ids = [item.id for item in self.imports]
        duplicate = next((item for item in ids if ids.count(item) > 1), None)
        if duplicate is not None:
            raise ValueError(f"import id duplicates {duplicate!r}")
        precedences = [item.precedence for item in self.imports]
        if any(right <= left for left, right in zip(precedences, precedences[1:])):
            raise ValueError("import precedence must increase strictly in list order")
        return self


def parse_to_home(value: Any) -> ToHomeSpec:
    """Parse ``spec.to_home`` without defaults, coercion, or legacy aliases."""
    try:
        parsed = _ToHomeModel.model_validate(value, strict=True)
    except ValidationError as exc:
        raise ToHomeSpecError(
            format_validation_error(exc, prefix="spec.to_home")
        ) from None
    return ToHomeSpec(
        imports=tuple(
            ToHomeImportSpec(
                id=item.id,
                source=item.source,
                precedence=item.precedence,
                apply=item.apply,
                destination=item.destination,
                mode=item.mode,
                conflict=item.conflict,
                stale=item.stale,
                required=item.required,
            )
            for item in parsed.imports
        )
    )


__all__ = ["ToHomeImportSpec", "ToHomeSpec", "ToHomeSpecError", "parse_to_home"]
