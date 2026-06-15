"""Validation for ``spec.autonomous`` (F-CS3 phase 1).

Extracted from :mod:`._validation` to keep that parent module under
the 512-line per-file cap. Mirrors the existing
``_acl_validation`` / ``_provider_validation`` split pattern: a
single ``validate_autonomous(spec) -> list[str]`` entry that the
parent calls and concatenates into its own error list.

The autonomous block authors the drive-until-done contract:

  * ``drive_until``       — non-empty marker string the runner watches
                            each assistant turn for.
  * ``max_turns``         — int > 0 cap on conversation length.
  * ``idle_kick_after_s`` — int > 0 idle deadline before a kick is
                            posted.
  * ``kick_text``         — string posted on idle timeout.
  * ``enabled``           — bool gate; runner ignores the block when
                            False.
"""

from __future__ import annotations


def validate_autonomous(spec: dict) -> list[str]:
    """Return shape errors for ``spec.autonomous`` (empty when valid).

    Absent block → no errors (the spec is optional). Non-dict block →
    one error. A dict block is field-by-field type-checked; positive
    integer constraints applied to ``max_turns`` + ``idle_kick_after_s``.
    """
    errors: list[str] = []
    autonomous = spec.get("autonomous")
    if autonomous is None:
        return errors
    if not isinstance(autonomous, dict):
        errors.append(
            f"spec.autonomous must be a mapping; got {type(autonomous).__name__}"
        )
        return errors
    drive_until = autonomous.get("drive_until")
    if drive_until is not None and not isinstance(drive_until, str):
        errors.append("spec.autonomous.drive_until must be a string")
    elif drive_until == "":
        errors.append("spec.autonomous.drive_until must be non-empty")
    for fld in ("max_turns", "idle_kick_after_s"):
        val = autonomous.get(fld)
        if val is not None:
            if not isinstance(val, int) or isinstance(val, bool):
                errors.append(f"spec.autonomous.{fld} must be an integer")
            elif val <= 0:
                errors.append(f"spec.autonomous.{fld} must be > 0")
    kick = autonomous.get("kick_text")
    if kick is not None and not isinstance(kick, str):
        errors.append("spec.autonomous.kick_text must be a string")
    enabled = autonomous.get("enabled")
    if enabled is not None and not isinstance(enabled, bool):
        errors.append("spec.autonomous.enabled must be a boolean")
    return errors


__all__ = ["validate_autonomous"]
