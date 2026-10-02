"""Typed wire models for the harness-neutral inbound turn endpoint."""

from __future__ import annotations

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    field_validator,
    model_validator,
)


class TurnRequest(BaseModel):
    """Validated ``POST /v1/turn`` request shared by every harness."""

    model_config = ConfigDict(extra="forbid", strict=True)

    text: str = Field(min_length=1)
    exit_after: StrictBool = False
    dispatch_id: str | None = Field(default=None, min_length=1)
    from_agent: str | None = Field(default=None, min_length=1)
    visible_delivery_id: str | None = Field(default=None, min_length=1)

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

    @model_validator(mode="after")
    def visible_delivery_must_bind_to_text(self) -> TurnRequest:
        # Match the public sender and TUI receiver's exact marker contract.
        # The field binds prompt text; it is not proof that a turn completed.
        if self.visible_delivery_id is not None and (
            f"<!-- delivery:{self.visible_delivery_id} -->" not in self.text
        ):
            raise ValueError(
                "visible_delivery_id is not bound to the submitted text; "
                "include its exact delivery marker before retrying"
            )
        return self
