"""Contributor spec YAML schema validator (ZOO#01 chunk C).

Validates rendered contributor agent spec.yaml files. Extends the base
validate_raw() with contributor-specific required fields:
  - metadata.labels (role, team, trigger, project)
  - spec.a2a.port   (integer, 1024-65535)
  - spec.startup_commands (non-empty list)
"""

from __future__ import annotations

from pathlib import Path

import yaml

from ._validation import validate_raw

_REQUIRED_LABEL_KEYS = ("role", "team", "trigger", "project")
_PORT_MIN = 1024
_PORT_MAX = 65535


def validate_contributor_spec_raw(raw: dict, path: str = "<unknown>") -> list[str]:
    """Validate a contributor spec dict. Returns list of error strings."""
    errors = validate_raw(raw, path)

    if not isinstance(raw, dict):
        return errors

    # metadata.labels — required for contributor specs
    metadata = raw.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata is required for contributor specs and must be a mapping")
    else:
        labels = metadata.get("labels")
        if not isinstance(labels, dict):
            errors.append("metadata.labels is required for contributor specs and must be a mapping")
        else:
            for key in _REQUIRED_LABEL_KEYS:
                if not labels.get(key):
                    errors.append(
                        f"metadata.labels.{key} is required for contributor specs"
                    )

    spec = raw.get("spec")
    if not isinstance(spec, dict):
        return errors  # base validator already reported this

    # spec.a2a.port — required for contributor specs
    a2a = spec.get("a2a")
    if not isinstance(a2a, dict):
        errors.append("spec.a2a is required for contributor specs and must be a mapping")
    else:
        port = a2a.get("port")
        if port is None:
            errors.append("spec.a2a.port is required for contributor specs")
        elif not isinstance(port, int):
            errors.append(
                f"spec.a2a.port must be an integer, got {type(port).__name__!r}"
            )
        elif not (_PORT_MIN <= port <= _PORT_MAX):
            errors.append(
                f"spec.a2a.port must be in range {_PORT_MIN}-{_PORT_MAX}, got {port}"
            )

    # spec.startup_commands — required, non-empty list
    cmds = spec.get("startup_commands")
    if cmds is None:
        errors.append("spec.startup_commands is required for contributor specs")
    elif not isinstance(cmds, list):
        errors.append("spec.startup_commands must be a list")
    elif len(cmds) == 0:
        errors.append("spec.startup_commands must not be empty")
    else:
        for i, cmd in enumerate(cmds):
            if not isinstance(cmd, dict):
                errors.append(f"spec.startup_commands[{i}] must be a mapping")
            elif not cmd.get("command"):
                errors.append(
                    f"spec.startup_commands[{i}].command is required and must be non-empty"
                )

    return errors


def validate_contributor_spec(path: str | Path) -> list[str]:
    """Validate a contributor spec YAML file. Returns list of errors (empty = valid)."""
    path = Path(path).resolve()
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        return [f"File not found: {path}"]
    except yaml.YAMLError as exc:
        return [f"YAML parse error: {exc}"]

    return validate_contributor_spec_raw(raw, str(path))
