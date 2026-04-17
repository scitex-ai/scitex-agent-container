"""Tests for sac's agent discovery, hostname substitution, and scheduling.

sac-only concerns: discovery searches ``~/.scitex/agent-container/agents/``
plus ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (plugin port). Any orochi- or
fleet-specific layering is the consumer's responsibility.
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    """Redirect ~ to tmp_path and reset env overrides for determinism."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
    return tmp_path


def _write_agent_yaml(
    base: Path,
    name: str,
    *,
    metadata_name: str | None = None,
    scheduling: dict | None = None,
    extra_labels: dict | None = None,
) -> Path:
    data: dict = {
        "apiVersion": "scitex-agent-container/v2",
        "kind": "Agent",
        "metadata": {
            "name": metadata_name if metadata_name is not None else name,
            "labels": {"role": "head", **(extra_labels or {})},
        },
        "spec": {
            "runtime": "claude-code",
            "model": "sonnet",
        },
    }
    if scheduling is not None:
        data["spec"]["scheduling"] = scheduling
    dest = base / name / f"{name}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(data))
    return dest


# ---------------------------------------------------------------------------
# 1. Agent discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_discovery_finds_primary_agents(self, fake_home):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        _write_agent_yaml(primary, "head")

        hits = _discover_all_agents()
        assert len(hits) == 1
        assert hits[0].endswith("agent-container/agents/head/head.yaml")

    def test_discovery_primary_wins_over_env_var(self, fake_home, monkeypatch):
        """When the same agent name exists in both primary and env-var dir,
        primary wins (earlier in the search order)."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        extra = fake_home / "ext" / "agents"
        _write_agent_yaml(primary, "head", extra_labels={"tier": "primary"})
        _write_agent_yaml(extra, "head", extra_labels={"tier": "extra"})
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))

        hits = _discover_all_agents()
        assert len(hits) == 1
        assert "agent-container/agents/head/head.yaml" in hits[0]

    def test_discovery_merges_across_search_dirs(self, fake_home, monkeypatch):
        """Different names across primary and env-var dirs all surface."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        extra = fake_home / "ext" / "agents"
        _write_agent_yaml(primary, "head")
        _write_agent_yaml(extra, "caduceus")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))

        hits = _discover_all_agents()
        assert len(hits) == 2
        names = {Path(h).parent.name for h in hits}
        assert names == {"head", "caduceus"}

    def test_discovery_skips_hidden_and_reserved(self, fake_home, monkeypatch):
        """Dirs starting with . / _ and reserved names are ignored."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        extra = fake_home / "ext" / "agents"
        for skip_name in (".hidden", "_private", "legacy-agents", "GITIGNORED"):
            _write_agent_yaml(primary, skip_name)
        _write_agent_yaml(primary, "primary-real")
        for skip_name in (".hidden", "_private"):
            _write_agent_yaml(extra, skip_name)
        _write_agent_yaml(extra, "extra-real")
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))

        hits = _discover_all_agents()
        names = {Path(h).parent.name for h in hits}
        assert names == {"primary-real", "extra-real"}

    def test_discovery_env_var_colon_separated(self, fake_home, monkeypatch):
        """Multiple paths in SCITEX_AGENT_CONTAINER_YAML_DIRS are searched in
        order; earlier wins on name collision."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        d1 = fake_home / "d1"
        d2 = fake_home / "d2"
        _write_agent_yaml(d1, "head", extra_labels={"src": "d1"})
        _write_agent_yaml(d2, "head", extra_labels={"src": "d2"})
        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{d1}:{d2}")

        hits = _discover_all_agents()
        assert len(hits) == 1
        assert str(d1) in hits[0]


# ---------------------------------------------------------------------------
# 2. Hostname substitution
# ---------------------------------------------------------------------------


class TestHostnameSubstitution:
    def test_substitution_happy_path(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "mba")
        obj = {
            "name": "head",
            "labels": {"machine": "${HOSTNAME}"},
            "commands": ["echo ${HOSTNAME}"],
        }
        out = substitute_hostnames(obj)
        assert out["labels"]["machine"] == "mba"
        assert out["commands"] == ["echo mba"]
        assert out["name"] == "head"  # unchanged

    def test_substitution_preserves_other_placeholders(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "nas")
        # Non-HOSTNAME placeholders pass through untouched for downstream
        # (MCP interpolation etc.) to handle.
        obj = {"token": "${SOME_OTHER_TOKEN}", "host": "${HOSTNAME}"}
        out = substitute_hostnames(obj)
        assert out["host"] == "nas"
        assert out["token"] == "${SOME_OTHER_TOKEN}"

    def test_substitution_deeply_nested(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "spartan")
        obj = {
            "a": [{"b": {"c": ["${HOSTNAME}-suffix"]}}],
        }
        out = substitute_hostnames(obj)
        assert out["a"][0]["b"]["c"] == ["spartan-suffix"]

    def test_env_var_wins_over_short_hostname(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "override")
        monkeypatch.setattr("socket.gethostname", lambda: "ignored.example.com")
        assert resolve_hostname() == "override"

    def test_short_hostname_fallback(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "mybox.local")
        assert resolve_hostname() == "mybox"

    def test_missing_var_raises_when_all_empty(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "")
        with pytest.raises(RuntimeError):
            resolve_hostname()

    def test_alias_map_translates_short_hostname(self, monkeypatch, tmp_path):
        """~/.scitex/agent-container/config.yaml::hostname_aliases maps
        raw short-hostname -> canonical label."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "Yusukes-MacBook-Air")
        config_path = tmp_path / ".scitex" / "agent-container" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "spec:\n"
            "  hostname_aliases:\n"
            "    Yusukes-MacBook-Air: mba\n"
            "    DXP480TPLUS-994: nas\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_path)
        assert host_mod.resolve_hostname() == "mba"

    def test_env_var_beats_alias_map(self, monkeypatch, tmp_path):
        """$SCITEX_AGENT_CONTAINER_HOSTNAME overrides the alias map."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "manual-override")
        monkeypatch.setattr("socket.gethostname", lambda: "Yusukes-MacBook-Air")
        config_path = tmp_path / ".scitex" / "agent-container" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_path)
        assert host_mod.resolve_hostname() == "manual-override"

    def test_unmapped_host_falls_through_to_identity(self, monkeypatch, tmp_path):
        """hostname -s with no alias entry returns unchanged."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "ywata-note-win")
        config_path = tmp_path / ".scitex" / "agent-container" / "config.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_path)
        assert host_mod.resolve_hostname() == "ywata-note-win"

    def test_missing_config_file_is_not_an_error(self, monkeypatch, tmp_path):
        """Hostname resolves without a config file — identity fallback only."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_AGENT_CONTAINER_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "bare-host")
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", tmp_path / "no-such.yaml")
        assert host_mod.resolve_hostname() == "bare-host"


# ---------------------------------------------------------------------------
# 3. Effective-id composition
# ---------------------------------------------------------------------------


class TestEffectiveId:
    def test_per_host_appends_suffix(self):
        from scitex_agent_container.config import SchedulingSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        assert (
            compose_effective_name(
                "head", SchedulingSpec(mode="per-host"), "ywata-note-win"
            )
            == "head-ywata-note-win"
        )

    def test_per_host_idempotent_when_name_already_suffixed(self):
        from scitex_agent_container.config import SchedulingSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        assert (
            compose_effective_name(
                "head-ywata-note-win",
                SchedulingSpec(mode="per-host"),
                "ywata-note-win",
            )
            == "head-ywata-note-win"
        )

    def test_singleton_keeps_bare_name(self):
        from scitex_agent_container.config import SchedulingSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        sched = SchedulingSpec(mode="singleton", preferred_host="ywata-note-win")
        assert (
            compose_effective_name("fleet-lead", sched, "ywata-note-win")
            == "fleet-lead"
        )
        assert compose_effective_name("fleet-lead", sched, "mba") == "fleet-lead"

    def test_load_v2_yields_host_suffixed_id(self, tmp_path, monkeypatch):
        """End-to-end: shared YAML on a specific host -> host-suffixed id + workdir."""
        from scitex_agent_container.config import load_config

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        monkeypatch.setenv("HOME", str(tmp_path))

        head_yaml = tmp_path / "head.yaml"
        head_yaml.write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v2
                kind: Agent
                metadata:
                  name: head
                  labels:
                    role: head
                    machine: ${HOSTNAME}
                spec:
                  runtime: claude-code
                  scheduling:
                    mode: per-host
                """
            )
        )
        cfg = load_config(str(head_yaml))
        assert cfg.name == "head-ywata-note-win"
        assert cfg.labels["machine"] == "ywata-note-win"
        assert cfg.scheduling.mode == "per-host"
        assert cfg.workdir == "~/.scitex/agent-container/workspaces/head-ywata-note-win"

    def test_load_v2_singleton_keeps_bare_id(self, tmp_path, monkeypatch):
        from scitex_agent_container.config import load_config

        monkeypatch.setenv("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")

        yaml_path = tmp_path / "fleet-lead.yaml"
        yaml_path.write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v2
                kind: Agent
                metadata:
                  name: fleet-lead
                  labels:
                    role: lead
                spec:
                  runtime: claude-code
                  scheduling:
                    mode: singleton
                    preferred-host: ywata-note-win
                    fallback-hosts: [mba, nas, spartan]
                """
            )
        )
        cfg = load_config(str(yaml_path))
        assert cfg.name == "fleet-lead"
        assert cfg.scheduling.mode == "singleton"
        assert cfg.scheduling.preferred_host == "ywata-note-win"
        assert cfg.scheduling.fallback_hosts == ["mba", "nas", "spartan"]

    def test_load_v2_without_scheduling_preserves_name(self, tmp_path):
        """Legacy v2 YAML without spec.scheduling keeps metadata.name unchanged."""
        from scitex_agent_container.config import load_config

        yaml_path = tmp_path / "legacy.yaml"
        yaml_path.write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v2
                kind: Agent
                metadata:
                  name: head-test
                  labels:
                    role: head
                spec:
                  runtime: claude-code
                """
            )
        )
        cfg = load_config(str(yaml_path))
        assert cfg.name == "head-test"


# ---------------------------------------------------------------------------
# 4. Singleton enforcement
# ---------------------------------------------------------------------------


class TestSingletonEnforcement:
    def test_singleton_match_returns_no_skip(self):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _singleton_skip_reason,
        )
        from scitex_agent_container.config import AgentConfig, SchedulingSpec

        cfg = AgentConfig(
            name="fleet-lead",
            scheduling=SchedulingSpec(
                mode="singleton",
                preferred_host="ywata-note-win",
                fallback_hosts=["mba", "nas"],
            ),
        )
        assert _singleton_skip_reason(cfg, "ywata-note-win") is None

    def test_singleton_mismatch_returns_skip_reason(self):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _singleton_skip_reason,
        )
        from scitex_agent_container.config import AgentConfig, SchedulingSpec

        cfg = AgentConfig(
            name="fleet-lead",
            scheduling=SchedulingSpec(
                mode="singleton",
                preferred_host="ywata-note-win",
                fallback_hosts=["mba", "nas"],
            ),
        )
        reason = _singleton_skip_reason(cfg, "mba")
        assert reason is not None
        assert "ywata-note-win" in reason
        assert "mba" in reason
        assert "fallback-hosts" in reason

    def test_per_host_never_skips(self):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _singleton_skip_reason,
        )
        from scitex_agent_container.config import AgentConfig, SchedulingSpec

        cfg = AgentConfig(name="head-mba", scheduling=SchedulingSpec(mode="per-host"))
        assert _singleton_skip_reason(cfg, "mba") is None
        assert _singleton_skip_reason(cfg, "ywata-note-win") is None

    def test_singleton_without_preferred_host_never_skips(self):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _singleton_skip_reason,
        )
        from scitex_agent_container.config import AgentConfig, SchedulingSpec

        cfg = AgentConfig(
            name="fleet-lead",
            scheduling=SchedulingSpec(mode="singleton", preferred_host=""),
        )
        assert _singleton_skip_reason(cfg, "any-host") is None


# ---------------------------------------------------------------------------
# 5. Scheduling parser error handling
# ---------------------------------------------------------------------------


class TestSchedulingParser:
    def test_invalid_mode_raises(self):
        from scitex_agent_container.config._parsers import parse_scheduling

        with pytest.raises(ValueError):
            parse_scheduling({"scheduling": {"mode": "bogus"}})

    def test_non_mapping_raises(self):
        from scitex_agent_container.config._parsers import parse_scheduling

        with pytest.raises(ValueError):
            parse_scheduling({"scheduling": "not-a-dict"})

    def test_absent_returns_not_explicit(self):
        from scitex_agent_container.config._parsers import parse_scheduling

        sched, explicit = parse_scheduling({})
        assert explicit is False
        assert sched.mode == "per-host"
