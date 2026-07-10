"""Tests for cli_pkg.lifecycle._start_group_filter.

``resolve_group_targets`` walks the REAL on-disk agent discovery
(``_discover_defined_agents``) against REAL spec.yaml fixtures under a
tmp-rooted HOME — no mocks. Tests also chdir into that tmp root so the
project-scope branch of discovery (which walks UP from cwd looking for
a checked-in ``.scitex/agent-container/``) cannot pick up this very
repo's own dev fixture agent when the test happens to run from the
repo root (matches the isolation pattern documented in
``test__agent_list.py``'s ``_discover_defined_agents`` section, made
explicit here via a real save/restore chdir since HOME alone does not
suppress it -- the project-scope walk is cwd-rooted, not HOME-rooted).

AAA, one assertion per test, no mocks/monkeypatch (STX-NM002).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg.lifecycle._start_group_filter import (
    apply_group_targets,
    resolve_group_targets,
)


def _write_agent_spec(agents_root: Path, name: str, *, groups_yaml: str) -> Path:
    """Write a REAL, fully-valid spec.yaml under ``<agents_root>/<name>/``.

    ``groups_yaml`` is the raw flow-list text for ``metadata.labels.groups``,
    e.g. ``"[developer]"`` or ``"[generalist, developer]"``. Every field
    ``load_config`` requires (host, apptainer.image/binds, health,
    restart, claude.model) is included so the fixture loads cleanly.
    """
    d = agents_root / name
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text(
        "apiVersion: scitex-agent-container/v3\n"
        "kind: Agent\n"
        "metadata:\n"
        "  labels:\n"
        f"    groups: {groups_yaml}\n"
        "spec:\n"
        "  runtime: tui\n"
        "  host: local\n"
        "  workdir: /home/agent/work\n"
        "  apptainer:\n    image: /x.sif\n    binds: []\n"
        "  health:\n    enabled: true\n    interval: 60\n"
        "  restart:\n    policy: on-failure\n    max_retries: 3\n"
        "  claude:\n    model: sonnet\n"
    )
    return d / "spec.yaml"


@pytest.fixture
def isolated_agents_root(tmp_path, env_save_restore) -> Path:
    """Tmp-rooted HOME + chdir so discovery sees ONLY this test's fixtures.

    Real save/restore (no monkeypatch): the cwd is saved, changed to
    ``tmp_path``, and restored on teardown regardless of test outcome.
    """
    saved_cwd = os.getcwd()
    os.chdir(tmp_path)
    env_save_restore.set("HOME", str(tmp_path))
    root = tmp_path / ".scitex" / "agent-container" / "agents"
    root.mkdir(parents=True)
    try:
        yield root
    finally:
        os.chdir(saved_cwd)


class TestResolveGroupTargets:
    def test_empty_wanted_returns_empty(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(())
        # Assert
        assert result == []

    def test_single_group_matches_agent(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == ["alpha"]

    def test_non_matching_agent_is_excluded(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        _write_agent_spec(isolated_agents_root, "beta", groups_yaml="[researcher]")
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == ["alpha"]

    def test_matching_is_case_insensitive(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("DEVELOPER",))
        # Assert
        assert result == ["alpha"]

    def test_nonexistent_group_returns_empty(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("nonexistent",))
        # Assert
        assert result == []

    def test_agent_with_multiple_groups_matches_non_first_element(
        self, isolated_agents_root
    ):
        # Arrange — grant-style spec: developer is NOT the first element,
        # so the ACL-effective group_from_labels would miss it; the
        # multi-value bulk-select reader must not.
        _write_agent_spec(
            isolated_agents_root,
            "grant",
            groups_yaml="[generalist, privileged, developer, researcher]",
        )
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == ["grant"]

    def test_multiple_wanted_groups_union_across_agents(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        _write_agent_spec(isolated_agents_root, "beta", groups_yaml="[researcher]")
        _write_agent_spec(isolated_agents_root, "gamma", groups_yaml="[generalist]")
        # Act
        result = resolve_group_targets(("developer", "researcher"))
        # Assert
        assert result == ["alpha", "beta"]

    def test_result_is_sorted(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "zeta", groups_yaml="[developer]")
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == ["alpha", "zeta"]

    def test_blank_wanted_values_are_ignored(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("   ", ""))
        # Assert
        assert result == []

    def test_broken_spec_is_excluded_not_raised(self, isolated_agents_root):
        # Arrange — a spec missing every required field must not crash
        # resolution; it is simply excluded from every group.
        broken = isolated_agents_root / "broken"
        broken.mkdir()
        (broken / "spec.yaml").write_text("apiVersion: x\n")
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == ["alpha"]

    def test_ungrouped_agent_never_matches(self, isolated_agents_root):
        # Arrange — spec with no groups label at all.
        d = isolated_agents_root / "ungrouped"
        d.mkdir()
        (d / "spec.yaml").write_text(
            "apiVersion: scitex-agent-container/v3\n"
            "kind: Agent\n"
            "spec:\n"
            "  runtime: tui\n"
            "  host: local\n"
            "  workdir: /home/agent/work\n"
            "  apptainer:\n    image: /x.sif\n    binds: []\n"
            "  health:\n    enabled: true\n    interval: 60\n"
            "  restart:\n    policy: on-failure\n    max_retries: 3\n"
            "  claude:\n    model: sonnet\n"
        )
        # Act
        result = resolve_group_targets(("developer",))
        # Assert
        assert result == []


class TestApplyGroupTargets:
    def test_targets_pass_through_unchanged_when_no_groups(self):
        # Arrange
        targets = ("alpha", "beta")
        # Act
        result = apply_group_targets(targets, ())
        # Assert
        assert result == ("alpha", "beta")

    def test_both_empty_exits_two(self):
        # Arrange
        # Act
        # Assert
        with pytest.raises(SystemExit, match="^2$"):
            apply_group_targets((), ())

    def test_both_empty_message_mentions_group(self, capsys):
        # Arrange
        # Act
        try:
            apply_group_targets((), ())
        except SystemExit:
            pass
        # Assert
        assert "--group" in capsys.readouterr().err

    def test_zero_match_group_exits_two(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        # Assert
        with pytest.raises(SystemExit, match="^2$"):
            apply_group_targets((), ("nonexistent",))

    def test_zero_match_group_message_names_the_group(
        self, isolated_agents_root, capsys
    ):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        try:
            apply_group_targets((), ("nonexistent",))
        except SystemExit:
            pass
        # Assert
        assert "nonexistent" in capsys.readouterr().err

    def test_matching_group_resolves_with_no_explicit_targets(
        self, isolated_agents_root
    ):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = apply_group_targets((), ("developer",))
        # Assert
        assert result == ("alpha",)

    def test_explicit_targets_and_group_are_unioned(self, isolated_agents_root):
        # Arrange
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = apply_group_targets(("other-agent",), ("developer",))
        # Assert
        assert result == ("other-agent", "alpha")

    def test_union_is_de_duplicated(self, isolated_agents_root):
        # Arrange — "alpha" is both an explicit target AND group-resolved.
        _write_agent_spec(isolated_agents_root, "alpha", groups_yaml="[developer]")
        # Act
        result = apply_group_targets(("alpha",), ("developer",))
        # Assert
        assert result == ("alpha",)
