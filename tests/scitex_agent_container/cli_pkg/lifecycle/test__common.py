"""Tests for cli_pkg.lifecycle._common.

Covers _singleton_skip_reason, _iter_agent_yamls, _discover_all_agents,
and the _multiplex_foreground_tails loop (with synthetic session.jsonl
+ a heartbeat that flips to 'stopping').

No-mocks pattern: HOME is redirected via env (Path.home reads $HOME),
``_discover_all_agents`` accepts a ``project_local_dirs`` callable, and
``_multiplex_foreground_tails`` accepts a ``sleeper`` callable.
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
def _isolate_home(tmp_path: Path, env_save_restore):
    # Redirect $HOME so Path.home() returns tmp_path naturally — no
    # module-attribute swap required.
    env_save_restore.set("HOME", str(tmp_path))


# ---------------------------------------------------------------------------
# _singleton_skip_reason — pure logic.
# ---------------------------------------------------------------------------


def _cfg(host="", hosts=None, sched_mode="per-host", pref="", fb=None) -> AgentConfig:
    c = AgentConfig(name="a")
    c.hosts_spec = HostsSpec(host=host, hosts=hosts or [])
    c.scheduling = SchedulingSpec(
        mode=sched_mode, preferred_host=pref, fallback_hosts=fb or []
    )
    return c


class TestSingletonSkipReason:
    def test_multi_instance_returns_none(self):
        # Arrange
        cfg = _cfg(hosts=["a", "b"])
        # Act
        msg = _singleton_skip_reason(cfg, "z")
        # Assert
        assert msg is None

    def test_no_host_pref_returns_none(self):
        # Arrange
        cfg = _cfg()
        # Act
        msg = _singleton_skip_reason(cfg, "anyhost")
        # Assert
        assert msg is None

    def test_v3_host_str_match_returns_none(self):
        # Arrange
        cfg = _cfg(host="alpha")
        # Act
        msg = _singleton_skip_reason(cfg, "alpha")
        # Assert
        assert msg is None

    def test_v3_host_str_mismatch_returns_reason(self):
        # Arrange
        cfg = _cfg(host="alpha")
        # Act
        msg = _singleton_skip_reason(cfg, "beta")
        # Assert
        assert msg and "alpha" in msg and "beta" in msg

    def test_v3_host_chain_primary_match_returns_none(self):
        # Arrange
        cfg = _cfg(host=["a", "b", "c"])
        # Act
        msg = _singleton_skip_reason(cfg, "a")
        # Assert
        assert msg is None

    def test_v3_host_chain_fallback_match_returns_none(self):
        # Arrange
        cfg = _cfg(host=["a", "b", "c"])
        # Act
        msg = _singleton_skip_reason(cfg, "b")
        # Assert
        assert msg is None

    def test_v3_host_chain_no_match_lists_fallbacks(self):
        # Arrange
        cfg = _cfg(host=["a", "b"])
        # Act
        msg = _singleton_skip_reason(cfg, "z")
        # Assert
        assert msg and "fallback-hosts: b" in msg

    def test_v3_empty_chain_treated_as_no_host(self):
        # Arrange
        cfg = _cfg(host=[])
        # Act
        msg = _singleton_skip_reason(cfg, "anyhost")
        # Assert
        assert msg is None

    def test_v2_singleton_pref_match_returns_none(self):
        # Arrange
        cfg = _cfg(sched_mode="singleton", pref="alpha")
        # Act
        msg = _singleton_skip_reason(cfg, "alpha")
        # Assert
        assert msg is None

    def test_v2_singleton_mismatch_lists_fallbacks(self):
        # Arrange
        cfg = _cfg(sched_mode="singleton", pref="alpha", fb=["beta", "gamma"])
        # Act
        msg = _singleton_skip_reason(cfg, "zeta")
        # Assert
        assert msg and "alpha" in msg and "beta, gamma" in msg

    def test_v2_singleton_no_pref_returns_none(self):
        # Arrange
        cfg = _cfg(sched_mode="singleton", pref="")
        # Act
        msg = _singleton_skip_reason(cfg, "anyhost")
        # Assert
        assert msg is None

    def test_v2_non_singleton_returns_none(self):
        # Arrange
        cfg = _cfg(sched_mode="per-host", pref="alpha")
        # Act
        msg = _singleton_skip_reason(cfg, "beta")
        # Assert
        assert msg is None


# ---------------------------------------------------------------------------
# _iter_agent_yamls — real filesystem fixtures.
# ---------------------------------------------------------------------------


class TestIterAgentYamls:
    def test_returns_empty_for_missing_dir(self, tmp_path):
        # Arrange
        missing = tmp_path / "nope"
        # Act
        result = _iter_agent_yamls(missing)
        # Assert
        assert result == []

    def test_discovers_yaml_yml_and_skips_legacy(self, tmp_path):
        # Arrange
        (tmp_path / "foo").mkdir()
        (tmp_path / "foo" / "foo.yaml").write_text("x")
        (tmp_path / "bar").mkdir()
        (tmp_path / "bar" / "bar.yml").write_text("x")
        (tmp_path / "_legacy").mkdir()
        (tmp_path / "_legacy" / "_legacy.yaml").write_text("x")
        (tmp_path / ".git").mkdir()
        (tmp_path / "legacy-agents").mkdir()
        (tmp_path / "legacy-agents" / "legacy-agents.yaml").write_text("x")
        (tmp_path / "loose.yaml").write_text("x")
        (tmp_path / "empty").mkdir()
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert [n for n, _ in result] == ["bar", "foo"]


# ---------------------------------------------------------------------------
# _discover_all_agents — uses the injected project_local_dirs callable.
# ---------------------------------------------------------------------------


class TestDiscoverAllAgents:
    def test_discovers_under_home_directory(self, tmp_path, env_save_restore):
        # Arrange
        agents = tmp_path / ".scitex" / "agent-container" / "agents"
        (agents / "foo").mkdir(parents=True)
        (agents / "foo" / "foo.yaml").write_text("x")
        (agents / "bar").mkdir()
        (agents / "bar" / "bar.yaml").write_text("x")
        env_save_restore.delete("SCITEX_AGENT_CONTAINER_YAML_DIRS")
        env_save_restore.delete("SAC_YAML_DIRS")
        # Act
        result = _discover_all_agents(project_local_dirs=lambda: [])
        # Assert
        assert sorted(Path(p).parent.name for p in result) == ["bar", "foo"]

    def test_env_var_extra_dirs_are_searched(self, tmp_path, env_save_restore):
        # Arrange
        extra = tmp_path / "extra"
        (extra / "zed").mkdir(parents=True)
        (extra / "zed" / "zed.yaml").write_text("x")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))
        env_save_restore.delete("SAC_YAML_DIRS")
        # Act
        result = _discover_all_agents(project_local_dirs=lambda: [])
        # Assert
        assert any("zed" in p for p in result)

    def test_project_local_dirs_win_over_home(self, tmp_path, env_save_restore):
        # Arrange
        local = tmp_path / "local"
        (local / "foo").mkdir(parents=True)
        local_yaml = local / "foo" / "foo.yaml"
        local_yaml.write_text("local")
        home_agents = tmp_path / ".scitex" / "agent-container" / "agents"
        (home_agents / "foo").mkdir(parents=True)
        (home_agents / "foo" / "foo.yaml").write_text("home")
        env_save_restore.delete("SCITEX_AGENT_CONTAINER_YAML_DIRS")
        env_save_restore.delete("SAC_YAML_DIRS")
        # Act
        result = _discover_all_agents(project_local_dirs=lambda: [local])
        # Assert
        assert result == [str(local_yaml)]


# ---------------------------------------------------------------------------
# _multiplex_foreground_tails — uses the injected sleeper callable.
# ---------------------------------------------------------------------------


def _runtime_for(tmp_path: Path, name: str) -> Path:
    d = tmp_path / ".scitex" / "agent-container" / "runtime" / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _hb(d: Path, state: str) -> None:
    (d / "heartbeat.json").write_text(json.dumps({"state": state}))


class TestMultiplexForegroundTails:
    def test_stops_when_heartbeat_flips_to_stopping(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "alpha")
        _hb(rt, "running")
        rt.joinpath("session.jsonl").write_text("")  # size-0 baseline
        call_count = {"n": 0}

        def sleeper(_seconds):
            # On first sleep, write content; on second, flip to stopping.
            call_count["n"] += 1
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

        # Act
        _multiplex_foreground_tails(["alpha"], sleeper=sleeper)
        # Assert
        out = capsys.readouterr().out
        assert "[alpha] [assistant] hello" in out

    def test_renders_result_line(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "alpha")
        _hb(rt, "running")
        rt.joinpath("session.jsonl").write_text("")
        n = {"i": 0}

        def sleeper(_s):
            n["i"] += 1
            if n["i"] == 1:
                rt.joinpath("session.jsonl").write_text(
                    json.dumps({"type": "result"}) + "\n"
                )
            else:
                _hb(rt, "stopping")

        # Act
        _multiplex_foreground_tails(["alpha"], sleeper=sleeper)
        # Assert
        assert "[alpha] [result]" in capsys.readouterr().out

    def test_renders_error_detail(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "alpha")
        _hb(rt, "running")
        rt.joinpath("session.jsonl").write_text("")
        n = {"i": 0}

        def sleeper(_s):
            n["i"] += 1
            if n["i"] == 1:
                rt.joinpath("session.jsonl").write_text(
                    json.dumps({"type": "error", "detail": "boom"}) + "\n"
                )
            else:
                _hb(rt, "stopping")

        # Act
        _multiplex_foreground_tails(["alpha"], sleeper=sleeper)
        # Assert
        assert "[alpha] [error] boom" in capsys.readouterr().out

    def test_renders_raw_line_when_jsonl_malformed(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "alpha")
        _hb(rt, "running")
        rt.joinpath("session.jsonl").write_text("")
        n = {"i": 0}

        def sleeper(_s):
            n["i"] += 1
            if n["i"] == 1:
                rt.joinpath("session.jsonl").write_text("not-valid-json\n")
            else:
                _hb(rt, "stopping")

        # Act
        _multiplex_foreground_tails(["alpha"], sleeper=sleeper)
        # Assert
        assert "[alpha] not-valid-json" in capsys.readouterr().out

    def test_emits_stopped_banner_after_state_flip(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "alpha")
        _hb(rt, "running")
        rt.joinpath("session.jsonl").write_text("")

        def sleeper(_s):
            _hb(rt, "stopping")

        # Act
        _multiplex_foreground_tails(["alpha"], sleeper=sleeper)
        # Assert
        assert "[alpha] (stopped)" in capsys.readouterr().out

    def test_tolerates_absent_session_jsonl_without_raising(self, tmp_path):
        # Arrange — no session.jsonl on disk; loop must not crash.
        rt = _runtime_for(tmp_path, "beta")

        def sleeper(_s):
            _hb(rt, "stopping")

        # Act
        result = _multiplex_foreground_tails(["beta"], sleeper=sleeper)
        # Assert
        # Production contract: function returns (does not raise) even
        # when an agent's session.jsonl file never appears.
        assert result is None

    def test_keyboard_interrupt_emits_interrupted_message(self, tmp_path, capsys):
        # Arrange
        rt = _runtime_for(tmp_path, "gamma")
        rt.joinpath("session.jsonl").write_text("")

        def sleeper(_s):
            raise KeyboardInterrupt

        # Act
        _multiplex_foreground_tails(["gamma"], sleeper=sleeper)
        # Assert
        assert "interrupted" in capsys.readouterr().out

    def test_starts_at_eof_for_pre_existing_session_file(self, tmp_path, capsys):
        # Arrange — populate session.jsonl BEFORE the loop snapshots offsets.
        rt = _runtime_for(tmp_path, "delta")
        rt.joinpath("session.jsonl").write_text(
            json.dumps({"type": "assistant", "text": "old"}) + "\n"
        )

        def sleeper(_s):
            _hb(rt, "stopping")

        # Act
        _multiplex_foreground_tails(["delta"], sleeper=sleeper)
        # Assert
        assert "old" not in capsys.readouterr().out
