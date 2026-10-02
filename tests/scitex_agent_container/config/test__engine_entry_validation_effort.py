"""Tests for reasoning_effort values in engine entries.

sac-effort-xhigh-allowlist-20260929: the fleet runs 13 live specs with
``reasoning_effort: xhigh`` (measured on the authority checkout), but
validation only knew none/low/medium/high — so A2A send, explain, and
start all refused those agents with a 404/validation error. The
allowlist must cover what the fleet actually runs. Ultra stays
rejected: no spec and no harness measurement uses it, and inventing
values is the failure this validator exists to prevent.
"""

from __future__ import annotations

from scitex_agent_container.config._engine_entry_validation import (
    validate_engine_entry,
)


def _entry(**fields: object) -> dict[str, object]:
    base: dict[str, object] = {"harness": "anthropic", "model": "sonnet"}
    base.update(fields)
    return base


def test_xhigh_is_accepted() -> None:
    assert validate_engine_entry("scitex-free", _entry(reasoning_effort="xhigh"), namespace="spec.engines") == []


def test_known_levels_still_accepted() -> None:
    for level in ("none", "low", "medium", "high"):
        assert validate_engine_entry("e", _entry(reasoning_effort=level), namespace="spec.engines") == []


def test_ultra_accepted_as_harness_level() -> None:
    # The harness itself accepts through ultra (hermes chat --help),
    # so validation must not refuse it even with no spec using it yet.
    assert validate_engine_entry("e", _entry(reasoning_effort="ultra"), namespace="spec.engines") == []


def test_garbage_still_rejected() -> None:
    for level in ("extreme", "banana"):
        errors = validate_engine_entry("e", _entry(reasoning_effort=level), namespace="spec.engines")
        assert any("reasoning_effort" in e for e in errors), level
