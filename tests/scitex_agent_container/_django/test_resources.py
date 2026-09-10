"""Process-resource observation: honest states, never fabricated values."""

from __future__ import annotations

import os

from scitex_agent_container._django._resources import read_resources


def test_no_pid_is_unavailable():
    assert read_resources(None) == {"state": "unavailable", "pid": None}
    assert read_resources("")["state"] == "unavailable"


def test_bad_pid_is_unavailable():
    assert read_resources("not-a-pid")["state"] == "unavailable"
    assert read_resources(-1)["state"] == "unavailable"


def test_unknown_pid_is_namespace_or_unavailable():
    # A large pid that does not exist in this namespace must NOT be reported as
    # ok with invented numbers.
    out = read_resources(499999999)
    assert out["state"] in {"namespace", "unavailable"}
    assert out.get("rss") in (None, "") or "rss" not in out


def test_own_pid_is_ok_with_real_values():
    out = read_resources(os.getpid())
    assert out["state"] == "ok"
    assert out["pid"] == os.getpid()
    assert isinstance(out["rss"], str) and out["rss"]
    assert isinstance(out["tree_size"], int) and out["tree_size"] >= 1
