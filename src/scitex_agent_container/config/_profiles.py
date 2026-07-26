"""Named launch-profile selection for v3 agent specifications.

Profiles select a complete harness-facing configuration while keeping the
agent's placement, mounts, workdir, lifecycle, and other common settings at
``spec`` root.  Version 1 intentionally does not deep-merge partial profile
blocks: every profile owns a complete ``claude`` block, so selecting a profile
cannot inherit surprising credentials or backend settings from another one.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "LaunchProfileSelection",
    "ProfileSelectionError",
    "materialize_profile",
    "profile_structure_errors",
]

_PROFILE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?$")
_PROFILE_KEYS = frozenset({"harness", "claude"})
_SUPPORTED_HARNESSES = frozenset({"claude-code"})


class ProfileSelectionError(ValueError):
    """A requested launch profile cannot be selected."""


@dataclass(frozen=True)
class LaunchProfileSelection:
    """Resolved identity of the selected launch profile."""

    name: str
    default_name: str
    harness: str
    backend: str
    available: tuple[str, ...]
    is_profiled: bool


def _backend_name(claude: object) -> str:
    """Return a concise backend identity from a raw ``claude`` block."""
    if not isinstance(claude, dict):
        return "anthropic"
    provider = claude.get("provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    if isinstance(provider, dict):
        name = provider.get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
        return "custom"
    return "anthropic"


def profile_structure_errors(raw: object) -> list[str]:
    """Validate only the profile envelope, not each effective agent spec."""
    if not isinstance(raw, dict):
        return []
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        return []

    has_profiles = "profiles" in spec
    has_default = "default_profile" in spec
    if not has_profiles:
        if has_default:
            return [
                "spec.default_profile requires spec.profiles; remove it or "
                "define the named profiles."
            ]
        return []

    errors: list[str] = []
    profiles = spec.get("profiles")
    default = spec.get("default_profile")

    if "claude" in spec:
        errors.append(
            "spec.claude cannot be combined with spec.profiles. Move each "
            "complete claude block under spec.profiles.<name>.claude."
        )
    if not isinstance(profiles, dict) or not profiles:
        errors.append("spec.profiles must be a non-empty mapping.")
        return errors
    if not isinstance(default, str) or not default.strip():
        errors.append(
            "spec.default_profile is required and must be a non-empty string "
            "when spec.profiles is declared."
        )
    elif default not in profiles:
        available = ", ".join(str(name) for name in profiles)
        errors.append(
            f"spec.default_profile '{default}' is not defined in spec.profiles. "
            f"Available profiles: {available}."
        )

    for name, block in profiles.items():
        path = f"spec.profiles.{name}"
        if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name):
            errors.append(
                f"Profile name {name!r} is invalid. Use lowercase letters, "
                "digits, dots, underscores, or hyphens."
            )
        if not isinstance(block, dict):
            errors.append(f"{path} must be a mapping.")
            continue
        for key in sorted(set(block) - _PROFILE_KEYS):
            errors.append(
                f"Unknown field '{path}.{key}'. Valid profile fields: "
                f"{sorted(_PROFILE_KEYS)}."
            )

        harness = block.get("harness")
        if not isinstance(harness, str) or not harness.strip():
            errors.append(f"{path}.harness is required and must be a string.")
        elif harness not in _SUPPORTED_HARNESSES:
            errors.append(
                f"{path}.harness must be one of "
                f"{sorted(_SUPPORTED_HARNESSES)}, got '{harness}'. Native "
                "Codex is not a supported harness yet; use harness: "
                "claude-code with claude.provider: codex."
            )

        claude = block.get("claude")
        if not isinstance(claude, dict):
            errors.append(
                f"{path}.claude is required and must be a complete mapping."
            )

    return errors


def materialize_profile(
    raw: dict[str, Any], requested: str | None = None
) -> tuple[dict[str, Any], LaunchProfileSelection]:
    """Return a conventional effective v3 document and profile identity.

    Legacy documents without ``spec.profiles`` remain valid when no profile is
    requested.  An explicit ``--profile`` against such a document fails loud:
    silently accepting it would imply that a selection took effect when it did
    not.
    """
    spec = raw.get("spec")
    if not isinstance(spec, dict):
        raise ProfileSelectionError("spec is required and must be a mapping")

    profiles = spec.get("profiles")
    if "profiles" not in spec:
        if requested:
            raise ProfileSelectionError(
                f"Profile '{requested}' was requested, but this spec does not "
                "define spec.profiles. Add profiles or omit --profile."
            )
        claude = spec.get("claude")
        selection = LaunchProfileSelection(
            name="default",
            default_name="default",
            harness="claude-code",
            backend=_backend_name(claude),
            available=(),
            is_profiled=False,
        )
        return copy.deepcopy(raw), selection

    structural_errors = profile_structure_errors(raw)
    if structural_errors:
        raise ProfileSelectionError("\n".join(structural_errors))
    assert isinstance(profiles, dict)

    default = str(spec["default_profile"])
    selected = requested or default
    if selected not in profiles:
        available = ", ".join(str(name) for name in profiles)
        raise ProfileSelectionError(
            f"Unknown profile '{selected}'. Available profiles: {available}."
        )

    profile = profiles[selected]
    assert isinstance(profile, dict)
    effective = copy.deepcopy(raw)
    effective_spec = effective["spec"]
    effective_spec.pop("profiles", None)
    effective_spec.pop("default_profile", None)
    effective_spec["claude"] = copy.deepcopy(profile["claude"])
    available_names = tuple(str(name) for name in profiles)
    selection = LaunchProfileSelection(
        name=selected,
        default_name=default,
        harness=str(profile["harness"]),
        backend=_backend_name(profile["claude"]),
        available=available_names,
        is_profiled=True,
    )
    return effective, selection
