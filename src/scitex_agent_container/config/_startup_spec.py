"""Strict authored startup contract and frozen runtime representation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from ._pydantic_errors import format_validation_error


class StartupSpecError(ValueError):
    """The authored ``spec.startup`` contract is invalid."""


@dataclass(frozen=True)
class StartupCommand:
    delay: int = 0
    command: str = ""


@dataclass(frozen=True)
class StartupEnvironmentSpec:
    resolve_on: str
    conflict_policy: str
    values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class StartupCommandExecutionSpec:
    location: str
    phase: str
    shell: str
    failure: str


@dataclass(frozen=True)
class StartupCommandsSpec:
    execution: StartupCommandExecutionSpec
    entries: tuple[StartupCommand, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StartupPromptExecutionSpec:
    location: str
    phase: str
    readiness: str


@dataclass(frozen=True)
class StartupPromptsSpec:
    execution: StartupPromptExecutionSpec
    entries: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class StartupSpec:
    environment: StartupEnvironmentSpec
    commands: StartupCommandsSpec
    prompts: StartupPromptsSpec


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EnvironmentModel(_StrictModel):
    resolve_on: Literal["target-host"]
    conflict_policy: Literal["specification-wins"]
    values: dict[str, str]

    @field_validator("values")
    @classmethod
    def _non_empty_keys(cls, value: dict[str, str]) -> dict[str, str]:
        invalid = [key for key in value if not key.strip()]
        if invalid:
            raise ValueError("environment variable names must be non-empty")
        return value


class _CommandExecutionModel(_StrictModel):
    location: Literal["apptainer"]
    phase: Literal["before-harness"]
    shell: Literal["/bin/bash -lc"]
    failure: Literal["abort"]


class _CommandEntryModel(_StrictModel):
    run: str
    delay_seconds: int = Field(ge=0)

    @field_validator("run")
    @classmethod
    def _non_empty_run(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must be a non-empty command string")
        return value


class _CommandsModel(_StrictModel):
    execution: _CommandExecutionModel
    entries: list[_CommandEntryModel]


class _PromptExecutionModel(_StrictModel):
    location: Literal["harness"]
    phase: Literal["first-turn"]
    readiness: Literal["required"]


class _PromptsModel(_StrictModel):
    execution: _PromptExecutionModel
    entries: list[str]

    @field_validator("entries")
    @classmethod
    def _non_empty_prompts(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("prompt entries must be non-empty strings")
        return value


class _StartupModel(_StrictModel):
    environment: _EnvironmentModel
    commands: _CommandsModel
    prompts: _PromptsModel


def parse_startup(value: Any) -> StartupSpec:
    """Parse ``spec.startup`` without defaults, coercion, or legacy aliases."""
    try:
        parsed = _StartupModel.model_validate(value, strict=True)
    except ValidationError as exc:
        raise StartupSpecError(
            format_validation_error(exc, prefix="spec.startup")
        ) from None
    return StartupSpec(
        environment=StartupEnvironmentSpec(
            resolve_on=parsed.environment.resolve_on,
            conflict_policy=parsed.environment.conflict_policy,
            values=dict(parsed.environment.values),
        ),
        commands=StartupCommandsSpec(
            execution=StartupCommandExecutionSpec(
                location=parsed.commands.execution.location,
                phase=parsed.commands.execution.phase,
                shell=parsed.commands.execution.shell,
                failure=parsed.commands.execution.failure,
            ),
            entries=tuple(
                StartupCommand(delay=item.delay_seconds, command=item.run)
                for item in parsed.commands.entries
            ),
        ),
        prompts=StartupPromptsSpec(
            execution=StartupPromptExecutionSpec(
                location=parsed.prompts.execution.location,
                phase=parsed.prompts.execution.phase,
                readiness=parsed.prompts.execution.readiness,
            ),
            entries=tuple(parsed.prompts.entries),
        ),
    )


__all__ = ["StartupCommand", "StartupSpec", "StartupSpecError", "parse_startup"]
