from __future__ import annotations

import pytest

from scitex_agent_container.config._startup_spec import (
    StartupSpecError,
    parse_startup,
)


def _startup(**changes):
    value = {
        "environment": {
            "resolve_on": "target-host",
            "conflict_policy": "specification-wins",
            "values": {"EXAMPLE": "authored"},
        },
        "commands": {
            "execution": {
                "location": "apptainer",
                "phase": "before-harness",
                "shell": "/bin/bash -lc",
                "failure": "abort",
            },
            "entries": [{"run": "echo ready", "delay_seconds": 0}],
        },
        "prompts": {
            "execution": {
                "location": "harness",
                "phase": "first-turn",
                "readiness": "required",
            },
            "entries": ["Continue the assigned work."],
        },
    }
    value.update(changes)
    return value


def test_parses_explicit_startup_contract_into_frozen_runtime_values():
    parsed = parse_startup(_startup())
    assert parsed.environment.values == {"EXAMPLE": "authored"}
    assert parsed.commands.entries[0].command == "echo ready"
    assert parsed.commands.entries[0].delay == 0
    assert parsed.prompts.entries == ("Continue the assigned work.",)


def test_missing_execution_semantics_report_exact_yaml_path():
    value = _startup()
    del value["commands"]["execution"]["phase"]
    with pytest.raises(
        StartupSpecError,
        match=r"spec\.startup\.commands\.execution\.phase",
    ):
        parse_startup(value)


def test_unknown_startup_field_is_forbidden_with_exact_path():
    value = _startup()
    value["prompts"]["execution"]["when"] = "later"
    with pytest.raises(
        StartupSpecError,
        match=r"spec\.startup\.prompts\.execution\.when",
    ):
        parse_startup(value)


def test_command_delay_does_not_coerce_strings():
    value = _startup()
    value["commands"]["entries"][0]["delay_seconds"] = "0"
    with pytest.raises(
        StartupSpecError,
        match=r"spec\.startup\.commands\.entries\[0\]\.delay_seconds",
    ):
        parse_startup(value)


def test_empty_lists_are_explicit_and_do_not_inject_hidden_defaults():
    value = _startup()
    value["commands"]["entries"] = []
    value["prompts"]["entries"] = []
    parsed = parse_startup(value)
    assert parsed.commands.entries == ()
    assert parsed.prompts.entries == ()
