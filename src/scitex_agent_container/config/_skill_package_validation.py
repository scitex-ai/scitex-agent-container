"""Validation for explicit canonical skill-package exposure."""

from __future__ import annotations

import re
from collections.abc import Mapping

_PACKAGE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def validate_skill_packages(spec: Mapping) -> list[str]:
    """Return stable author-facing errors for ``spec.skill_packages``."""
    if "skill_packages" not in spec:
        return []
    value = spec.get("skill_packages")
    if not isinstance(value, list):
        return [
            "spec.skill_packages must be a list of package IDs from "
            "~/.scitex/dev/skills"
        ]
    errors: list[str] = []
    seen: set[str] = set()
    for index, package_id in enumerate(value):
        path = f"spec.skill_packages[{index}]"
        if not isinstance(package_id, str) or not _PACKAGE_ID.fullmatch(package_id):
            errors.append(
                f"{path} must be a non-empty package ID using only letters, "
                "digits, '.', '_' or '-' (paths and globs are not accepted)"
            )
            continue
        if package_id in seen:
            errors.append(f"{path} duplicates package ID {package_id!r}")
        seen.add(package_id)
    return errors


__all__ = ["validate_skill_packages"]
