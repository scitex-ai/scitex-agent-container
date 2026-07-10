"""Tests for config._group_sync.

``sync_groups_line`` is a pure text-in/text-out function (no I/O) —
covered directly. ``discover_symlink_farm_groups`` and
``sync_agent_groups_from_symlink_farm`` touch the real filesystem via
``tmp_path`` — no mocks.

AAA, one assertion per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.config._group_sync import (
    discover_symlink_farm_groups,
    sync_agent_groups_from_symlink_farm,
    sync_groups_line,
)

# ---------------------------------------------------------------------------
# sync_groups_line — pure text editor
# ---------------------------------------------------------------------------


def test_sync_groups_line_appends_missing_group():
    # Arrange
    text = "metadata:\n  labels:\n    groups: [developer]\n    role: x\n"
    # Act
    new_text, changed = sync_groups_line(text, "active")
    # Assert
    assert "groups: [developer, active]" in new_text


def test_sync_groups_line_reports_changed_true_on_append():
    # Arrange
    text = "    groups: [developer]\n"
    # Act
    _new_text, changed = sync_groups_line(text, "active")
    # Assert
    assert changed is True


def test_sync_groups_line_is_idempotent_when_already_present():
    # Arrange
    text = "    groups: [developer, active]\n"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert new_text == text


def test_sync_groups_line_already_present_reports_changed_false():
    # Arrange
    text = "    groups: [developer, active]\n"
    # Act
    _new_text, changed = sync_groups_line(text, "active")
    # Assert
    assert changed is False


def test_sync_groups_line_preserves_surrounding_lines():
    # Arrange
    text = "spec:\n  runtime: tui\n    groups: [developer]\n  workdir: /x\n"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert "  workdir: /x\n" in new_text


def test_sync_groups_line_preserves_indentation():
    # Arrange
    text = "    groups: [developer]\n"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert new_text.startswith("    groups:")


def test_sync_groups_line_no_groups_line_reports_changed_false():
    # Arrange
    text = "metadata:\n  labels:\n    role: x\n"
    # Act
    _new_text, changed = sync_groups_line(text, "active")
    # Assert
    assert changed is False


def test_sync_groups_line_no_groups_line_leaves_text_untouched():
    # Arrange
    text = "metadata:\n  labels:\n    role: x\n"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert new_text == text


def test_sync_groups_line_blank_group_is_a_no_op():
    # Arrange
    text = "    groups: [developer]\n"
    # Act
    _new_text, changed = sync_groups_line(text, "   ")
    # Assert
    assert changed is False


def test_sync_groups_line_handles_single_element_list():
    # Arrange
    text = "    groups: [privileged]\n"
    # Act
    new_text, _changed = sync_groups_line(text, "infra")
    # Assert
    assert "groups: [privileged, infra]" in new_text


def test_sync_groups_line_preserves_trailing_comment():
    # Arrange
    text = "    groups: [developer]  # dev cohort\n"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert "# dev cohort" in new_text


def test_sync_groups_line_preserves_no_trailing_newline():
    # Arrange
    text = "    groups: [developer]"
    # Act
    new_text, _changed = sync_groups_line(text, "active")
    # Assert
    assert new_text == "    groups: [developer, active]"


# ---------------------------------------------------------------------------
# discover_symlink_farm_groups — real symlinks under tmp_path
# ---------------------------------------------------------------------------


def _make_agent_dir(agents_root: Path, name: str) -> Path:
    d = agents_root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "spec.yaml").write_text("spec:\n  runtime: tui\n")
    return d


def test_discover_symlink_farm_groups_finds_membership(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    _make_agent_dir(agents_root, "figrecipe")
    group_dir = agents_root / "_group_active"
    group_dir.mkdir()
    (group_dir / "figrecipe").symlink_to(agents_root / "figrecipe")
    # Act
    result = discover_symlink_farm_groups(agents_root)
    # Assert
    assert result == {"figrecipe": {"active"}}


def test_discover_symlink_farm_groups_unions_multiple_farms(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    _make_agent_dir(agents_root, "scitex-dev")
    for group in ("active", "infra"):
        group_dir = agents_root / f"_group_{group}"
        group_dir.mkdir()
        (group_dir / "scitex-dev").symlink_to(agents_root / "scitex-dev")
    # Act
    result = discover_symlink_farm_groups(agents_root)
    # Assert
    assert result == {"scitex-dev": {"active", "infra"}}


def test_discover_symlink_farm_groups_ignores_non_symlink_entries(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    group_dir = agents_root / "_group_active"
    group_dir.mkdir(parents=True)
    (group_dir / "not-a-link").write_text("x")
    # Act
    result = discover_symlink_farm_groups(agents_root)
    # Assert
    assert result == {}


def test_discover_symlink_farm_groups_ignores_non_group_dirs(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    _make_agent_dir(agents_root, "figrecipe")
    # Act
    result = discover_symlink_farm_groups(agents_root)
    # Assert
    assert result == {}


def test_discover_symlink_farm_groups_missing_root_returns_empty(tmp_path):
    # Arrange
    agents_root = tmp_path / "does-not-exist"
    # Act
    result = discover_symlink_farm_groups(agents_root)
    # Assert
    assert result == {}


# ---------------------------------------------------------------------------
# sync_agent_groups_from_symlink_farm — end-to-end over real files
# ---------------------------------------------------------------------------


def test_sync_agent_groups_writes_the_missing_group(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    d = agents_root / "figrecipe"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("metadata:\n  labels:\n    groups: [developer]\n")
    group_dir = agents_root / "_group_active"
    group_dir.mkdir()
    (group_dir / "figrecipe").symlink_to(d)
    # Act
    sync_agent_groups_from_symlink_farm(agents_root)
    # Assert
    assert "active" in (d / "spec.yaml").read_text()


def test_sync_agent_groups_report_names_the_added_group(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    d = agents_root / "figrecipe"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text("metadata:\n  labels:\n    groups: [developer]\n")
    group_dir = agents_root / "_group_active"
    group_dir.mkdir()
    (group_dir / "figrecipe").symlink_to(d)
    # Act
    report = sync_agent_groups_from_symlink_farm(agents_root)
    # Assert
    assert report == {"figrecipe": ["active"]}


def test_sync_agent_groups_already_synced_is_empty_report(tmp_path):
    # Arrange
    agents_root = tmp_path / "agents"
    d = agents_root / "figrecipe"
    d.mkdir(parents=True)
    (d / "spec.yaml").write_text(
        "metadata:\n  labels:\n    groups: [developer, active]\n"
    )
    group_dir = agents_root / "_group_active"
    group_dir.mkdir()
    (group_dir / "figrecipe").symlink_to(d)
    # Act
    report = sync_agent_groups_from_symlink_farm(agents_root)
    # Assert
    assert report == {}


def test_sync_agent_groups_missing_spec_is_skipped_not_raised(tmp_path):
    # Arrange — symlink exists but the target dir has no spec.yaml.
    agents_root = tmp_path / "agents"
    d = agents_root / "ghost"
    d.mkdir(parents=True)
    group_dir = agents_root / "_group_active"
    group_dir.mkdir()
    (group_dir / "ghost").symlink_to(d)
    # Act
    report = sync_agent_groups_from_symlink_farm(agents_root)
    # Assert
    assert report == {}
