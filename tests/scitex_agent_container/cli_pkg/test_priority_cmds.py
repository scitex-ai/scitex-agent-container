"""Tests for cli_pkg.priority_cmds: _priority_report, _probe_ssh,
_ssh_start_agent, check-priority, singleton-reconcile."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

from click.testing import CliRunner

import scitex_agent_container.cli_pkg.priority_cmds as pc
from scitex_agent_container.cli_pkg.priority_cmds import (
    _priority_report,
    _probe_ssh,
    _ssh_start_agent,
    priority_check,
    singleton_reconcile,
)

# ---------------------------------------------------------------------------
# _probe_ssh / _ssh_start_agent — subprocess flips
# ---------------------------------------------------------------------------


class _Proc:
    def __init__(self, returncode=0):
        self.returncode = returncode


def test_probe_ssh_returns_true_on_zero(monkeypatch):
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **kw: _Proc(0))
    assert _probe_ssh("h") is True


def test_probe_ssh_returns_false_on_nonzero(monkeypatch):
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **kw: _Proc(1))
    assert _probe_ssh("h") is False


def test_probe_ssh_returns_false_on_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=5)

    monkeypatch.setattr(pc.subprocess, "run", boom)
    assert _probe_ssh("h") is False


def test_probe_ssh_returns_false_on_oserror(monkeypatch):
    def boom(*a, **kw):
        raise OSError("nope")

    monkeypatch.setattr(pc.subprocess, "run", boom)
    assert _probe_ssh("h") is False


def test_ssh_start_agent_success(monkeypatch):
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **kw: _Proc(0))
    assert _ssh_start_agent("h", "ag") is True


def test_ssh_start_agent_failure(monkeypatch):
    monkeypatch.setattr(pc.subprocess, "run", lambda *a, **kw: _Proc(2))
    assert _ssh_start_agent("h", "ag") is False


def test_ssh_start_agent_timeout(monkeypatch):
    def boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=30)

    monkeypatch.setattr(pc.subprocess, "run", boom)
    assert _ssh_start_agent("h", "ag") is False


# ---------------------------------------------------------------------------
# _priority_report — pure-ish logic, mock load_config
# ---------------------------------------------------------------------------


def _make_config(name="x", host=None, hosts=""):
    return SimpleNamespace(
        name=name,
        hosts_spec=SimpleNamespace(host=host or "", hosts=hosts),
    )


def test_priority_report_multi_instance(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(hosts=["a", "b"]))
    out = _priority_report("/p", "a")
    assert out["mode"] == "multi-instance"
    assert out["should_yield"] is False


def test_priority_report_local_singleton_no_host(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=""))
    out = _priority_report("/p", "x")
    assert out["mode"] == "local-singleton"
    assert "no host preference" in out["reason"]


def test_priority_report_local_singleton_empty_chain(monkeypatch):
    # Empty list goes through `not host_val` → "no host preference" path.
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=[]))
    out = _priority_report("/p", "x")
    assert out["mode"] == "local-singleton"
    assert "no host preference" in out["reason"]


def test_priority_report_already_on_preferred(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=["a", "b"]))
    out = _priority_report("/p", "a")
    assert out["should_yield"] is False
    assert out["current_rank"] == 1


def test_priority_report_current_not_in_chain(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=["a", "b"]))
    out = _priority_report("/p", "z")
    assert out["should_yield"] is False
    assert "not in the priority chain" in out["reason"]


def test_priority_report_yield_when_higher_reachable(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=["a", "b", "c"]))
    monkeypatch.setattr(pc, "_probe_ssh", lambda h: h == "a")
    out = _priority_report("/p", "b")
    assert out["should_yield"] is True
    assert out["reachable_higher_hosts"] == ["a"]
    assert out["current_rank"] == 2


def test_priority_report_stay_when_higher_unreachable(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host=["a", "b"]))
    monkeypatch.setattr(pc, "_probe_ssh", lambda h: False)
    out = _priority_report("/p", "b")
    assert out["should_yield"] is False
    assert out["unreachable_higher_hosts"] == ["a"]


def test_priority_report_string_host_normalised(monkeypatch):
    monkeypatch.setattr(pc, "load_config", lambda p: _make_config(host="solo"))
    out = _priority_report("/p", "solo")
    assert out["current_rank"] == 1


# ---------------------------------------------------------------------------
# check-priority CLI
# ---------------------------------------------------------------------------


def test_check_priority_config_not_found(monkeypatch):
    def boom(_):
        raise FileNotFoundError("missing")

    monkeypatch.setattr(pc, "resolve_with_prefix", boom)
    runner = CliRunner()
    result = runner.invoke(priority_check, ["x", "--json"])
    assert result.exit_code == 2
    assert "missing" in result.output


def test_check_priority_report_error(monkeypatch):
    monkeypatch.setattr(pc, "resolve_with_prefix", lambda _: "/p")
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "myhost")
    monkeypatch.setattr(
        pc, "_priority_report", lambda c, h: (_ for _ in ()).throw(RuntimeError("bad"))
    )
    runner = CliRunner()
    result = runner.invoke(priority_check, ["x", "--json"])
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert "bad" in payload["error"]


def test_check_priority_yield_exits_1(monkeypatch):
    monkeypatch.setattr(pc, "resolve_with_prefix", lambda _: "/p")
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "b")
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {
            "agent": "x",
            "should_yield": True,
            "reason": "yield",
            "host_chain": ["a", "b"],
            "reachable_higher_hosts": ["a"],
        },
    )
    runner = CliRunner()
    result = runner.invoke(priority_check, ["x"])
    assert result.exit_code == 1
    assert "YIELD" in result.output


def test_check_priority_stay_exits_0(monkeypatch):
    monkeypatch.setattr(pc, "resolve_with_prefix", lambda _: "/p")
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "a")
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {
            "agent": "x",
            "should_yield": False,
            "reason": "stay",
        },
    )
    runner = CliRunner()
    result = runner.invoke(priority_check, ["x"])
    assert result.exit_code == 0
    assert "STAY" in result.output


def test_check_priority_json_output(monkeypatch):
    monkeypatch.setattr(pc, "resolve_with_prefix", lambda _: "/p")
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "a")
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {"agent": "x", "should_yield": False},
    )
    runner = CliRunner()
    result = runner.invoke(priority_check, ["x", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["agent"] == "x"


def test_check_priority_uses_explicit_current_host(monkeypatch):
    monkeypatch.setattr(pc, "resolve_with_prefix", lambda _: "/p")
    seen = {}

    def fake_report(c, h):
        seen["host"] = h
        return {"agent": "x", "should_yield": False}

    monkeypatch.setattr(pc, "_priority_report", fake_report)
    runner = CliRunner()
    result = runner.invoke(
        priority_check, ["x", "--current-host", "specific", "--json"]
    )
    assert result.exit_code == 0
    assert seen["host"] == "specific"


# ---------------------------------------------------------------------------
# singleton-reconcile
# ---------------------------------------------------------------------------


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries

    def list_all(self):
        return list(self._entries)


def test_reconcile_no_agents_exits_0(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "h")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(_reg, "Registry", lambda: _FakeRegistry([]))
    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, [])
    assert result.exit_code == 0


def test_reconcile_yield_recommended_exits_1(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "b")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(
        _reg, "Registry", lambda: _FakeRegistry([{"name": "ag", "config": "/p"}])
    )

    def report(c, h):
        return {
            "agent": "ag",
            "should_yield": True,
            "preferred_host": "a",
            "reachable_higher_hosts": ["a"],
            "host_chain": ["a", "b"],
            "reason": "yield",
        }

    monkeypatch.setattr(pc, "_priority_report", report)
    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, [])
    assert result.exit_code == 1
    assert "YIELD" in result.output


def test_reconcile_executes_handover(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "b")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(
        _reg, "Registry", lambda: _FakeRegistry([{"name": "ag", "config": "/p"}])
    )
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {
            "agent": "ag",
            "should_yield": True,
            "preferred_host": "a",
            "reachable_higher_hosts": ["a"],
            "host_chain": ["a", "b"],
            "reason": "yield",
        },
    )
    monkeypatch.setattr(pc, "_ssh_start_agent", lambda h, n: True)
    import scitex_agent_container._lifecycle.lifecycle as _l

    monkeypatch.setattr(_l, "agent_stop", lambda n: True)

    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, ["--execute"])
    # Yielded successfully → exit 0 when no errors.
    assert result.exit_code == 0
    assert "yielded" in result.output


def test_reconcile_skips_when_priority_report_raises(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "a")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(
        _reg, "Registry", lambda: _FakeRegistry([{"name": "x", "config": "/p"}])
    )
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: (_ for _ in ()).throw(ValueError("bad-yaml")),
    )
    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, [])
    # No yield + error → exit 2.
    assert result.exit_code == 2
    assert "bad-yaml" in result.output


def test_reconcile_stay_exit_0(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "a")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(
        _reg, "Registry", lambda: _FakeRegistry([{"name": "x", "config": "/p"}])
    )
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {"agent": "x", "should_yield": False, "reason": "stay"},
    )
    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, [])
    assert result.exit_code == 0


def test_reconcile_json_output(monkeypatch):
    monkeypatch.setattr(pc, "resolve_hostname", lambda: "a")
    import scitex_agent_container._state.registry as _reg

    monkeypatch.setattr(
        _reg, "Registry", lambda: _FakeRegistry([{"name": "x", "config": "/p"}])
    )
    monkeypatch.setattr(
        pc,
        "_priority_report",
        lambda c, h: {"agent": "x", "should_yield": False, "reason": "stay"},
    )
    runner = CliRunner()
    result = runner.invoke(singleton_reconcile, ["--json"])
    payload = json.loads(result.output)
    assert payload[0]["agent"] == "x"
