from __future__ import annotations

import pytest

from scitex_agent_container.config._to_home_spec import ToHomeSpecError, parse_to_home


def _layer(**changes):
    value = {
        "id": "per-agent",
        "source": "./to_home",
        "precedence": 100,
        "apply": "pre-launch-on-start-and-restart",
        "destination": ".",
        "mode": "managed-overlay-v1",
        "conflict": "higher-precedence-wins",
        "stale": "preserve",
        "required": True,
    }
    value.update(changes)
    return value


def test_parses_ordered_self_contained_layers():
    parsed = parse_to_home(
        {"imports": [_layer(id="shared", precedence=10), _layer()]}
    )
    assert [item.id for item in parsed.imports] == ["shared", "per-agent"]
    assert parsed.imports[1].conflict == "higher-precedence-wins"


@pytest.mark.parametrize("legacy", ["./to_home", None, ["per-agent"]])
def test_refuses_legacy_or_missing_shape(legacy):
    with pytest.raises(ToHomeSpecError, match="legacy path scalar"):
        parse_to_home(legacy)


def test_requires_every_semantic_field_and_unique_ids():
    incomplete = _layer()
    incomplete.pop("required")
    with pytest.raises(ToHomeSpecError, match="missing=.*required"):
        parse_to_home({"imports": [incomplete]})
    with pytest.raises(ToHomeSpecError, match="duplicates"):
        parse_to_home({"imports": [_layer(), _layer()]})


@pytest.mark.parametrize("target", ["/root", "../escape"])
def test_target_cannot_escape_home(target):
    with pytest.raises(ToHomeSpecError, match="beneath the agent home"):
        parse_to_home({"imports": [_layer(destination=target)]})
