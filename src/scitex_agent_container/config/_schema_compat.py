"""Canonical authoring names translated at the v3 parser boundary."""

from __future__ import annotations

import copy
from dataclasses import asdict
from typing import Mapping

from ._harness_lookup import canonical_harness
from ._harness_types import resolve_spec_harness
from ._parsers._claude import parse_claude

AVAILABLE_ENGINES_KEY = "available_engines"
AVAILABLE_HARNESSES_KEY = "available_harnesses"
LEGACY_ENGINES_KEY = "engines"
LEGACY_HARNESS_OPTIONS_KEY = "claude"
LEGACY_CONTAINER_KEY = "container"

_COMMON_HARNESS_ENTRY_KEYS = frozenset(
    {
        "session",
    }
)
_SESSION_KEYS = frozenset({"mode", "max_age_minutes"})
_WATCHDOG_KEYS = frozenset({"enabled", "interval", "responses"})
_WATCHDOG_RESPONSE_KEYS = frozenset({"y_n", "y_y_n", "waiting"})
_HERMES_COMPRESSION_KEYS = frozenset(
    {"threshold", "target_ratio", "tail_mode", "in_place"}
)


def _is_number(value: object) -> bool:
    return type(value) in {int, float}


def _validate_hermes_compression(
    value: object, *, path: str, errors: list[str]
) -> None:
    if not isinstance(value, Mapping):
        errors.append(f"{path} must be a mapping")
        return
    unknown = sorted(set(map(str, value)) - _HERMES_COMPRESSION_KEYS)
    if unknown:
        errors.append(f"{path} has unknown fields: {unknown}")
    threshold = value.get("threshold", 0.80)
    threshold_valid = _is_number(threshold) and 0 < threshold < 1
    if not threshold_valid:
        errors.append(f"{path}.threshold must be a number between 0 and 1")
    target_ratio = value.get("target_ratio", 0.20)
    target_valid = _is_number(target_ratio) and 0 < target_ratio < 1
    if not target_valid:
        errors.append(f"{path}.target_ratio must be a number between 0 and 1")
    elif threshold_valid and target_ratio >= threshold:
        errors.append(f"{path}.target_ratio must be less than threshold")
    tail_mode = value.get("tail_mode", "lean")
    if tail_mode not in {"lean", "legacy"}:
        errors.append(f"{path}.tail_mode must be lean or legacy")
    if type(value.get("in_place", True)) is not bool:
        errors.append(f"{path}.in_place must be a boolean")


def _public_harness(name: str) -> str | None:
    """Return the product/program name used by the authored schema."""
    family = canonical_harness(name)
    return {
        "anthropic": "claude-code",
        "openai": "openai-agents",
    }.get(family, family)


def _selected_entry(spec: Mapping) -> tuple[str, Mapping] | None:
    block = spec.get(AVAILABLE_HARNESSES_KEY)
    if not isinstance(block, Mapping):
        return None
    try:
        selected = _public_harness(resolve_spec_harness(spec))
    except ValueError:
        return None
    for key, value in block.items():
        if _public_harness(str(key)) == selected and isinstance(value, Mapping):
            return str(key), value
    return None


def _claude_compat(entry: Mapping, channels: object = None) -> dict:
    session = entry.get("session")
    session = session if isinstance(session, Mapping) else {}
    approval = entry.get("approval_policy")
    return {
        "model": "",
        "channels": list(channels or []),
        "flags": [],
        "raw_options": {},
        "session": session.get("mode"),
        "continue_max_age_minutes": session.get("max_age_minutes"),
        "resume_id": "",
        "auto_accept": approval == "never",
        # ``ClaudeSpec`` remains the typed runtime boundary. Carry the
        # canonical product-owned account pin into that boundary so every
        # existing auth/preflight consumer sees the authored value.
        "account": entry.get("account", ""),
        "credentials_file": "",
        "credentials_files": [],
        "provider": None,
    }


def _claude_values(raw: Mapping) -> dict:
    return asdict(parse_claude({"claude": dict(raw)}))


def canonical_surface_errors(raw: object) -> list[str]:
    """Validate canonical aliases and conflicts before compatibility folding."""
    if not isinstance(raw, Mapping):
        return []
    spec = raw.get("spec")
    if not isinstance(spec, Mapping):
        return []
    errors: list[str] = []

    available_engines = spec.get(AVAILABLE_ENGINES_KEY)
    legacy_engines = spec.get(LEGACY_ENGINES_KEY)
    if AVAILABLE_ENGINES_KEY in spec and not isinstance(available_engines, Mapping):
        errors.append("spec.available_engines must be a mapping of engine entries")
    if (
        AVAILABLE_ENGINES_KEY in spec
        and LEGACY_ENGINES_KEY in spec
        and available_engines != legacy_engines
    ):
        errors.append(
            "spec.available_engines and legacy spec.engines disagree; declare "
            "one surface, or make the two mappings identical"
        )

    comms = spec.get("comms")

    if AVAILABLE_HARNESSES_KEY not in spec:
        return errors
    harnesses = spec.get(AVAILABLE_HARNESSES_KEY)
    if not isinstance(harnesses, Mapping) or not harnesses:
        return errors + [
            "spec.available_harnesses must be a non-empty mapping of harness entries"
        ]

    canonical_keys: dict[str, str] = {}
    for raw_key, raw_entry in harnesses.items():
        key = str(raw_key)
        family = _public_harness(key)
        path = f"spec.available_harnesses.{key}"
        if family is None:
            errors.append(f"{path} names an unknown harness")
            continue
        if key != family:
            errors.append(
                f"{path} uses a compatibility label; write the product harness "
                f"name {family!r}"
            )
        if family in canonical_keys:
            errors.append(
                f"{path} duplicates {canonical_keys[family]!r}; both resolve to "
                f"the same harness {family!r}"
            )
            continue
        canonical_keys[family] = key
        if not isinstance(raw_entry, Mapping):
            errors.append(f"{path} must be a mapping")
            continue
        entry_keys = set(map(str, raw_entry))
        allowed = set(_COMMON_HARNESS_ENTRY_KEYS)
        required = set(_COMMON_HARNESS_ENTRY_KEYS)
        if "compression" in raw_entry:
            allowed.add("compression")
        if "background_review" in raw_entry:
            allowed.add("background_review")
        if "run_budget_seconds" in raw_entry:
            allowed.add("run_budget_seconds")
        if "max_turns" in raw_entry:
            allowed.add("max_turns")
        if family == "claude-code":
            allowed.update({"account", "approval_policy", "watchdog"})
            required.update({"approval_policy", "watchdog"})
        elif family == "codex":
            allowed.update({"approval_policy", "sandbox_mode"})
            required.update({"approval_policy", "sandbox_mode"})
        elif family == "hermes":
            allowed.update(
                {"background_review", "compression", "max_turns", "run_budget_seconds"}
            )
        missing = sorted(required - entry_keys)
        unknown = sorted(entry_keys - allowed - {"channels"})
        if missing:
            errors.append(f"{path} is missing required fields: {missing}")
        if unknown:
            errors.append(f"{path} has unknown fields: {unknown}")
        if "channels" in raw_entry:
            errors.append(
                f"{path}.channels is harness-specific placement; move it to "
                "spec.comms.channels so the same declaration drives Claude Code, "
                "Hermes, and Codex"
            )

        if "compression" in raw_entry:
            if family != "hermes":
                errors.append(
                    f"{path}.compression is only valid for the Hermes harness"
                )
            else:
                _validate_hermes_compression(
                    raw_entry.get("compression"),
                    path=f"{path}.compression",
                    errors=errors,
                )

        if "background_review" in raw_entry:
            if family != "hermes":
                errors.append(
                    f"{path}.background_review is only valid for the Hermes harness"
                )
            elif type(raw_entry.get("background_review")) is not bool:
                errors.append(f"{path}.background_review must be a boolean")

        if "run_budget_seconds" in raw_entry:
            if family != "hermes":
                errors.append(
                    f"{path}.run_budget_seconds is only valid for the Hermes harness"
                )
            else:
                budget = raw_entry.get("run_budget_seconds")
                if budget is not None and (type(budget) is not int or budget <= 0):
                    errors.append(
                        f"{path}.run_budget_seconds must be null or a positive integer"
                    )

        if "max_turns" in raw_entry:
            if family != "hermes":
                errors.append(f"{path}.max_turns is only valid for the Hermes harness")
            else:
                max_turns = raw_entry.get("max_turns")
                if max_turns is not None and (
                    type(max_turns) is not int or max_turns <= 0
                ):
                    errors.append(
                        f"{path}.max_turns must be null or a positive integer"
                    )

        session = raw_entry.get("session")
        if not isinstance(session, Mapping):
            errors.append(f"{path}.session must be a mapping")
        else:
            session_keys = set(map(str, session))
            session_missing = sorted(_SESSION_KEYS - session_keys)
            session_unknown = sorted(session_keys - _SESSION_KEYS)
            if session_missing:
                errors.append(
                    f"{path}.session is missing required fields: {session_missing}"
                )
            if session_unknown:
                errors.append(f"{path}.session has unknown fields: {session_unknown}")
            mode = session.get("mode")
            if mode not in ("fresh", "continue"):
                errors.append(f"{path}.session.mode must be fresh or continue")
            age = session.get("max_age_minutes")
            if age is not None and (type(age) is not int or age <= 0):
                errors.append(
                    f"{path}.session.max_age_minutes must be null or a positive integer"
                )
            elif family == "hermes" and age is not None:
                errors.append(
                    f"{path}.session.max_age_minutes must be null because the "
                    "Hermes TUI runtime does not implement age-gated continuation"
                )

        if (
            "approval_policy" in required
            and raw_entry.get("approval_policy") != "never"
        ):
            errors.append(f"{path}.approval_policy must be 'never'")
        if family == "claude-code" and "account" in raw_entry:
            account = raw_entry.get("account")
            if not isinstance(account, str) or not account.strip():
                errors.append(f"{path}.account must be a non-empty string")
            elif account != account.strip():
                errors.append(
                    f"{path}.account must not have leading or trailing whitespace"
                )
        if (
            "sandbox_mode" in required
            and raw_entry.get("sandbox_mode") != "danger-full-access"
        ):
            errors.append(f"{path}.sandbox_mode must be 'danger-full-access'")

        watchdog = raw_entry.get("watchdog")
        if family == "claude-code" and isinstance(watchdog, Mapping):
            watchdog_keys = set(map(str, watchdog))
            watchdog_missing = sorted(_WATCHDOG_KEYS - watchdog_keys)
            watchdog_unknown = sorted(watchdog_keys - _WATCHDOG_KEYS)
            if watchdog_missing:
                errors.append(
                    f"{path}.watchdog is missing required fields: {watchdog_missing}"
                )
            if watchdog_unknown:
                errors.append(f"{path}.watchdog has unknown fields: {watchdog_unknown}")
            responses = watchdog.get("responses")
            if not isinstance(responses, Mapping):
                errors.append(f"{path}.watchdog.responses must be a mapping")
            else:
                response_keys = set(map(str, responses))
                response_missing = sorted(_WATCHDOG_RESPONSE_KEYS - response_keys)
                response_unknown = sorted(response_keys - _WATCHDOG_RESPONSE_KEYS)
                if response_missing:
                    errors.append(
                        f"{path}.watchdog.responses is missing required fields: "
                        f"{response_missing}"
                    )
                if response_unknown:
                    errors.append(
                        f"{path}.watchdog.responses has unknown fields: "
                        f"{response_unknown}"
                    )

    if "watchdog" in spec:
        errors.append(
            "spec.watchdog is a legacy global field; under the canonical "
            "surface it belongs to spec.available_harnesses.claude-code.watchdog"
        )
    if LEGACY_CONTAINER_KEY in spec:
        errors.append(
            "spec.container is a legacy Docker-era block with no launch-time "
            "consumer; use spec.apptainer for the container image, binds, "
            "environment, and isolation settings"
        )

    try:
        selected = _public_harness(resolve_spec_harness(spec))
    except ValueError:
        selected = None
    if selected is not None and selected not in canonical_keys:
        errors.append(
            f"spec.harness selects {selected!r}, but spec.available_harnesses "
            f"does not declare it; available: {sorted(canonical_keys)}"
        )

    selected_entry = _selected_entry(spec)
    communication_channels = (
        comms.get("channels") if isinstance(comms, Mapping) else None
    )
    if communication_channels is None:
        errors.append(
            "spec.comms.channels is required with spec.available_harnesses; "
            "declare the shared channel set once outside harness entries"
        )
    legacy = spec.get(LEGACY_HARNESS_OPTIONS_KEY)
    if selected_entry is not None and LEGACY_HARNESS_OPTIONS_KEY in spec:
        _, entry = selected_entry
        if not isinstance(legacy, Mapping) or _claude_values(legacy) != _claude_values(
            _claude_compat(entry, communication_channels)
        ):
            errors.append(
                "spec.available_harnesses selected entry and legacy spec.claude "
                "disagree; declare one surface, or make their resolved values identical"
            )
    return errors


def normalize_document(raw: object) -> object:
    """Return a copy with canonical authoring keys folded to v3 internals."""
    if not isinstance(raw, Mapping):
        return raw
    out = copy.deepcopy(dict(raw))
    spec = out.get("spec")
    if not isinstance(spec, dict):
        return out
    if AVAILABLE_ENGINES_KEY in spec:
        spec[LEGACY_ENGINES_KEY] = copy.deepcopy(spec[AVAILABLE_ENGINES_KEY])
    if AVAILABLE_HARNESSES_KEY in spec and LEGACY_CONTAINER_KEY not in spec:
        spec[LEGACY_CONTAINER_KEY] = {
            "image": "scitex-agent-container:latest",
            "volumes": [],
            "network": "host",
            "mount_host_claude": False,
        }
    selected = _selected_entry(spec)
    if selected is not None:
        _, entry = selected
        comms = spec.get("comms")
        channels = comms.get("channels") if isinstance(comms, Mapping) else []
        spec[LEGACY_HARNESS_OPTIONS_KEY] = _claude_compat(entry, channels)
        watchdog = entry.get("watchdog")
        if isinstance(watchdog, Mapping):
            spec["watchdog"] = copy.deepcopy(dict(watchdog))
        elif "watchdog" not in spec:
            spec["watchdog"] = {
                "enabled": False,
                "interval": 1.5,
                "responses": {
                    "y_n": "1",
                    "y_y_n": "2",
                    "waiting": "/speak-and-call",
                },
            }
    return out


__all__ = [
    "AVAILABLE_ENGINES_KEY",
    "AVAILABLE_HARNESSES_KEY",
    "canonical_surface_errors",
    "normalize_document",
]
