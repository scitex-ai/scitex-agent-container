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

from scitex_agent_container._state.host_config import PeerSpec
from scitex_agent_container.cli_pkg.lifecycle._common import (
    _bound_host,
    _discover_all_agents,
    _iter_agent_yamls,
    _local_host_names,
    _multiplex_foreground_tails,
    _registry_active_on,
    _resolve_dispatch_peer,
    _resolve_singleton_skip,
    _singleton_skip_reason,
    classify_dispatch_host,
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

    def test_v3_host_pin_naming_this_machine_by_another_name_returns_none(self):
        # Arrange — the nas-03 shape: the pin IS this machine, spelled the way
        # the fleet spells it, while `hostname -s` is the appliance's factory
        # name. A skip here would be a SILENT no-start on the agent's own host.
        cfg = _cfg(host="scitex-nas-03")
        # Act
        msg = _singleton_skip_reason(
            cfg, "DXP480TPLUS-994", local_names={"scitex-nas-03"}
        )
        # Assert
        assert msg is None

    def test_v3_host_pin_naming_a_different_machine_still_returns_reason(self):
        # Arrange — the case that must KEEP skipping: the pin names a machine
        # this one is not, under any of its spellings.
        cfg = _cfg(host="scitex-nas-03")
        # Act
        msg = _singleton_skip_reason(cfg, "DXP480TPLUS-994", local_names={"nas-99"})
        # Assert
        assert msg and "scitex-nas-03" in msg

    def test_v2_preferred_host_naming_this_machine_by_another_name_returns_none(
        self,
    ):
        # Arrange — same identity question on the v2 scheduling spec.
        cfg = _cfg(sched_mode="singleton", pref="scitex-nas-03")
        # Act
        msg = _singleton_skip_reason(
            cfg, "DXP480TPLUS-994", local_names={"scitex-nas-03"}
        )
        # Assert
        assert msg is None

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
# _resolve_singleton_skip — gated wrapper around _singleton_skip_reason.
#
# Bug 1 root cause: `_start_single` consulted `_singleton_skip_reason`
# even with `no_redispatch=True`, so a singleton-on-wrong-host check
# silently no-op'd starts that had NO other host to defer to (the
# `--on <peer>` propagated remote call sets `--no-redispatch`, so the
# skip path was a permanent dead-end). The gate skips the singleton
# check whenever `no_redispatch=True` — the operator's explicit
# "do it here" signal overrides the host-pinning preference.
# ---------------------------------------------------------------------------


def _live(*_args, **_kwargs) -> bool:
    """Oracle stub: 'agent IS live on the bound host' — preserves the
    pre-liveness-gate skip behaviour for tests that target the
    no_redispatch / multi-instance branches."""
    return True


def _dead(*_args, **_kwargs) -> bool:
    """Oracle stub: 'agent is NOT live on the bound host' — the stale
    spec-host binding the lead's bm025 repro identified."""
    return False


class TestResolveSingletonSkip:
    def test_skips_singleton_check_when_no_redispatch(self):
        # Arrange — singleton pinned to alpha, current host beta.
        # Without the gate, _singleton_skip_reason returns a skip reason.
        cfg = _cfg(host="alpha")
        # Act — no_redispatch=True means "run HERE, no further routing".
        msg = _resolve_singleton_skip(
            cfg, "beta", no_redispatch=True, liveness_oracle=_live
        )
        # Assert — gate suppresses the dead-end skip.
        assert msg is None

    def test_returns_skip_reason_when_bound_host_has_live_row(self):
        # Arrange — same misalignment, redispatch on, oracle says
        # the agent IS live on alpha → skip-and-defer is the right call.
        cfg = _cfg(host="alpha")
        # Act
        msg = _resolve_singleton_skip(
            cfg, "beta", no_redispatch=False, liveness_oracle=_live
        )
        # Assert — preserve the original skip-and-defer behaviour.
        assert msg and "alpha" in msg and "beta" in msg

    def test_passes_through_none_when_host_matches(self):
        # Arrange
        cfg = _cfg(host="alpha")
        # Act
        msg = _resolve_singleton_skip(
            cfg, "alpha", no_redispatch=False, liveness_oracle=_live
        )
        # Assert
        assert msg is None

    def test_v2_singleton_mismatch_skip_still_propagates_when_live(self):
        # Arrange — v2-style singleton with preferred_host=alpha; gate is
        # a thin wrapper, so the v2 path's reason must survive when the
        # bound host has a live row.
        cfg = _cfg(sched_mode="singleton", pref="alpha")
        # Act
        msg = _resolve_singleton_skip(
            cfg, "beta", no_redispatch=False, liveness_oracle=_live
        )
        # Assert
        assert msg and "alpha" in msg

    def test_no_redispatch_bypasses_v2_singleton_skip_too(self):
        # Arrange — same as above but with the no_redispatch gate.
        cfg = _cfg(sched_mode="singleton", pref="alpha")
        # Act
        msg = _resolve_singleton_skip(cfg, "beta", no_redispatch=True)
        # Assert
        assert msg is None

    # -----------------------------------------------------------------
    # Liveness gate — the lead's bm025 stale-binding repro.
    #
    # When the spec-bound host has no active instances row for the
    # singleton, the binding is stale (the operator already moved the
    # spec to a new host but the prior host still pins the skip). With
    # no live agent over there, deferring is a dead-end — release the
    # binding and fall through to a local start instead.
    # -----------------------------------------------------------------

    def test_falls_through_when_bound_host_has_no_live_row(self):
        # Arrange — singleton pinned to alpha; we're on beta; registry
        # says no live row for the agent anywhere.
        cfg = _cfg(host="alpha")
        # Act
        msg = _resolve_singleton_skip(
            cfg, "beta", no_redispatch=False, liveness_oracle=_dead
        )
        # Assert — stale binding released, fall through to local start.
        assert msg is None

    def test_oracle_invoked_with_agent_name_and_bound_host(self):
        # Arrange — pin contract: oracle sees (name, bound_host).
        cfg = _cfg(host="alpha")
        calls: list[tuple[str, str]] = []

        def _capture(name: str, host: str) -> bool:
            calls.append((name, host))
            return True

        # Act
        _resolve_singleton_skip(
            cfg, "beta", no_redispatch=False, liveness_oracle=_capture
        )
        # Assert
        assert calls == [("a", "alpha")]

    def test_oracle_uses_head_of_chain_for_bound_host(self):
        # Arrange — chain ['a','b','c'], we're on 'z'; head 'a' is bound.
        cfg = _cfg(host=["a", "b", "c"])
        calls: list[tuple[str, str]] = []

        def _capture(name: str, host: str) -> bool:
            calls.append((name, host))
            return True

        # Act
        _resolve_singleton_skip(cfg, "z", no_redispatch=False, liveness_oracle=_capture)
        # Assert
        assert calls == [("a", "a")]

    def test_oracle_not_consulted_when_no_redispatch_true(self):
        # Arrange — no_redispatch shortcut MUST short-circuit the oracle.
        cfg = _cfg(host="alpha")
        called: list[bool] = []

        def _boom(*_a, **_k) -> bool:
            called.append(True)
            return True

        # Act
        _resolve_singleton_skip(cfg, "beta", no_redispatch=True, liveness_oracle=_boom)
        # Assert
        assert called == []

    def test_oracle_not_consulted_when_singleton_check_returns_none(self):
        # Arrange — host matches → skip returns None upfront, oracle
        # never invoked.
        cfg = _cfg(host="alpha")
        called: list[bool] = []

        def _boom(*_a, **_k) -> bool:
            called.append(True)
            return True

        # Act
        _resolve_singleton_skip(
            cfg, "alpha", no_redispatch=False, liveness_oracle=_boom
        )
        # Assert
        assert called == []


# ---------------------------------------------------------------------------
# _bound_host — accessor for the head-of-chain spec-host pin used by
# the liveness gate.
# ---------------------------------------------------------------------------


class TestBoundHost:
    def test_str_host_returned_verbatim(self):
        # Arrange
        cfg = _cfg(host="alpha")
        # Act
        h = _bound_host(cfg)
        # Assert
        assert h == "alpha"

    def test_list_host_returns_head(self):
        # Arrange
        cfg = _cfg(host=["a", "b", "c"])
        # Act
        h = _bound_host(cfg)
        # Assert
        assert h == "a"

    def test_multi_instance_returns_none(self):
        # Arrange
        cfg = _cfg(hosts=["a", "b"])
        # Act
        h = _bound_host(cfg)
        # Assert
        assert h is None

    def test_no_pin_returns_none(self):
        # Arrange
        cfg = _cfg()
        # Act
        h = _bound_host(cfg)
        # Assert
        assert h is None

    def test_v2_singleton_preferred_host_returned(self):
        # Arrange
        cfg = _cfg(sched_mode="singleton", pref="alpha")
        # Act
        h = _bound_host(cfg)
        # Assert
        assert h == "alpha"


# ---------------------------------------------------------------------------
# _registry_active_on — default bound-host liveness oracle (state.db).
# ---------------------------------------------------------------------------


def _reload_state_db_at(path: Path, env_save_restore):
    """Redirect DEFAULT_DB_PATH at ``path`` and reload the state_db
    module so the rebound path takes effect.

    PA-306 §3 no-mocks: uses the project ``env_save_restore`` fixture
    (NOT pytest's ``monkeypatch``) to manage the env var save/restore.
    Returns the reloaded module so the caller can call its
    ``record_instance_start`` etc. against the redirected DB.
    """
    import importlib

    env_save_restore.set("SCITEX_AGENT_CONTAINER_STATE_DB", str(path))
    import scitex_agent_container._state.state_db as state_db_mod

    importlib.reload(state_db_mod)
    return state_db_mod


class TestRegistryActiveOn:
    def test_missing_state_db_treated_as_not_live(self, tmp_path, env_save_restore):
        # Arrange — point state.db at a non-existent path; reload module
        # so DEFAULT_DB_PATH is recomputed.
        import importlib

        state_db_mod = _reload_state_db_at(tmp_path / "nope.db", env_save_restore)
        try:
            # Act
            live = _registry_active_on("ghost", "alpha")
            # Assert
            assert live is False
        finally:
            importlib.reload(state_db_mod)

    def test_recorded_instance_seen_as_live(self, pg_schema: str, tmp_path, env_save_restore):
        # Arrange
        import importlib

        state_db_mod = _reload_state_db_at(tmp_path / "state.db", env_save_restore)
        try:
            state_db_mod.record_instance_start(
                name="clew",
                host="alpha",
                a2a_port=19100,
                bound_port=19100,
                remote=False,
                spawned_by="cli",
            )
            # Act
            live = _registry_active_on("clew", "alpha")
            # Assert
            assert live is True
        finally:
            importlib.reload(state_db_mod)

    def test_instance_on_other_host_not_live_on_target(
        self, pg_schema: str, tmp_path, env_save_restore
    ):
        # Arrange — row recorded on beta, asking about alpha.
        import importlib

        state_db_mod = _reload_state_db_at(tmp_path / "state.db", env_save_restore)
        try:
            state_db_mod.record_instance_start(
                name="clew",
                host="beta",
                a2a_port=19101,
                bound_port=19101,
                remote=False,
                spawned_by="cli",
            )
            # Act
            live = _registry_active_on("clew", "alpha")
            # Assert
            assert live is False
        finally:
            importlib.reload(state_db_mod)

    def test_ended_instance_not_live(self, pg_schema: str, tmp_path, env_save_restore):
        # Arrange — record then end; the row's ended_at != NULL so it
        # must not be reported as live (mirrors what stop --force would
        # do via the new release path).
        import importlib

        state_db_mod = _reload_state_db_at(tmp_path / "state.db", env_save_restore)
        try:
            row_id = state_db_mod.record_instance_start(
                name="clew",
                host="alpha",
                a2a_port=19100,
                bound_port=19100,
                remote=False,
                spawned_by="cli",
            )
            state_db_mod.record_instance_stop(row_id, exit_reason="released")
            # Act
            live = _registry_active_on("clew", "alpha")
            # Assert
            assert live is False
        finally:
            importlib.reload(state_db_mod)


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

    # -- the layout that actually exists on every host ----------------------
    #
    # Until 2026-08-27 this helper matched ONLY <name>/<name>.yaml, so it
    # returned 0 against a registry holding 122 <name>/spec.yaml agents. Every
    # fixture above uses the self-named layout, which is why the suite stayed
    # green over a shape no host produces. These tests pin the real one.

    def test_discovers_the_spec_yaml_layout_every_host_uses(self, tmp_path):
        # Arrange
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "spec.yaml").write_text("x")
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert [n for n, _ in result] == ["alpha"]

    def test_returns_the_spec_yaml_path_not_a_self_named_guess(self, tmp_path):
        # Arrange
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "spec.yaml").write_text("x")
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert result[0][1].endswith("/alpha/spec.yaml")

    def test_prefers_spec_yaml_when_both_layouts_are_present(self, tmp_path):
        # Arrange
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "spec.yaml").write_text("x")
        (tmp_path / "alpha" / "alpha.yaml").write_text("x")
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert result[0][1].endswith("/alpha/spec.yaml")

    def test_still_finds_the_self_named_layout_the_materializers_write(
        self, tmp_path
    ):
        # Arrange -- `sac fleet materialize` and render_contributor_spec still
        # emit <name>/<name>.yaml; the fallback is the alias half of the
        # migration and must not regress while they do.
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "spec.yaml").write_text("x")
        (tmp_path / "beta").mkdir()
        (tmp_path / "beta" / "beta.yaml").write_text("x")
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert [n for n, _ in result] == ["alpha", "beta"]

    def test_skips_the_self_peer_marker(self, tmp_path):
        # Arrange -- agents/self/spec.yaml registers the running listen's own
        # identity and is NOT a launchable agent. It was invisible here only
        # because this helper could not see spec.yaml at all.
        (tmp_path / "self").mkdir()
        (tmp_path / "self" / "spec.yaml").write_text(
            "name: scitex-compute-04\nlisten_url: http://127.0.0.1:7878\n"
        )
        (tmp_path / "alpha").mkdir()
        (tmp_path / "alpha" / "spec.yaml").write_text("x")
        # Act
        result = _iter_agent_yamls(tmp_path)
        # Assert
        assert [n for n, _ in result] == ["alpha"]


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


# ---------------------------------------------------------------------------
# _resolve_dispatch_peer — pure resolver: never raises, never logs, never
# reads files. Covers the documented behaviour table plus case + whitespace
# edge cases (peer keys are taken verbatim from YAML, no folding).
# ---------------------------------------------------------------------------


def _peers_with_spartan() -> dict[str, PeerSpec]:
    """Build a single-peer registry; minimal fixture for the behaviour table."""
    return {
        "spartan-bm152": PeerSpec(name="spartan-bm152", ssh="spartan-bm152"),
    }


class TestResolveDispatchPeer:
    def test_target_none_returns_none_for_local_execution(self):
        # Arrange
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer(None, "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_target_matches_current_host_returns_none_when_peer_exists(self):
        # Arrange — peer registry knows about the current host too.
        peers = {
            "ywata-note-win": PeerSpec(name="ywata-note-win", ssh="ywata-note-win"),
        }
        # Act
        out = _resolve_dispatch_peer("ywata-note-win", "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_target_matches_current_host_returns_none_when_peer_missing(self):
        # Arrange — peer registry does NOT list the current host.
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer("ywata-note-win", "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_unknown_target_returns_none_for_caller_to_decide(self):
        # Arrange
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer("unknown-host", "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_known_peer_distinct_from_current_returns_peer_name(self):
        # Arrange
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer("spartan-bm152", "ywata-note-win", peers)
        # Assert
        assert out == "spartan-bm152"

    def test_target_host_lookup_is_case_sensitive(self):
        # Arrange — uppercase target with lowercase peer key must NOT match.
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer("SPARTAN-BM152", "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_target_host_whitespace_padding_is_not_stripped(self):
        # Arrange — literal string compare; config drift surfaces as a miss.
        peers = _peers_with_spartan()
        # Act
        out = _resolve_dispatch_peer(" spartan-bm152 ", "ywata-note-win", peers)
        # Assert
        assert out is None

    def test_alias_of_self_that_is_also_a_peer_stays_local(self):
        # Arrange — the current machine is ALSO registered as a peer (so
        # remote hosts can ssh to it); an alias spelling must resolve LOCAL,
        # never ssh-to-self, even though it is a peer key.
        peers = {
            "ywata-note-win": PeerSpec(name="ywata-note-win", ssh="localhost"),
        }
        # Act
        out = _resolve_dispatch_peer(
            "ywata-note-win",
            "some-raw-hostname",
            peers,
            local_names={"ywata-note-win", "some-raw-hostname"},
        )
        # Assert
        assert out is None


# ---------------------------------------------------------------------------
# classify_dispatch_host — the operator's concrete-hostname resolution layer:
# concrete host -> local | remote:<peer> | unknown. Pure; never reads files.
# ---------------------------------------------------------------------------


class TestClassifyDispatchHost:
    def test_absent_host_classifies_local(self):
        # Arrange — host: local / absent normalizes to None upstream.
        peers = _peers_with_spartan()
        # Act
        kind, peer = classify_dispatch_host(None, "ywata-note-win", peers)
        # Assert
        assert (kind, peer) == ("local", None)

    def test_canonical_name_of_current_host_classifies_local(self):
        # Arrange — host: <this-canonical> equals current_host.
        peers = _peers_with_spartan()
        # Act
        kind, peer = classify_dispatch_host(
            "ywata-note-win", "ywata-note-win", peers
        )
        # Assert
        assert (kind, peer) == ("local", None)

    def test_alias_of_current_host_classifies_local(self):
        # Arrange — target differs from current_host but is a known local
        # alias (host_config canonical/aliases denote THIS machine).
        peers = _peers_with_spartan()
        # Act
        kind, peer = classify_dispatch_host(
            "ywata-note-win",
            "raw-short-name",
            peers,
            local_names={"raw-short-name", "ywata-note-win"},
        )
        # Assert
        assert (kind, peer) == ("local", None)

    def test_local_wins_over_peer_table_no_ssh_to_self(self):
        # Arrange — the machine is registered as a peer too; local must win.
        peers = {"ywata-note-win": PeerSpec(name="ywata-note-win", ssh="localhost")}
        # Act
        kind, peer = classify_dispatch_host(
            "ywata-note-win",
            "ywata-note-win",
            peers,
            local_names={"ywata-note-win"},
        )
        # Assert
        assert (kind, peer) == ("local", None)

    def test_known_peer_classifies_remote(self):
        # Arrange
        peers = _peers_with_spartan()
        # Act
        kind, peer = classify_dispatch_host(
            "spartan-bm152", "ywata-note-win", peers
        )
        # Assert
        assert (kind, peer) == ("remote", "spartan-bm152")

    def test_unknown_host_classifies_unknown(self):
        # Arrange — neither local nor a peer key.
        peers = _peers_with_spartan()
        # Act
        kind, peer = classify_dispatch_host("typo-host", "ywata-note-win", peers)
        # Assert
        assert (kind, peer) == ("unknown", None)

    def test_glob_peer_key_classifies_remote(self):
        # Arrange — PeersMap resolves spartan-* patterns on lookup.
        from scitex_agent_container._state.host_config import PeersMap

        peers = PeersMap()
        peers["spartan-*"] = PeerSpec(name="spartan-*", ssh="")
        # Act
        kind, peer = classify_dispatch_host(
            "spartan-bm999", "ywata-note-win", peers
        )
        # Assert
        assert (kind, peer) == ("remote", "spartan-bm999")


# ---------------------------------------------------------------------------
# _local_host_names — impure adapter unioning the two hostname authorities.
# No-mocks: a real config.yaml fixture via $SCITEX_AGENT_CONTAINER_CONFIG.
# ---------------------------------------------------------------------------


class TestLocalHostNames:
    def test_includes_current_host_argument(self):
        # Arrange
        current = "passed-in-host"
        # Act
        names = _local_host_names(current)
        # Assert
        assert "passed-in-host" in names

    def test_includes_config_canonical_and_alias(
        self, tmp_path: Path, env_save_restore
    ):
        # Arrange — real config.yaml: canonical name + an alias for the short
        # hostname this test process actually reports.
        import socket

        short = socket.gethostname().split(".")[0]
        cfg = tmp_path / "config.yaml"
        cfg.write_text(
            "host:\n"
            "  canonical: box-canonical\n"
            "  aliases:\n"
            f"    {short}: box-alias\n"
        )
        env_save_restore.set("SCITEX_AGENT_CONTAINER_CONFIG", str(cfg))
        # Act
        names = _local_host_names()
        # Assert — $SAC_HOST unset, so canonical_host() returns host.canonical.
        assert "box-canonical" in names

    def test_never_raises_without_config(self, tmp_path: Path, env_save_restore):
        # Arrange — point at a nonexistent config; must degrade, not raise.
        env_save_restore.set(
            "SCITEX_AGENT_CONTAINER_CONFIG", str(tmp_path / "missing.yaml")
        )
        # Act
        names = _local_host_names("host-x")
        # Assert — still returns the short hostname + the passed current_host.
        assert "host-x" in names and names
