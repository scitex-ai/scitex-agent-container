"""Tests for the ``to_home_layers`` spec text editor.

The migration touches 102 hand-maintained spec.yaml files, so these pin the
things a bulk rewrite gets wrong: everything the edit did not intend to touch
stays byte-identical, a re-run does not duplicate the key, and a spec whose
shape is unrecognised is REFUSED rather than guessed at.

STX-NM002: no mocks — pure string in, string out.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.config._to_home_layers_line import (
    insert_to_home_layers,
    render_layers_value,
)

_SPEC = (
    "apiVersion: scitex-agent-container/v3\n"
    "kind: Agent\n"
    "spec:\n"
    "  runtime: tui\n"
    "  to_home: ./to_home\n"
    "  # a trailing comment that must survive\n"
    "  workdir: /home/ywatanabe/proj/x\n"
)


def test_declaration_is_inserted_after_the_anchor() -> None:
    # Arrange
    text = _SPEC
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared", "per-agent"])
    # Assert
    assert "  to_home: ./to_home\n  to_home_layers: [user-shared, per-agent]\n" in new


def test_insertion_reports_changed() -> None:
    # Arrange
    text = _SPEC
    # Act
    _, changed = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert changed is True


def test_every_other_line_survives_byte_identical() -> None:
    # Arrange — the whole point of a line edit over a load+dump cycle.
    text = _SPEC
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert [ln for ln in new.splitlines() if "to_home_layers" not in ln] == (
        text.splitlines()
    )


def test_the_anchor_indent_is_reused() -> None:
    # Arrange — a deeper-nested spec must not get a flat insertion.
    text = "spec:\n  nested:\n      to_home: ./to_home\n"
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert "\n      to_home_layers: [user-shared]\n" in new


def test_rerunning_does_not_duplicate_the_key() -> None:
    # Arrange — idempotence; the migration must be safe to re-run.
    once, _ = insert_to_home_layers(_SPEC, ["user-shared"])
    # Act
    twice, _ = insert_to_home_layers(once, ["user-shared"])
    # Assert
    assert twice.count("to_home_layers:") == 1


def test_rerunning_reports_unchanged() -> None:
    # Arrange
    once, _ = insert_to_home_layers(_SPEC, ["user-shared"])
    # Act
    _, changed = insert_to_home_layers(once, ["user-shared"])
    # Assert
    assert changed is False


def test_an_existing_declaration_below_line_one_is_still_detected() -> None:
    # Arrange — a bare regex search without MULTILINE only sees position 0,
    # which would silently duplicate the key on every re-run.
    text = "spec:\n  to_home: ./to_home\n  to_home_layers: [user-shared]\n"
    # Act
    _, changed = insert_to_home_layers(text, ["per-agent"])
    # Assert
    assert changed is False


def test_a_spec_without_the_anchor_is_refused() -> None:
    # Arrange — one real spec on this host has no to_home: line.
    text = "spec:\n  runtime: tui\n  workdir: /tmp\n"
    # Act
    _, changed = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert changed is False


def test_a_refused_spec_is_returned_untouched() -> None:
    # Arrange — refusal must not be a partial write.
    text = "spec:\n  runtime: tui\n"
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert new == text


def test_crlf_line_endings_are_preserved() -> None:
    # Arrange — a spec edited on Windows must not gain a lone LF.
    text = "spec:\r\n  to_home: ./to_home\r\n  workdir: /tmp\r\n"
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert "\r\n  to_home_layers: [user-shared]\r\n" in new


def test_an_unterminated_final_line_gains_a_terminator() -> None:
    # Arrange — without one, the insert would fuse two keys onto one line.
    text = "spec:\n  to_home: ./to_home"
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert new == "spec:\n  to_home: ./to_home\n  to_home_layers: [user-shared]\n"


def test_an_empty_declaration_renders_as_an_empty_flow_list() -> None:
    # Arrange — "inherit nothing" is a real declaration, not an omission.
    # Act
    rendered = render_layers_value([])
    # Assert
    assert rendered == "[]"


def test_it_composes_with_the_other_spec_editor_in_either_order() -> None:
    # Arrange — `_group_sync.sync_groups_line` ALREADY bulk-edits these same
    # hand-maintained spec files. Two independent line editors over 102 files
    # is exactly the shape that yields a corrupted spec nobody can attribute,
    # so their independence is pinned rather than assumed.
    from scitex_agent_container.config._group_sync import sync_groups_line

    spec = (
        "metadata:\n"
        "  labels:\n"
        "    groups: [developer, infra]\n"
        "spec:\n"
        "  to_home: ./to_home\n"
    )
    # Act
    a, _ = sync_groups_line(insert_to_home_layers(spec, ["user-shared"])[0], "active")
    b, _ = insert_to_home_layers(sync_groups_line(spec, "active")[0], ["user-shared"])
    # Assert
    assert a == b


def test_the_other_editors_line_is_left_intact() -> None:
    # Arrange — neither editor may disturb the line the other owns.
    from scitex_agent_container.config._group_sync import sync_groups_line

    spec = "metadata:\n  groups: [developer]\nspec:\n  to_home: ./to_home\n"
    # Act
    both, _ = sync_groups_line(
        insert_to_home_layers(spec, ["user-shared"])[0], "active"
    )
    # Assert
    assert "  groups: [developer, active]\n" in both


def test_only_the_first_anchor_is_used() -> None:
    # Arrange — two to_home: lines is a shape we have not seen; touch one.
    text = "a:\n  to_home: ./x\nb:\n  to_home: ./y\n"
    # Act
    new, _ = insert_to_home_layers(text, ["user-shared"])
    # Assert
    assert new.count("to_home_layers:") == 1
