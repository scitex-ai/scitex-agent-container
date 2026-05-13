"""Tests for config._parsers._helpers (get_nested, interpolate_metadata,
_parse_command_list)."""

from __future__ import annotations

from scitex_agent_container.config._parsers._helpers import (
    HOOK_KEYS,
    MODEL_DISPLAY_NAMES,
    _parse_command_list,
    get_nested,
    interpolate_metadata,
)


def test_hook_keys_includes_known_lifecycle():
    assert "pre_start" in HOOK_KEYS
    assert "post_stop" in HOOK_KEYS
    assert "on_compact" in HOOK_KEYS


def test_model_display_names_known_aliases():
    assert MODEL_DISPLAY_NAMES["opus[1m]"] == "Claude Opus (1M)"
    assert "haiku" in MODEL_DISPLAY_NAMES


# ---------------------------------------------------------------------------
# get_nested
# ---------------------------------------------------------------------------


def test_get_nested_simple_key():
    assert get_nested({"a": 1}, "a") == 1


def test_get_nested_dotted_path():
    assert get_nested({"a": {"b": {"c": "deep"}}}, "a.b.c") == "deep"


def test_get_nested_missing_returns_default():
    assert get_nested({"a": {}}, "a.b.c", default="X") == "X"


def test_get_nested_default_is_none_by_default():
    assert get_nested({}, "x.y") is None


def test_get_nested_intermediate_not_dict_returns_default():
    assert get_nested({"a": "string"}, "a.b", default="d") == "d"


# ---------------------------------------------------------------------------
# interpolate_metadata
# ---------------------------------------------------------------------------


def test_interpolate_metadata_name():
    out = interpolate_metadata("hello ${metadata.name}", {"name": "agent-7"})
    assert out == "hello agent-7"


def test_interpolate_metadata_label():
    out = interpolate_metadata(
        "tier=${metadata.labels.tier}", {"labels": {"tier": "edge"}}
    )
    assert out == "tier=edge"


def test_interpolate_metadata_missing_name_keeps_placeholder():
    out = interpolate_metadata("x=${metadata.name}", {})
    assert out == "x=${metadata.name}"


def test_interpolate_metadata_missing_label_keeps_placeholder():
    out = interpolate_metadata("x=${metadata.labels.zz}", {"labels": {}})
    assert out == "x=${metadata.labels.zz}"


def test_interpolate_metadata_labels_field_none_keeps_placeholder():
    out = interpolate_metadata("x=${metadata.labels.zz}", {"labels": None})
    assert out == "x=${metadata.labels.zz}"


def test_interpolate_metadata_unknown_key_left_intact():
    out = interpolate_metadata("${metadata.other.thing}", {"name": "a"})
    assert out == "${metadata.other.thing}"


def test_interpolate_metadata_no_placeholders_unchanged():
    assert interpolate_metadata("nothing here", {"name": "x"}) == "nothing here"


def test_interpolate_metadata_multiple_substitutions():
    out = interpolate_metadata(
        "n=${metadata.name} t=${metadata.labels.tier}",
        {"name": "alpha", "labels": {"tier": "core"}},
    )
    assert out == "n=alpha t=core"


# ---------------------------------------------------------------------------
# _parse_command_list
# ---------------------------------------------------------------------------


def test_parse_command_list_none_yields_empty():
    assert _parse_command_list(None) == []


def test_parse_command_list_empty_list_yields_empty():
    assert _parse_command_list([]) == []


def test_parse_command_list_strings_become_zero_delay_commands():
    out = _parse_command_list(["echo a", "echo b"])
    assert [(c.delay, c.command) for c in out] == [(0, "echo a"), (0, "echo b")]


def test_parse_command_list_empty_string_skipped():
    out = _parse_command_list(["", "echo go"])
    assert [c.command for c in out] == ["echo go"]


def test_parse_command_list_dict_with_delay():
    out = _parse_command_list([{"command": "x", "delay": 7}])
    assert out[0].delay == 7
    assert out[0].command == "x"


def test_parse_command_list_dict_delay_invalid_falls_back_to_zero():
    out = _parse_command_list([{"command": "x", "delay": "junk"}])
    assert out[0].delay == 0


def test_parse_command_list_dict_without_command_skipped():
    out = _parse_command_list([{"delay": 5}, {"command": "ok"}])
    assert [c.command for c in out] == ["ok"]


def test_parse_command_list_mixed_skips_non_str_non_dict():
    out = _parse_command_list(["echo", 42, None, {"command": "go"}])
    assert [c.command for c in out] == ["echo", "go"]
