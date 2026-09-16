"""Shared validation for one engine-library entry, wherever authored."""

from __future__ import annotations

import re
from typing import Mapping

from ._engine_types import ENGINE_ENTRY_KEYS, ENGINE_PIN_KEY
from ._harness_types import is_known_harness, list_harnesses
from ._provider_validation import validate_provider

__all__ = ["validate_engine_entry"]

_ENGINE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_REASONING_EFFORTS = ("none", "low", "medium", "high")


def validate_engine_entry(key: str, raw: object, *, namespace: str) -> list[str]:
    """Return shape errors for one named engine in a spec or fleet library."""
    path = f"{namespace}.{key}"
    errors: list[str] = []
    if not _ENGINE_KEY_RE.match(key):
        errors.append(
            f"{namespace} key {key!r} is not a usable engine name. It is "
            "typed on a command line (--engine <key>), so it must start "
            "alphanumeric and contain only letters, digits, '.', '_' and '-'."
        )
    if not isinstance(raw, Mapping):
        return errors + [
            f"{path} must be a mapping of engine fields "
            f"({', '.join(sorted(ENGINE_ENTRY_KEYS))}), got "
            f"{type(raw).__name__}."
        ]

    unknown = sorted(set(map(str, raw)) - ENGINE_ENTRY_KEYS)
    if unknown:
        errors.append(
            f"{path} has unknown field(s): {', '.join(unknown)}. An engine "
            f"entry carries {sorted(ENGINE_ENTRY_KEYS)} — the SAME fields "
            "the single-backend surface carries, plus the per-engine "
            "parameters. Anything else belongs in spec.extensions."
        )

    harness = raw.get("harness")
    if harness is not None and str(harness).strip():
        if not is_known_harness(str(harness).strip().lower()):
            errors.append(
                f"{path}.harness must be one of {list_harnesses()} (got "
                f"{str(harness)!r}). It resolves through the SAME harness "
                "registry as spec.harness — an engine cannot invent a "
                "harness the fleet cannot run."
            )

    model = raw.get("model")
    if model is not None and not isinstance(model, str):
        errors.append(f"{path}.model must be a string, got {type(model).__name__}.")

    if "provider" in raw:
        errors += [
            msg.replace("spec.claude.provider", f"{path}.provider")
            for msg in validate_provider(raw.get("provider"))
        ]

    subscription = raw.get("subscription")
    if subscription is not None:
        if not isinstance(subscription, Mapping):
            errors.append(f"{path}.subscription must be a mapping.")
        else:
            unknown_subscription = sorted(
                set(map(str, subscription)) - {"provider", "account"}
            )
            if unknown_subscription:
                errors.append(
                    f"{path}.subscription has unknown field(s): "
                    f"{', '.join(unknown_subscription)}."
                )
            if subscription.get("provider") != "openai":
                errors.append(f"{path}.subscription.provider must be 'openai'.")
            account = subscription.get("account")
            if (
                not isinstance(account, str)
                or not account.strip().startswith("openai:")
                or len(account.strip()) == len("openai:")
            ):
                errors.append(
                    f"{path}.subscription.account must name a collected, qualified "
                    "OpenAI account as openai:<slug>."
                )
        if "provider" in raw:
            errors.append(
                f"{path} cannot declare both provider and subscription; choose "
                "an API endpoint or one collected subscription account."
            )

    default = raw.get("default")
    if default is not None and not isinstance(default, bool):
        errors.append(
            f"{path}.default must be true or false, got "
            f"{type(default).__name__}. (DEPRECATED: `{ENGINE_PIN_KEY}: "
            "<key>` at the top of spec: says the same thing without making "
            "the CHOICE a property of the CHOSEN.)"
        )

    effort = raw.get("reasoning_effort")
    if effort is not None and str(effort).strip():
        if str(effort).strip().lower() not in _REASONING_EFFORTS:
            errors.append(
                f"{path}.reasoning_effort must be one of "
                f"{list(_REASONING_EFFORTS)} (got {str(effort)!r})."
            )

    max_ctx = raw.get("max_context_tokens")
    if max_ctx is not None:
        if isinstance(max_ctx, bool) or not isinstance(max_ctx, int):
            errors.append(
                f"{path}.max_context_tokens must be a positive integer, got "
                f"{type(max_ctx).__name__}."
            )
        elif max_ctx <= 0:
            errors.append(f"{path}.max_context_tokens must be > 0, got {max_ctx}.")

    timeouts = raw.get("timeouts")
    if timeouts is not None:
        if not isinstance(timeouts, Mapping):
            errors.append(
                f"{path}.timeouts must be a mapping of upstream_deadline_seconds "
                "and client_abandonment_seconds."
            )
        else:
            allowed = {
                "upstream_deadline_seconds",
                "client_abandonment_seconds",
            }
            unknown_timeouts = sorted(set(map(str, timeouts)) - allowed)
            if unknown_timeouts:
                errors.append(
                    f"{path}.timeouts has unknown field(s): "
                    f"{', '.join(unknown_timeouts)}."
                )
            upstream = timeouts.get("upstream_deadline_seconds")
            abandonment = timeouts.get("client_abandonment_seconds")
            for timeout_name, timeout_value in (
                ("upstream_deadline_seconds", upstream),
                ("client_abandonment_seconds", abandonment),
            ):
                if timeout_value is not None and (
                    isinstance(timeout_value, bool)
                    or not isinstance(timeout_value, int)
                    or timeout_value <= 0
                ):
                    errors.append(
                        f"{path}.timeouts.{timeout_name} must be a positive integer."
                    )
            if (upstream is None) != (abandonment is None):
                errors.append(
                    f"{path}.timeouts must declare upstream_deadline_seconds and "
                    "client_abandonment_seconds together."
                )
            elif (
                isinstance(upstream, int)
                and not isinstance(upstream, bool)
                and isinstance(abandonment, int)
                and not isinstance(abandonment, bool)
                and abandonment <= upstream
            ):
                errors.append(
                    f"{path}.timeouts.client_abandonment_seconds must be greater "
                    "than upstream_deadline_seconds."
                )

    env = raw.get("env")
    if env is not None and not isinstance(env, Mapping):
        errors.append(
            f"{path}.env must be a mapping of env var name → value, got "
            f"{type(env).__name__}."
        )
    return errors
