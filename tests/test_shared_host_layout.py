"""Tests for the shared-host fleet layout (feat/orochi-shared-host-layout).

Covers:
  * Agent discovery (host override > shared).
  * ``${HOSTNAME}`` / ``${SCITEX_OROCHI_HOSTNAME}`` substitution.
  * Effective-id composition for ``per-host`` vs ``singleton`` scheduling.
  * Singleton launch-skip decision on non-preferred hosts.
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
    """Redirect ~ to tmp_path and unset env overrides."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SCITEX_AGENT_CONTAINER_YAML_DIRS", raising=False)
    # Default the canonical hostname to a stable value for determinism.
    monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "ywata-note-win")
    return tmp_path


def _write_agent_yaml(
    base: Path,
    name: str,
    *,
    metadata_name: str | None = None,
    scheduling: dict | None = None,
    extra_labels: dict | None = None,
) -> Path:
    """Write ``<base>/<name>/<name>.yaml`` and return its path."""
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
    def test_discovery_finds_shared_agents(self, fake_home):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        shared = fake_home / ".scitex" / "orochi" / "shared" / "agents"
        _write_agent_yaml(shared, "head")

        hits = _discover_all_agents()
        assert len(hits) == 1
        assert hits[0].endswith("shared/agents/head/head.yaml")

    def test_discovery_host_override_wins_over_shared(self, fake_home):
        """Host-specific dir wins over shared for the same agent name."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        shared = fake_home / ".scitex" / "orochi" / "shared" / "agents"
        host = fake_home / ".scitex" / "orochi" / "ywata-note-win" / "agents"
        _write_agent_yaml(shared, "head", extra_labels={"tier": "shared"})
        _write_agent_yaml(host, "head", extra_labels={"tier": "host"})

        hits = _discover_all_agents()
        assert len(hits) == 1
        # Host override wins.
        assert "ywata-note-win/agents/head/head.yaml" in hits[0]

    def test_discovery_merges_when_names_differ(self, fake_home):
        """Different names across host + shared all surface."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        shared = fake_home / ".scitex" / "orochi" / "shared" / "agents"
        host_dir = fake_home / ".scitex" / "orochi" / "ywata-note-win" / "agents"
        _write_agent_yaml(shared, "head")
        _write_agent_yaml(host_dir, "caduceus")

        hits = _discover_all_agents()
        assert len(hits) == 2
        names = {Path(h).parent.name for h in hits}
        assert names == {"head", "caduceus"}

    def test_discovery_skips_hidden_and_reserved(self, fake_home):
        """Dirs starting with . / _ and reserved names are ignored."""
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _discover_all_agents,
        )

        root = fake_home / ".scitex" / "orochi"
        shared = root / "shared" / "agents"
        for skip_name in (".hidden", "_private", "legacy-agents", "GITIGNORED"):
            _write_agent_yaml(shared, skip_name)
        _write_agent_yaml(shared, "shared-real")
        host_dir = root / "ywata-note-win" / "agents"
        for skip_name in (".hidden", "_private"):
            _write_agent_yaml(host_dir, skip_name)
        _write_agent_yaml(host_dir, "host-real")

        hits = _discover_all_agents()
        names = {Path(h).parent.name for h in hits}
        assert names == {"shared-real", "host-real"}


# ---------------------------------------------------------------------------
# 2. Hostname substitution
# ---------------------------------------------------------------------------


class TestHostnameSubstitution:
    def test_substitution_happy_path(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "mba")
        obj = {
            "name": "head",
            "labels": {"machine": "${HOSTNAME}"},
            "commands": ["echo ${SCITEX_OROCHI_HOSTNAME}"],
        }
        out = substitute_hostnames(obj)
        assert out["labels"]["machine"] == "mba"
        assert out["commands"] == ["echo mba"]
        assert out["name"] == "head"  # unchanged

    def test_substitution_preserves_other_placeholders(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "nas")
        obj = {"token": "${SCITEX_OROCHI_TOKEN}", "host": "${HOSTNAME}"}
        out = substitute_hostnames(obj)
        assert out["host"] == "nas"
        # Other placeholders are left for downstream (e.g. mcp) interpolation.
        assert out["token"] == "${SCITEX_OROCHI_TOKEN}"

    def test_substitution_deeply_nested(self, monkeypatch):
        from scitex_agent_container.config._host import substitute_hostnames

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "spartan")
        obj = {
            "a": [{"b": {"c": ["${HOSTNAME}-suffix"]}}],
        }
        out = substitute_hostnames(obj)
        assert out["a"][0]["b"]["c"] == ["spartan-suffix"]

    def test_env_var_wins_over_short_hostname(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "override")
        monkeypatch.setattr("socket.gethostname", lambda: "ignored.example.com")
        assert resolve_hostname() == "override"

    def test_short_hostname_fallback(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.delenv("SCITEX_OROCHI_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "mybox.local")
        assert resolve_hostname() == "mybox"

    def test_missing_var_raises_when_all_empty(self, monkeypatch):
        from scitex_agent_container.config._host import resolve_hostname

        monkeypatch.delenv("SCITEX_OROCHI_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "")
        with pytest.raises(RuntimeError):
            resolve_hostname()

    def test_alias_map_translates_short_hostname(self, monkeypatch, tmp_path):
        """shared/config.yaml::hostname_aliases maps raw -> fleet label."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_OROCHI_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "Yusukes-MacBook-Air")
        config_dir = tmp_path / ".scitex" / "orochi" / "shared"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "spec:\n"
            "  hostname_aliases:\n"
            "    Yusukes-MacBook-Air: mba\n"
            "    DXP480TPLUS-994: nas\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_dir / "config.yaml")
        assert host_mod.resolve_hostname() == "mba"

    def test_env_var_beats_alias_map(self, monkeypatch, tmp_path):
        """$SCITEX_OROCHI_HOSTNAME overrides the alias map."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "manual-override")
        monkeypatch.setattr("socket.gethostname", lambda: "Yusukes-MacBook-Air")
        config_dir = tmp_path / ".scitex" / "orochi" / "shared"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_dir / "config.yaml")
        assert host_mod.resolve_hostname() == "manual-override"

    def test_unmapped_host_falls_through_to_identity(self, monkeypatch, tmp_path):
        """hostname -s with no alias entry returns unchanged."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_OROCHI_HOSTNAME", raising=False)
        monkeypatch.setattr("socket.gethostname", lambda: "ywata-note-win")
        config_dir = tmp_path / ".scitex" / "orochi" / "shared"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text(
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n"
        )
        monkeypatch.setattr(host_mod, "_CONFIG_PATH", config_dir / "config.yaml")
        assert host_mod.resolve_hostname() == "ywata-note-win"

    def test_missing_config_file_is_not_an_error(self, monkeypatch, tmp_path):
        """Hostname resolves without a config file — identity fallback only."""
        import scitex_agent_container.config._host as host_mod

        monkeypatch.delenv("SCITEX_OROCHI_HOSTNAME", raising=False)
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
        """Legacy flat YAMLs with ``metadata.name: head-ywata-note-win`` pass through."""
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
        # Even on a non-preferred host the id is still bare — launch-skip is
        # a separate decision (see TestSingletonEnforcement).
        assert compose_effective_name("fleet-lead", sched, "mba") == "fleet-lead"

    def test_load_v2_head_yaml_on_wsl_yields_host_suffixed_id(
        self, tmp_path, monkeypatch
    ):
        """End-to-end: shared/agents/head/head.yaml on WSL -> head-ywata-note-win."""
        from scitex_agent_container.config import load_config

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "ywata-note-win")
        # Isolate HOME so the legacy-workdir fallback probe doesn't hit the
        # dev machine's real ~/.scitex/orochi/workspaces/ and return the
        # pre-runtime/ path.
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
        assert cfg.workdir == "~/.scitex/orochi/runtime/workspaces/head-ywata-note-win"

    def test_load_v2_fleet_lead_on_wsl_keeps_bare_id(self, tmp_path, monkeypatch):
        """Singleton fleet-lead on preferred host keeps bare ``fleet-lead`` id."""
        from scitex_agent_container.config import load_config

        monkeypatch.setenv("SCITEX_OROCHI_HOSTNAME", "ywata-note-win")

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

    def test_load_v2_without_scheduling_preserves_legacy_behavior(self, tmp_path):
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
        assert cfg.name == "head-test"  # not mangled


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
        # Fallback hosts are surfaced for operator context.
        assert "fallback-hosts" in reason

    def test_per_host_never_skips(self):
        from scitex_agent_container.cli_pkg.lifecycle_cmds import (
            _singleton_skip_reason,
        )
        from scitex_agent_container.config import AgentConfig, SchedulingSpec

        cfg = AgentConfig(name="head-mba", scheduling=SchedulingSpec(mode="per-host"))
        # No matter the host, per-host agents always launch.
        assert _singleton_skip_reason(cfg, "mba") is None
        assert _singleton_skip_reason(cfg, "ywata-note-win") is None

    def test_singleton_without_preferred_host_never_skips(self):
        """Singleton without a preferred-host is a no-op guard — launches anywhere."""
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
