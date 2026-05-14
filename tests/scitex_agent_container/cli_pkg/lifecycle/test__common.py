"""Tests for cli_pkg.lifecycle._common.

Covers _singleton_skip_reason, _iter_agent_yamls, _discover_all_agents,
and the _multiplex_foreground_tails loop (with synthetic session.jsonl
+ a heartbeat that flips to 'stopping').
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._common import (
    _discover_all_agents,
    _iter_agent_yamls,
    _multiplex_foreground_tails,
    _singleton_skip_reason,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._types import HostsSpec, SchedulingSpec


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))


# ---------------------------------------------------------------------------
# _singleton_skip_reason
# ---------------------------------------------------------------------------


def _cfg(host="", hosts=None, sched_mode="per-host", pref="", fb=None) -> AgentConfig:
    c = AgentConfig(name="a")
    c.hosts_spec = HostsSpec(host=host, hosts=hosts or [])
    c.scheduling = SchedulingSpec(
        mode=sched_mode, preferred_host=pref, fallback_hosts=fb or []
    )
    return c


class TestSingletonSkipReason:
    def test_multi_instance_never_skips(self):
        assert _singleton_skip_reason(_cfg(hosts=["a", "b"]), "z") is None

    def test_no_host_pref_runs_anywhere(self):
        assert _singleton_skip_reason(_cfg(), "anyhost") is None

    def test_v3_host_str_match(self):
        assert _singleton_skip_reason(_cfg(host="alpha"), "alpha") is None

    def test_v3_host_str_mismatch(self):
        msg = _singleton_skip_reason(_cfg(host="alpha"), "beta")
        assert msg and "alpha" in msg and "beta" in msg

    def test_v3_host_chain_primary_match(self):
        assert _singleton_skip_reason(_cfg(host=["a", "b", "c"]), "a") is None

    def test_v3_host_chain_fallback_match(self):
        assert _singleton_skip_reason(_cfg(host=["a", "b", "c"]), "b") is None

    def test_v3_host_chain_no_match(self):
        msg = _singleton_skip_reason(_cfg(host=["a", "b"]), "z")
        assert msg and "fallback-hosts: b" in msg

    def test_v3_empty_chain_treated_as_no_host(self):
        assert _singleton_skip_reason(_cfg(host=[]), "anyhost") is None

    def test_v2_singleton_pref_match(self):
        c = _cfg(sched_mode="singleton", pref="alpha")
        assert _singleton_skip_reason(c, "alpha") is None

    def test_v2_singleton_mismatch_with_fallback(self):
        c = _cfg(sched_mode="singleton", pref="alpha", fb=["beta", "gamma"])
        msg = _singleton_skip_reason(c, "zeta")
        assert msg and "alpha" in msg and "beta, gamma" in msg

    def test_v2_singleton_no_pref_runs_anywhere(self):
        c = _cfg(sched_mode="singleton", pref="")
        assert _singleton_skip_reason(c, "anyhost") is None

    def test_v2_non_singleton_skips(self):
        c = _cfg(sched_mode="per-host", pref="alpha")
        assert _singleton_skip_reason(c, "beta") is None


# ---------------------------------------------------------------------------
# _iter_agent_yamls
# ---------------------------------------------------------------------------


class TestIterAgentYamls:
    def test_missing_dir(self, tmp_path):
        assert _iter_agent_yamls(tmp_path / "nope") == []

    def test_yaml_and_yml_and_skips(self, tmp_path):
        # foo with foo.yaml
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo" / "foo.yaml").write_text("x")
        # bar with bar.yml
        (tmp_path / "bar").mkdir()
        (tmp_path / "bar" / "bar.yml").write_text("x")
        # hidden _legacy dir - skip
        (tmp_path / "_legacy").mkdir()
        (tmp_path / "_legacy" / "_legacy.yaml").write_text("x")
        # .git
        (tmp_path / ".git").mkdir()
        # reserved
        (tmp_path / "legacy-agents").mkdir()
        (tmp_path / "legacy-agents" / "legacy-agents.yaml").write_text("x")
        # plain file (non-dir) at root
        (tmp_path / "loose.yaml").write_text("x")
        # subdir without matching yaml
        (tmp_path / "empty").mkdir()

        result = _iter_agent_yamls(tmp_path)
        names = [n for n, _ in result]
        assert names == ["bar", "foo"]
        assert all(p.endswith((".yaml", ".yml")) for _, p in result)


# ---------------------------------------------------------------------------
# _discover_all_agents
# ---------------------------------------------------------------------------


class TestDiscoverAllAgents:
    def test_discovers_under_home(self, tmp_path, monkeypatch):
        agents = tmp_path / ".scitex" / "agent-container" / "agents"
        (agents / "foo").mkdir(parents=True)
        (agents / "foo" / "foo.yaml").write_text("x")
        (agents / "bar").mkdir()
        (agents / "bar" / "bar.yaml").write_text("x")
        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
        # Suppress project-local: stub _project_local_dirs to []
        monkeypatch.setattr(
            "scitex_agent_container.config._resolve._project_local_dirs",
            lambda: [],
        )
        result = _discover_all_agents()
        names = [Path(p).parent.name for p in result]
        assert sorted(names) == ["bar", "foo"]

    def test_env_var_extra_dirs(self, tmp_path, monkeypatch):
        extra = tmp_path / "extra"
        (extra / "zed").mkdir(parents=True)
        (extra / "zed" / "zed.yaml").write_text("x")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))
        monkeypatch.setattr(
            "scitex_agent_container.config._resolve._project_local_dirs",
            lambda: [],
        )
        result = _discover_all_agents()
        assert any("zed" in p for p in result)

    def test_project_local_priority(self, tmp_path, monkeypatch):
        # Same name in two locations — project-local wins.
        local = tmp_path / "local"
        (local / "foo").mkdir(parents=True)
        local_yaml = local / "foo" / "foo.yaml"
        local_yaml.write_text("local")

        home_agents = tmp_path / ".scitex" / "agent-container" / "agents"
        (home_agents / "foo").mkdir(parents=True)
        (home_agents / "foo" / "foo.yaml").write_text("home")

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
        monkeypatch.setattr(
            "scitex_agent_container.config._resolve._project_local_dirs",
            lambda: [local],
        )
        result = _discover_all_agents()
        assert len(result) == 1
        assert result[0] == str(local_yaml)


# ---------------------------------------------------------------------------
# _multiplex_foreground_tails
# ---------------------------------------------------------------------------


def _runtime_for(tmp_path: Path, name: str) -> Path:
    d = tmp_path / ".scitex" / "agent-container" / "runtime" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hb(d: Path, state: str) -> None:
    (d / "heartbeat.json").write_text(json.dumps({"state": state}))


class TestMultiplexForegroundTails:
    def test_stops_when_heartbeat_says_stopping(self, tmp_path, monkeypatch, capsys):
        rt = _runtime_for(tmp_path, "alpha")
        # session.jsonl with one assistant, one result, one error, one bad json
        rt.joinpath("session.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"type": "assistant", "text": "hello world"}),
                    json.dumps({"type": "result"}),
                    json.dumps({"type": "error", "detail": "boom"}),
                    "not-json-line",
                ]
            )
            + "\n"
        )
        # Pre-existing offset model: writes after start are tailed; but since
        # the multiplexer snapshots size at the start of the call, we set
        # heartbeat to "stopping" so it loops once then stops. To still see
        # the lines, write the file AFTER offsets are recorded.
        _hb(rt, "running")

        # Patch time.sleep to flip to stopping after first iteration

        original_sleep = __import__("time").sleep

        call_count = {"n": 0}

        def fake_sleep(_):
            call_count["n"] += 1
            if call_count["n"] >= 1:
                _hb(rt, "stopping")

        # Patch the imported time module inside the function. The function
        # does `import time as _time` so monkeypatch time.sleep globally.
        monkeypatch.setattr("time.sleep", fake_sleep)

        # Pre-truncate then rewrite to ensure offsets = 0 so the lines are read.
        rt.joinpath("session.jsonl").unlink()
        # heartbeat is "running" now; multiplexer enters loop, no file → any_progress False → sleep → flips stopping.
        # But we also want to test the read path. Recreate file before any sleep.
        # Simplest: write file, then offsets = file size, content already counted as 0 to read.
        # So instead, start with empty file (size 0), then on next sleep call write content & set stopping.

        def fake_sleep2(_):
            call_count["n"] += 1
            # On first sleep, write content then flip stopping after another pass.
            if call_count["n"] == 1:
                rt.joinpath("session.jsonl").write_text(
                    "\n".join(
                        [
                            json.dumps({"type": "assistant", "text": "hello"}),
                            json.dumps({"type": "result"}),
                            json.dumps({"type": "error", "detail": "boom"}),
                            "raw-line",
                        ]
                    )
                    + "\n"
                )
            else:
                _hb(rt, "stopping")

        rt.joinpath("session.jsonl").write_text("")  # size 0
        monkeypatch.setattr("time.sleep", fake_sleep2)

        _multiplex_foreground_tails(["alpha"])
        out = capsys.readouterr().out
        assert "[alpha] [assistant] hello" in out
        assert "[alpha] [result]" in out
        assert "[alpha] [error] boom" in out
        assert "[alpha] raw-line" in out
        assert "[alpha] (stopped)" in out

    def test_missing_session_file_then_stopping(self, tmp_path, monkeypatch, capsys):
        rt = _runtime_for(tmp_path, "beta")
        # heartbeat absent initially → _is_stopping returns False → loop runs once
        # then we flip to stopping. session.jsonl never appears.
        calls = {"n": 0}

        def fake_sleep(_):
            calls["n"] += 1
            _hb(rt, "stopping")

        monkeypatch.setattr("time.sleep", fake_sleep)
        _multiplex_foreground_tails(["beta"])
        # No output expected for absent jsonl, but no crash.

    def test_keyboard_interrupt(self, tmp_path, monkeypatch, capsys):
        rt = _runtime_for(tmp_path, "gamma")
        rt.joinpath("session.jsonl").write_text("")

        def fake_sleep(_):
            raise KeyboardInterrupt

        monkeypatch.setattr("time.sleep", fake_sleep)
        _multiplex_foreground_tails(["gamma"])
        out = capsys.readouterr().out
        assert "interrupted" in out

    def test_starts_at_eof_for_existing_file(self, tmp_path, monkeypatch, capsys):
        rt = _runtime_for(tmp_path, "delta")
        # Pre-populate; multiplexer should skip these lines (start at EOF)
        rt.joinpath("session.jsonl").write_text(
            json.dumps({"type": "assistant", "text": "old"}) + "\n"
        )

        def fake_sleep(_):
            _hb(rt, "stopping")

        monkeypatch.setattr("time.sleep", fake_sleep)
        _multiplex_foreground_tails(["delta"])
        out = capsys.readouterr().out
        assert "old" not in out
        assert "[delta] (stopped)" in out
