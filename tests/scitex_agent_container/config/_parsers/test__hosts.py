"""Tests for config._parsers._hosts: parse_hosts_spec + parse_scheduling."""

from __future__ import annotations

import pytest

from scitex_agent_container.config._parsers._hosts import (
    parse_hosts_spec,
    parse_scheduling,
)

# ---------------------------------------------------------------------------
# parse_hosts_spec
# ---------------------------------------------------------------------------


def test_hosts_neither_field_yields_empty():
    h = parse_hosts_spec({})
    assert h.host == ""
    assert h.hosts == ""


def test_hosts_host_string():
    h = parse_hosts_spec({"host": "alpha"})
    assert h.host == "alpha"
    assert h.hosts == ""


def test_hosts_host_list_normalised_to_str():
    h = parse_hosts_spec({"host": ["a", 1, "b"]})
    assert h.host == ["a", "1", "b"]


def test_hosts_host_explicit_none_yields_empty():
    h = parse_hosts_spec({"host": None})
    assert h.host == ""
    assert h.hosts == ""


def test_hosts_hosts_all_keyword():
    h = parse_hosts_spec({"hosts": "all"})
    assert h.host == ""
    assert h.hosts == "all"


def test_hosts_hosts_list():
    h = parse_hosts_spec({"hosts": ["x", "y"]})
    assert h.hosts == ["x", "y"]


def test_hosts_unsupported_host_type_treated_as_empty():
    # int gets validator-rejected elsewhere; parser just normalises to empty
    h = parse_hosts_spec({"host": 42})
    assert h.host == ""


# ---------------------------------------------------------------------------
# parse_scheduling
# ---------------------------------------------------------------------------


def test_scheduling_absent_returns_default_implicit():
    sched, explicit = parse_scheduling({})
    assert explicit is False
    assert sched.mode == "per-host"  # SchedulingSpec default
    assert sched.preferred_host == ""
    assert sched.fallback_hosts == []


def test_scheduling_present_marks_explicit():
    sched, explicit = parse_scheduling({"scheduling": {"mode": "singleton"}})
    assert explicit is True
    assert sched.mode == "singleton"


def test_scheduling_default_mode_when_block_empty():
    sched, explicit = parse_scheduling({"scheduling": {}})
    assert explicit is True
    assert sched.mode == "per-host"


def test_scheduling_invalid_mode_raises():
    with pytest.raises(ValueError, match="mode"):
        parse_scheduling({"scheduling": {"mode": "swarm"}})


def test_scheduling_non_dict_block_raises():
    with pytest.raises(ValueError, match="mapping"):
        parse_scheduling({"scheduling": ["a"]})


def test_scheduling_preferred_host_hyphen_form():
    sched, _ = parse_scheduling(
        {"scheduling": {"preferred-host": "node1", "fallback-hosts": ["n2", "n3"]}}
    )
    assert sched.preferred_host == "node1"
    assert sched.fallback_hosts == ["n2", "n3"]


def test_scheduling_preferred_host_underscore_form():
    sched, _ = parse_scheduling(
        {
            "scheduling": {
                "preferred_host": "node-u",
                "fallback_hosts": "single-fallback",
            }
        }
    )
    assert sched.preferred_host == "node-u"
    # Single string lifted into a list.
    assert sched.fallback_hosts == ["single-fallback"]


def test_scheduling_fallback_hosts_coerced_to_str():
    sched, _ = parse_scheduling({"scheduling": {"fallback_hosts": [1, "two", 3.0]}})
    assert sched.fallback_hosts == ["1", "two", "3.0"]


def test_scheduling_none_block_treated_as_empty_dict():
    sched, explicit = parse_scheduling({"scheduling": None})
    assert explicit is True
    assert sched.mode == "per-host"
