"""Typed wire models for the harness-neutral inbound turn endpoint."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class TurnRequest(BaseModel):
    """Validated ``POST /v1/turn`` request shared by every harness."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1)
    exit_after: StrictBool = False
    dispatch_id: str | None = Field(default=None, min_length=1)
    from_agent: str | None = Field(default=None, min_length=1)

    @field_validator("text")
    @classmethod
    def text_must_not_be_whitespace(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty or whitespace")
        return value

    @field_validator("dispatch_id", "from_agent", mode="before")
    @classmethod
    def blank_optional_identity_is_absent(cls, value: object) -> object:
        if value == "":
            return None
        return value
