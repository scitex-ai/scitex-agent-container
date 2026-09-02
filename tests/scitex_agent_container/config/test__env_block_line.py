"""Tests for the ``spec.apptainer.env`` text editor.

This editor replaces a hand-written ``sed`` script, so what is pinned here is
mostly the set of things that script got wrong and a rewrite must not repeat:

* a key that also names a field in ANOTHER block (``model:`` under ``claude:``)
  must not be read from, or written to, that other block;
* only the targeted line may move — every other byte, comments included, stays;
* a key already at its target must leave the text BYTE-identical, because the
  CLI decides both "skip the write" and "skip the restart" from that; and
* a value shape the editor cannot rewrite on one line is REFUSED by name
  rather than half-deleted.

The idempotence assertions compare against the LOADER's own reading of a
value (``str(...)``, as ``parse_apptainer`` does), not against the raw bytes:
``KEY: 1`` and ``KEY: "1"`` are the same environment variable.

STX-NM002: no mocks, no monkeypatch — string in, string out.
STX-TQ007: one logical assert per test.
"""

from __future__ import annotations

import pytest
import yaml

from scitex_agent_container.config._env_block_line import (
    ABSENT,
    ADDED,
    REFUSED_NO_APPTAINER,
    REMOVED,
    UNCHANGED,
    UPDATED,
    EnvEdit,
    MalformedAssignment,
    parse_assignment,
    render_scalar,
    set_env,
    should_restart,
    unset_env,
    validate_key,
)

# The real shape of a fleet spec, decoys included: a `model:` key under
# `claude:` (the retired script's first-match bug), a comment inside the env
# block, and a sibling key after it.
_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  host: ywata-note-win
  apptainer:
    image: /x.sif
    raw_args:
    - --userns
    env:
      # 2026-08-12: the pool could never resolve this slot.
      CCT_BOT_TOKEN_SLOT: SAC
    post: ''
  claude:
    model: fable
"""


def _env_of(text: str) -> dict:
    """The env block as the loader reads it — the value under test, not bytes."""
    return yaml.safe_load(text)["spec"]["apptainer"]["env"]


def _attempt(fn, *args):
    """Run ``fn`` and return either its result or the exception it raised.

    Turns "does this raise?" into a value the Assert step can inspect, so the
    Act and Assert steps stay separable instead of collapsing into one
    ``pytest.raises`` block.
    """
    try:
        return fn(*args)
    except Exception as exc:  # stx-allow: fallback (reason: the raised exception IS the observation under test)
        return exc


# ---------------------------------------------------------------------------
# set — the happy paths


def test_a_new_key_is_added_to_the_existing_env_block() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("NEW_VAR", "1")])
    # Assert
    assert _env_of(edit.text)["NEW_VAR"] == "1"


def test_a_new_key_lands_inside_env_and_not_after_the_next_sibling() -> None:
    # Arrange — `post: ''` follows the env block; an anchor at the block's
    # end must not step over it.
    text = _SPEC
    # Act
    edit = set_env(text, [("NEW_VAR", "1")])
    # Assert
    assert '      NEW_VAR: "1"\n    post:' in edit.text


def test_an_existing_key_is_replaced_in_place() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "OTHER")])
    # Assert
    assert _env_of(edit.text)["CCT_BOT_TOKEN_SLOT"] == "OTHER"


def test_replacing_a_key_keeps_its_position_in_the_block() -> None:
    # Arrange — order is how an operator finds a key again; a rewrite that
    # appends is a diff nobody can read.
    text = set_env(_SPEC, [("SECOND", "b")]).text
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "OTHER")])
    # Assert
    assert list(_env_of(edit.text)) == ["CCT_BOT_TOKEN_SLOT", "SECOND"]


def test_replacing_a_key_keeps_the_comment_above_it() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "OTHER")])
    # Assert
    assert "# 2026-08-12: the pool could never resolve this slot." in edit.text


def test_an_edit_leaves_every_other_line_untouched() -> None:
    # Arrange — the retired script's sed rewrote every indented `K:` line in
    # the file; this pins that exactly one line differs.
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "OTHER")])
    differing = [
        (a, b) for a, b in zip(text.splitlines(), edit.text.splitlines()) if a != b
    ]
    # Assert
    assert len(differing) == 1


def test_a_key_that_also_names_a_field_elsewhere_is_not_read_from_that_block() -> None:
    # Arrange — `spec.claude.model` is the first `^\\s+model:` line in the
    # document; the retired script compared against it.
    text = _SPEC
    # Act
    edit = set_env(text, [("model", "fable")])
    # Assert — a genuine addition to env, NOT "already at target".
    assert edit.changes[0].action == ADDED


def test_a_key_that_also_names_a_field_elsewhere_does_not_rewrite_that_field() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("model", "SOMETHING-ELSE")])
    # Assert
    assert yaml.safe_load(edit.text)["spec"]["claude"]["model"] == "fable"


def test_several_keys_are_applied_in_one_edit() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("A", "1"), ("B", "2")])
    # Assert
    assert (_env_of(edit.text)["A"], _env_of(edit.text)["B"]) == ("1", "2")


# ---------------------------------------------------------------------------
# set — idempotence, which the CLI's write AND restart decisions rest on


def test_setting_a_key_to_its_current_value_reports_unchanged() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "SAC")])
    # Assert
    assert edit.changes[0].action == UNCHANGED


def test_setting_a_key_to_its_current_value_returns_the_text_byte_identical() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "SAC")])
    # Assert
    assert edit.text == text


def test_setting_a_key_to_its_current_value_reports_not_changed() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = set_env(text, [("CCT_BOT_TOKEN_SLOT", "SAC")])
    # Assert
    assert edit.changed is False


def test_a_quoted_and_an_unquoted_spelling_of_one_value_are_the_same_value() -> None:
    # Arrange — the loader stringifies, so `1` and `"1"` are one env value and
    # rewriting between them would be a diff that changes nothing.
    text = _SPEC.replace("CCT_BOT_TOKEN_SLOT: SAC", "PORT: 8080")
    # Act
    edit = set_env(text, [("PORT", "8080")])
    # Assert
    assert edit.changed is False


def test_a_second_identical_run_over_an_edited_spec_changes_nothing() -> None:
    # Arrange
    once = set_env(_SPEC, [("NEW_VAR", "a:b")]).text
    # Act
    twice = set_env(once, [("NEW_VAR", "a:b")])
    # Assert
    assert twice.text == once


# ---------------------------------------------------------------------------
# set — shapes the block can be in


def test_an_env_block_is_created_when_the_spec_has_none() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    image: /x.sif\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert _env_of(edit.text) == {"A": "1"}


def test_a_created_env_block_sits_above_a_trailing_comment_it_does_not_own() -> None:
    # Arrange — a comment at the end of the apptainer block introduces the
    # NEXT key; an insertion below it would orphan it.
    text = "spec:\n  apptainer:\n    image: /x.sif\n    # about claude\n  claude:\n    model: fable\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert '      A: "1"\n    # about claude\n' in edit.text


def test_an_empty_flow_mapping_env_is_reopened_as_a_block() -> None:
    # Arrange — `env: {}` is the shape a yaml.safe_dump round-trip leaves.
    text = "spec:\n  apptainer:\n    env: {}\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert _env_of(edit.text) == {"A": "1"}


def test_a_comment_on_the_env_key_line_survives_reopening_the_block() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    env: {}  # nothing yet\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert "env:  # nothing yet\n" in edit.text


def test_a_childless_env_key_gains_its_first_child() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    image: /x.sif\n    env:\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert _env_of(edit.text) == {"A": "1"}


def test_a_final_line_with_no_terminator_does_not_fuse_with_the_insertion() -> None:
    # Arrange — no trailing newline is legal and is where naive inserters
    # produce `image: /x.sif    env:` on one line.
    text = "spec:\n  apptainer:\n    image: /x.sif"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert _env_of(edit.text) == {"A": "1"}


def test_a_commented_apptainer_key_line_is_still_descended_into() -> None:
    # Arrange — `apptainer:  # engine block` opens a block; reading its note
    # as the block's value hides every key underneath.
    text = "spec:\n  apptainer:  # engine block\n    env:\n      A: 1\n"
    # Act
    edit = set_env(text, [("B", "2")])
    # Assert
    assert _env_of(edit.text) == {"A": 1, "B": "2"}


# ---------------------------------------------------------------------------
# set — values that need quoting, and shapes that are refused


@pytest.mark.parametrize(
    "value",
    ["a:b", "yes", "no", "*alias", "#hash", "1:2:3", "", "a # b", 'say "hi"'],
)
def test_a_value_round_trips_through_the_loader_unchanged(value: str) -> None:
    # Arrange — every one of these is mis-parsed when written unquoted.
    text = _SPEC
    # Act
    edit = set_env(text, [("V", value)])
    # Assert
    assert _env_of(edit.text)["V"] == value


def test_a_block_scalar_value_is_refused_rather_than_rewritten() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    env:\n      S: |\n        one\n        two\n"
    # Act
    edit = set_env(text, [("S", "x")])
    # Assert
    assert edit.refusal is not None


def test_a_refused_set_returns_the_original_text() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    env:\n      S: |\n        one\n        two\n"
    # Act
    edit = set_env(text, [("S", "x")])
    # Assert
    assert edit.text == text


def test_a_refusal_on_the_second_key_writes_neither() -> None:
    # Arrange — all-or-nothing: a half-applied spec is worse than none.
    text = "spec:\n  apptainer:\n    env:\n      S: |\n        one\n"
    # Act
    edit = set_env(text, [("OK", "1"), ("S", "x")])
    # Assert
    assert edit.text == text


def test_a_spec_with_no_apptainer_block_is_refused_by_name() -> None:
    # Arrange — creating an engine block for a spec that has none is a guess.
    text = "spec:\n  host: h\n"
    # Act
    edit = set_env(text, [("A", "1")])
    # Assert
    assert edit.refusal == REFUSED_NO_APPTAINER


def test_a_collection_valued_env_key_is_refused() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    env:\n      L: [1, 2]\n"
    # Act
    edit = set_env(text, [("L", "x")])
    # Assert
    assert edit.refusal is not None


# ---------------------------------------------------------------------------
# unset


def test_unsetting_a_present_key_removes_it() -> None:
    # Arrange
    text = set_env(_SPEC, [("EXTRA", "e")]).text
    # Act
    edit = unset_env(text, ["EXTRA"])
    # Assert
    assert "EXTRA" not in _env_of(edit.text)


def test_unsetting_a_present_key_reports_its_old_value() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = unset_env(text, ["CCT_BOT_TOKEN_SLOT"])
    # Assert
    assert edit.changes[0].old == "SAC"


def test_unsetting_a_present_key_reports_removed() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = unset_env(text, ["CCT_BOT_TOKEN_SLOT"])
    # Assert
    assert edit.changes[0].action == REMOVED


def test_unsetting_a_missing_key_is_a_no_op_not_an_error() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = unset_env(text, ["NOT_THERE"])
    # Assert
    assert edit.changes[0].action == ABSENT


def test_unsetting_a_missing_key_leaves_the_text_byte_identical() -> None:
    # Arrange
    text = _SPEC
    # Act
    edit = unset_env(text, ["NOT_THERE"])
    # Assert
    assert edit.text == text


def test_unsetting_from_a_spec_with_no_env_block_is_a_no_op() -> None:
    # Arrange
    text = "spec:\n  apptainer:\n    image: /x.sif\n"
    # Act
    edit = unset_env(text, ["A"])
    # Assert
    assert edit.changed is False


def test_removing_the_last_key_leaves_an_explicit_empty_mapping() -> None:
    # Arrange — a bare `env:` reads as a block whose contents went missing.
    text = "spec:\n  apptainer:\n    env:\n      ONLY: v\n    post: ''\n"
    # Act
    edit = unset_env(text, ["ONLY"])
    # Assert
    assert _env_of(edit.text) == {}


def test_a_document_emptied_of_env_keys_still_parses() -> None:
    # Arrange — an orphaned comment is kept, and YAML ignores its indent.
    text = "spec:\n  apptainer:\n    env:\n      # why\n      ONLY: v\n"
    # Act
    edit = unset_env(text, ["ONLY"])
    # Assert
    assert yaml.safe_load(edit.text)["spec"]["apptainer"]["env"] == {}


def test_unsetting_a_block_scalar_value_is_refused_rather_than_half_deleted() -> None:
    # Arrange — deleting only the first line strands the continuation.
    text = "spec:\n  apptainer:\n    env:\n      S: |\n        one\n        two\n"
    # Act
    edit = unset_env(text, ["S"])
    # Assert
    assert edit.text == text


# ---------------------------------------------------------------------------
# argument parsing — the exit-2 surface


def test_a_pair_splits_on_the_first_equals_only() -> None:
    # Arrange — a real fleet value: GIT_SSH_COMMAND carries its own `=`.
    # It is an inert string under test, not an ssh invocation — the exact bytes
    # one live spec carries, and the point is that the split does not mangle them.
    arg = "GIT_SSH_COMMAND=ssh -o ControlMaster=no"  # stx-allow: STX-HPC001
    # Act
    parsed = parse_assignment(arg)
    # Assert
    assert parsed == ("GIT_SSH_COMMAND", "ssh -o ControlMaster=no")  # stx-allow: STX-HPC001


def test_an_empty_value_is_legal_and_means_the_empty_string() -> None:
    # Arrange
    arg = "K="
    # Act
    parsed = parse_assignment(arg)
    # Assert
    assert parsed == ("K", "")


def test_an_argument_with_no_equals_is_malformed() -> None:
    # Arrange
    arg = "JUST_A_KEY"
    # Act
    outcome = _attempt(parse_assignment, arg)
    # Assert
    assert isinstance(outcome, MalformedAssignment)


def test_a_key_that_is_not_a_variable_name_is_malformed() -> None:
    # Arrange — legal YAML key, not a variable any shell can export.
    arg = "not.a.var=1"
    # Act
    outcome = _attempt(parse_assignment, arg)
    # Assert
    assert isinstance(outcome, MalformedAssignment)


def test_a_key_starting_with_a_digit_is_malformed() -> None:
    # Arrange
    key = "1BAD"
    # Act
    outcome = _attempt(validate_key, key)
    # Assert
    assert isinstance(outcome, MalformedAssignment)


def test_a_rendered_scalar_is_always_quoted() -> None:
    # Arrange — an unquoted value is where the YAML surprises live.
    value = "plain"
    # Act
    rendered = render_scalar(value)
    # Assert
    assert rendered == '"plain"'


# ---------------------------------------------------------------------------
# the --restart rule, factored out so it needs no container to test


def test_a_restart_is_requested_and_the_spec_moved_so_it_fires() -> None:
    # Arrange
    edit = EnvEdit(text="x", changed=True)
    # Act
    fires = should_restart(edit, requested=True)
    # Assert
    assert fires is True


def test_a_restart_does_not_fire_when_nothing_changed() -> None:
    # Arrange — the whole point of idempotence: a re-run must not cost the
    # agent its session.
    edit = EnvEdit(text="x", changed=False)
    # Act
    fires = should_restart(edit, requested=True)
    # Assert
    assert fires is False


def test_a_restart_does_not_fire_when_it_was_not_requested() -> None:
    # Arrange
    edit = EnvEdit(text="x", changed=True)
    # Act
    fires = should_restart(edit, requested=False)
    # Assert
    assert fires is False


def test_a_refused_edit_never_restarts() -> None:
    # Arrange
    edit = EnvEdit(text="x", changed=False, refusal="nope")
    # Act
    fires = should_restart(edit, requested=True)
    # Assert
    assert fires is False


def test_an_updated_change_carries_both_the_old_and_the_new_value() -> None:
    # Arrange
    text = _SPEC
    # Act
    change = set_env(text, [("CCT_BOT_TOKEN_SLOT", "OTHER")]).changes[0]
    # Assert
    assert (change.action, change.old, change.new) == (UPDATED, "SAC", "OTHER")
