"""Tests for sac's agent discovery, hostname substitution, and scheduling.

sac-only concerns: discovery searches ``~/.scitex/agent-container/agents/``
plus ``$SCITEX_AGENT_CONTAINER_YAML_DIRS`` (plugin port). Any orochi- or
fleet-specific layering is the consumer's responsibility.

No-mocks: tests use the real ``env_save_restore`` fixture to redirect env
vars (HOME, SCITEX_DIR, SCITEX_AGENT_CONTAINER_HOSTNAME, *_YAML_DIRS),
real public seams (``substitute_hostnames(hostname=...)``,
``resolve_hostname(gethostname=...)``), and real ``tmp_path`` filesystem
state. No ``monkeypatch``, no ``unittest.mock``.
"""

from __future__ import annotations

import os
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_home(env_save_restore, tmp_path):
    """Redirect HOME + SCITEX_DIR to ``tmp_path`` and isolate cwd.

    Real seams only:
      * ``env_save_restore.set("HOME", ...)`` — production reads via
        ``Path.home()`` / ``os.path.expanduser("~")``.
      * ``env_save_restore.set("SCITEX_DIR", ...)`` — production reads
        via ``scitex_config._ecosystem.local_state.path`` cascade.
      * ``os.chdir(tmp_path)`` with restore — keeps the project-local
        agent discovery (``_project_local_dirs`` walks upward from cwd)
        from picking up the in-repo sdk-test agent.
    """
    # Arrange real env redirects
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.set("SCITEX_DIR", str(tmp_path / ".scitex"))
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_YAML_DIRS")
    env_save_restore.delete("SAC_YAML_DIRS")
    env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield tmp_path
    finally:
        os.chdir(saved_cwd)


@pytest.fixture
def host_env(env_save_restore):
    """Save/restore hostname env vars for resolve_hostname tests."""
    # Clear both forms so the test starts from a known state.
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HOSTNAME")
    env_save_restore.delete("SAC_HOSTNAME")
    return env_save_restore


@pytest.fixture
def host_env_with_scitex_dir(env_save_restore, tmp_path):
    """host_env + redirect SCITEX_DIR so _config_path lands in tmp_path.

    Also chdirs into ``tmp_path`` so the project-scope local-state
    lookup in ``_local_state.path`` cannot find a real repo scope.
    """
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_HOSTNAME")
    env_save_restore.delete("SAC_HOSTNAME")
    env_save_restore.set("SCITEX_DIR", str(tmp_path / ".scitex"))
    env_save_restore.set("HOME", str(tmp_path))
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        yield env_save_restore
    finally:
        os.chdir(saved_cwd)


def _write_agent_yaml(
    base: Path,
    name: str,
    *,
    host: str | list[str] | None = None,
    hosts: str | list[str] | None = None,
    extra_labels: dict | None = None,
) -> Path:
    """Write a v3 YAML at <base>/<name>/<name>.yaml. Dir-as-SSoT."""
    spec: dict = {"runtime": "apptainer", "model": "sonnet"}
    if host is not None:
        spec["host"] = host
    if hosts is not None:
        spec["hosts"] = hosts
    data: dict = {
        "apiVersion": "scitex-agent-container/v3",
        "kind": "Agent",
        "metadata": {"labels": {"role": "head", **(extra_labels or {})}},
        "spec": spec,
    }
    dest = base / name / f"{name}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(yaml.safe_dump(data))
    return dest


def _write_scitex_config(scitex_dir: Path, aliases_yaml: str) -> Path:
    """Write ``$SCITEX_DIR/agent-container/config.yaml`` with given body."""
    cfg_path = scitex_dir / "agent-container" / "config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(aliases_yaml)
    return cfg_path


# ---------------------------------------------------------------------------
# 1. Agent discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_primary_agent_dir_is_discovered_in_home(self, fake_home):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        _write_agent_yaml(primary, "head")
        # Act
        hits = _discover_all_agents()
        # Assert
        assert hits[0].endswith("agent-container/agents/head/head.yaml")

    def test_primary_agent_dir_yields_exactly_one_hit(self, fake_home):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        _write_agent_yaml(primary, "head")
        # Act
        hits = _discover_all_agents()
        # Assert
        assert len(hits) == 1

    def test_primary_beats_env_dir_on_name_collision(self, fake_home, env_save_restore):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        extra = fake_home / "ext" / "agents"
        _write_agent_yaml(primary, "head", extra_labels={"tier": "primary"})
        _write_agent_yaml(extra, "head", extra_labels={"tier": "extra"})
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))
        # Act
        hits = _discover_all_agents()
        # Assert
        assert "agent-container/agents/head/head.yaml" in hits[0]

    def test_discovery_merges_distinct_names_across_dirs(
        self, fake_home, env_save_restore
    ):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _discover_all_agents,
        )

        primary = fake_home / ".scitex" / "agent-container" / "agents"
        extra = fake_home / "ext" / "agents"
        _write_agent_yaml(primary, "head")
        _write_agent_yaml(extra, "caduceus")
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))
        # Act
        names = {Path(h).parent.name for h in _discover_all_agents()}
        # Assert
        assert names == {"head", "caduceus"}

    def test_hidden_and_reserved_subdirs_are_skipped(self, fake_home, env_save_restore):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
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
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", str(extra))
        # Act
        names = {Path(h).parent.name for h in _discover_all_agents()}
        # Assert
        assert names == {"primary-real", "extra-real"}

    def test_env_var_colon_separated_first_path_wins(self, fake_home, env_save_restore):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _discover_all_agents,
        )

        d1 = fake_home / "d1"
        d2 = fake_home / "d2"
        _write_agent_yaml(d1, "head", extra_labels={"src": "d1"})
        _write_agent_yaml(d2, "head", extra_labels={"src": "d2"})
        env_save_restore.set("SCITEX_AGENT_CONTAINER_YAML_DIRS", f"{d1}:{d2}")
        # Act
        hits = _discover_all_agents()
        # Assert
        assert str(d1) in hits[0]


# ---------------------------------------------------------------------------
# 2. Hostname substitution
# ---------------------------------------------------------------------------


class TestHostnameSubstitution:
    def test_substitute_replaces_hostname_in_label_value(self):
        # Arrange
        from scitex_agent_container.config._host import substitute_hostnames

        obj = {"labels": {"machine": "${HOSTNAME}"}}
        # Act
        out = substitute_hostnames(obj, hostname="mba")
        # Assert
        assert out["labels"]["machine"] == "mba"

    def test_substitute_replaces_hostname_inside_list_string(self):
        # Arrange
        from scitex_agent_container.config._host import substitute_hostnames

        obj = {"commands": ["echo ${HOSTNAME}"]}
        # Act
        out = substitute_hostnames(obj, hostname="mba")
        # Assert
        assert out["commands"] == ["echo mba"]

    def test_substitute_leaves_non_placeholder_strings_unchanged(self):
        # Arrange
        from scitex_agent_container.config._host import substitute_hostnames

        obj = {"name": "head"}
        # Act
        out = substitute_hostnames(obj, hostname="mba")
        # Assert
        assert out["name"] == "head"

    def test_substitute_preserves_other_placeholders(self):
        # Arrange
        from scitex_agent_container.config._host import substitute_hostnames

        obj = {"token": "${SOME_OTHER_TOKEN}"}
        # Act
        out = substitute_hostnames(obj, hostname="nas")
        # Assert
        assert out["token"] == "${SOME_OTHER_TOKEN}"

    def test_substitute_walks_deeply_nested_structures(self):
        # Arrange
        from scitex_agent_container.config._host import substitute_hostnames

        obj = {"a": [{"b": {"c": ["${HOSTNAME}-suffix"]}}]}
        # Act
        out = substitute_hostnames(obj, hostname="spartan")
        # Assert
        assert out["a"][0]["b"]["c"] == ["spartan-suffix"]

    def test_env_var_override_wins_over_socket_gethostname(self, host_env):
        # Arrange
        from scitex_agent_container.config._host import resolve_hostname

        host_env.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "override")
        # Act
        result = resolve_hostname(gethostname=lambda: "ignored.example.com")
        # Assert
        assert result == "override"

    def test_short_hostname_is_used_when_env_var_unset(self, host_env):
        # Arrange: host_env fixture already deletes the env var.
        from scitex_agent_container.config._host import resolve_hostname

        # Act
        result = resolve_hostname(gethostname=lambda: "mybox.local")
        # Assert
        assert result == "mybox"

    def test_empty_env_and_empty_socket_raises_runtime_error(self, host_env):
        # Arrange
        from scitex_agent_container.config._host import resolve_hostname

        empty_gethostname = lambda: ""
        # Act
        ctx = pytest.raises(RuntimeError)
        # Assert
        with ctx:
            resolve_hostname(gethostname=empty_gethostname)

    def test_alias_map_translates_short_hostname_to_canonical(
        self, host_env_with_scitex_dir, tmp_path
    ):
        # Arrange
        from scitex_agent_container.config._host import resolve_hostname

        _write_scitex_config(
            tmp_path / ".scitex",
            "spec:\n"
            "  hostname_aliases:\n"
            "    Yusukes-MacBook-Air: mba\n"
            "    DXP480TPLUS-994: nas\n",
        )
        # Act
        result = resolve_hostname(gethostname=lambda: "Yusukes-MacBook-Air")
        # Assert
        assert result == "mba"

    def test_env_var_beats_alias_map(self, host_env_with_scitex_dir, tmp_path):
        # Arrange
        from scitex_agent_container.config._host import resolve_hostname

        host_env_with_scitex_dir.set(
            "SCITEX_AGENT_CONTAINER_HOSTNAME", "manual-override"
        )
        _write_scitex_config(
            tmp_path / ".scitex",
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n",
        )
        # Act
        result = resolve_hostname(gethostname=lambda: "Yusukes-MacBook-Air")
        # Assert
        assert result == "manual-override"

    def test_unmapped_short_hostname_passes_through_unchanged(
        self, host_env_with_scitex_dir, tmp_path
    ):
        # Arrange
        from scitex_agent_container.config._host import resolve_hostname

        _write_scitex_config(
            tmp_path / ".scitex",
            "spec:\n  hostname_aliases:\n    Yusukes-MacBook-Air: mba\n",
        )
        # Act
        result = resolve_hostname(gethostname=lambda: "ywata-note-win")
        # Assert
        assert result == "ywata-note-win"

    def test_missing_config_file_falls_back_to_identity(self, host_env_with_scitex_dir):
        # Arrange: host_env_with_scitex_dir points SCITEX_DIR at empty tmp_path.
        from scitex_agent_container.config._host import resolve_hostname

        # Act
        result = resolve_hostname(gethostname=lambda: "bare-host")
        # Assert
        assert result == "bare-host"


# ---------------------------------------------------------------------------
# 3. Effective-id composition
# ---------------------------------------------------------------------------


class TestEffectiveId:
    """compose_effective_name: dir name + HostsSpec + hostname -> effective id."""

    def test_hosts_all_appends_hostname_suffix(self):
        # Arrange
        from scitex_agent_container.config import HostsSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        # Act
        result = compose_effective_name(
            "head", HostsSpec(hosts="all"), "ywata-note-win"
        )
        # Assert
        assert result == "head-ywata-note-win"

    def test_already_suffixed_name_is_left_idempotent(self):
        # Arrange
        from scitex_agent_container.config import HostsSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        # Act
        result = compose_effective_name(
            "head-ywata-note-win",
            HostsSpec(hosts="all"),
            "ywata-note-win",
        )
        # Assert
        assert result == "head-ywata-note-win"

    def test_host_singleton_keeps_bare_name_on_preferred_host(self):
        # Arrange
        from scitex_agent_container.config import HostsSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        spec = HostsSpec(host=["ywata-note-win", "mba"])
        # Act
        result = compose_effective_name("lead", spec, "ywata-note-win")
        # Assert
        assert result == "lead"

    def test_host_singleton_keeps_bare_name_on_fallback_host(self):
        # Arrange
        from scitex_agent_container.config import HostsSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        spec = HostsSpec(host=["ywata-note-win", "mba"])
        # Act
        result = compose_effective_name("lead", spec, "mba")
        # Assert
        assert result == "lead"

    def test_empty_hosts_spec_keeps_bare_name(self):
        # Arrange
        from scitex_agent_container.config import HostsSpec
        from scitex_agent_container.config._loaders import compose_effective_name

        # Act
        result = compose_effective_name("lead", HostsSpec(), "anywhere")
        # Assert
        assert result == "lead"

    def test_load_yields_host_suffixed_id_for_multi(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        env_save_restore.set("HOME", str(tmp_path))
        head_dir = tmp_path / "head"
        head_dir.mkdir()
        (head_dir / "head.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: head
                    machine: ${HOSTNAME}
                spec:
                  runtime: apptainer
                  hosts: all
                """
            )
        )
        # Act
        cfg = load_config(str(head_dir / "head.yaml"))
        # Assert
        assert cfg.name == "head-ywata-note-win"

    def test_load_substitutes_hostname_in_labels(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        env_save_restore.set("HOME", str(tmp_path))
        head_dir = tmp_path / "head"
        head_dir.mkdir()
        (head_dir / "head.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    machine: ${HOSTNAME}
                spec:
                  runtime: apptainer
                  hosts: all
                """
            )
        )
        # Act
        cfg = load_config(str(head_dir / "head.yaml"))
        # Assert
        assert cfg.labels["machine"] == "ywata-note-win"

    def test_load_sets_hosts_all_sentinel_in_spec(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        env_save_restore.set("HOME", str(tmp_path))
        head_dir = tmp_path / "head"
        head_dir.mkdir()
        (head_dir / "head.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: head
                spec:
                  runtime: apptainer
                  hosts: all
                """
            )
        )
        # Act
        cfg = load_config(str(head_dir / "head.yaml"))
        # Assert
        assert cfg.hosts_spec.hosts == "all"

    def test_load_workdir_is_runtime_path_for_multi_host(
        self, env_save_restore, tmp_path
    ):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        env_save_restore.set("HOME", str(tmp_path))
        head_dir = tmp_path / "head"
        head_dir.mkdir()
        (head_dir / "head.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: head
                spec:
                  runtime: apptainer
                  hosts: all
                """
            )
        )
        # Act
        cfg = load_config(str(head_dir / "head.yaml"))
        # Assert
        assert cfg.workdir == (
            "~/.scitex/agent-container/runtime/agents/head-ywata-note-win"
        )

    def test_load_singleton_keeps_bare_id(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        d = tmp_path / "lead"
        d.mkdir()
        (d / "lead.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: lead
                spec:
                  runtime: apptainer
                  host:
                    - ywata-note-win
                    - mba
                    - nas
                    - spartan
                """
            )
        )
        # Act
        cfg = load_config(str(d / "lead.yaml"))
        # Assert
        assert cfg.name == "lead"

    def test_load_singleton_preserves_host_list(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        d = tmp_path / "lead"
        d.mkdir()
        (d / "lead.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: lead
                spec:
                  runtime: apptainer
                  host:
                    - ywata-note-win
                    - mba
                    - nas
                    - spartan
                """
            )
        )
        # Act
        cfg = load_config(str(d / "lead.yaml"))
        # Assert
        assert cfg.hosts_spec.host == [
            "ywata-note-win",
            "mba",
            "nas",
            "spartan",
        ]

    def test_load_singleton_leaves_hosts_field_empty(self, env_save_restore, tmp_path):
        # Arrange
        from scitex_agent_container.config import load_config

        env_save_restore.set("SCITEX_AGENT_CONTAINER_HOSTNAME", "ywata-note-win")
        d = tmp_path / "lead"
        d.mkdir()
        (d / "lead.yaml").write_text(
            dedent(
                """\
                apiVersion: scitex-agent-container/v3
                kind: Agent
                metadata:
                  labels:
                    role: lead
                spec:
                  runtime: apptainer
                  host:
                    - ywata-note-win
                    - mba
                """
            )
        )
        # Act
        cfg = load_config(str(d / "lead.yaml"))
        # Assert
        assert cfg.hosts_spec.hosts == ""


# ---------------------------------------------------------------------------
# 4. Singleton enforcement
# ---------------------------------------------------------------------------


class TestSingletonEnforcement:
    def _cfg(self, host=None, hosts=None):
        from scitex_agent_container.config import AgentConfig, HostsSpec

        spec = HostsSpec()
        if host is not None:
            spec.host = host
        if hosts is not None:
            spec.hosts = hosts
        return AgentConfig(name="lead", hosts_spec=spec)

    def test_preferred_host_match_returns_none(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        result = _singleton_skip_reason(cfg, "ywata-note-win")
        # Assert
        assert result is None

    def test_fallback_host_first_in_chain_returns_none(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        result = _singleton_skip_reason(cfg, "mba")
        # Assert
        assert result is None

    def test_fallback_host_last_in_chain_returns_none(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        result = _singleton_skip_reason(cfg, "nas")
        # Assert
        assert result is None

    def test_offlist_host_returns_skip_reason(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        reason = _singleton_skip_reason(cfg, "spartan")
        # Assert
        assert reason is not None

    def test_offlist_host_skip_reason_mentions_preferred(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        reason = _singleton_skip_reason(cfg, "spartan")
        # Assert
        assert "ywata-note-win" in reason

    def test_offlist_host_skip_reason_mentions_current_host(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        reason = _singleton_skip_reason(cfg, "spartan")
        # Assert
        assert "spartan" in reason

    def test_offlist_host_skip_reason_mentions_fallback_hosts(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(host=["ywata-note-win", "mba", "nas"])
        # Act
        reason = _singleton_skip_reason(cfg, "spartan")
        # Assert
        assert "fallback-hosts" in reason

    def test_multi_instance_never_skips_on_fallback_host(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(hosts="all")
        # Act
        result = _singleton_skip_reason(cfg, "mba")
        # Assert
        assert result is None

    def test_multi_instance_never_skips_on_preferred_host(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg(hosts="all")
        # Act
        result = _singleton_skip_reason(cfg, "ywata-note-win")
        # Assert
        assert result is None

    def test_local_singleton_never_skips_anywhere(self):
        # Arrange
        from scitex_agent_container.cli_pkg.lifecycle._common import (
            _singleton_skip_reason,
        )

        cfg = self._cfg()
        # Act
        result = _singleton_skip_reason(cfg, "any-host")
        # Assert
        assert result is None


# ---------------------------------------------------------------------------
# 5. Hosts-spec parser
# ---------------------------------------------------------------------------


class TestHostsSpecParser:
    def test_absent_keys_yield_empty_host(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({})
        # Assert
        assert spec.host == ""

    def test_absent_keys_yield_empty_hosts(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({})
        # Assert
        assert spec.hosts == ""

    def test_host_string_is_preserved_verbatim(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"host": "spartan"})
        # Assert
        assert spec.host == "spartan"

    def test_host_string_leaves_hosts_empty(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"host": "spartan"})
        # Assert
        assert spec.hosts == ""

    def test_host_list_is_preserved_as_list(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"host": ["a", "b", "c"]})
        # Assert
        assert spec.host == ["a", "b", "c"]

    def test_host_list_leaves_hosts_empty(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"host": ["a", "b", "c"]})
        # Assert
        assert spec.hosts == ""

    def test_hosts_all_sentinel_preserved_as_string(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"hosts": "all"})
        # Assert
        assert spec.hosts == "all"

    def test_hosts_all_sentinel_leaves_host_empty(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"hosts": "all"})
        # Assert
        assert spec.host == ""

    def test_hosts_list_preserved_as_list(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"hosts": ["mba", "nas"]})
        # Assert
        assert spec.hosts == ["mba", "nas"]

    def test_hosts_list_leaves_host_empty(self):
        # Arrange
        from scitex_agent_container.config._parsers import parse_hosts_spec

        # Act
        spec = parse_hosts_spec({"hosts": ["mba", "nas"]})
        # Assert
        assert spec.host == ""


class TestHostsSpecValidation:
    """Mutually-exclusive + type checks happen in _validation, not the parser."""

    def _validate(self, spec_extra: dict) -> list[str]:
        from scitex_agent_container.config._validation import validate_raw

        return validate_raw(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": {"runtime": "apptainer", **spec_extra},
            },
            "test.yaml",
        )

    def test_both_host_and_hosts_set_is_rejected(self):
        # Arrange
        spec_extra = {"host": "a", "hosts": "all"}
        # Act
        errors = self._validate(spec_extra)
        # Assert
        assert any("mutually exclusive" in e for e in errors)

    def test_legacy_scheduling_block_is_rejected(self):
        # Arrange
        spec_extra = {"scheduling": {"mode": "per-host"}}
        # Act
        errors = self._validate(spec_extra)
        # Assert
        assert any("scheduling block is no longer accepted" in e for e in errors)

    def test_invalid_hosts_string_is_rejected(self):
        # Arrange
        spec_extra = {"hosts": "everything"}
        # Act
        errors = self._validate(spec_extra)
        # Assert
        assert any("'all'" in e for e in errors)

    def test_invalid_host_type_is_rejected(self):
        # Arrange
        spec_extra = {"host": 123}
        # Act
        errors = self._validate(spec_extra)
        # Assert
        assert any("must be a string, list of strings, or empty" in e for e in errors)
