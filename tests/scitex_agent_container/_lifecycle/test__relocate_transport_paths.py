#!/usr/bin/env python3
"""A transcript under the wrong directory name is present, intact, and invisible.

The encoding assertions here are not guesses at Claude Code's rules — they are
pinned against directory names MEASURED on this fleet's own disk (2026-08-11,
``~/.claude/projects``)::

    /home/ywatanabe/proj/scitex-agent-container
        -> -home-ywatanabe-proj-scitex-agent-container
    /home/ywatanabe/proj/scitex-agent-container/.worktrees/fix-accounts-...
        -> -home-ywatanabe-proj-scitex-agent-container--worktrees-fix-accounts-...
    /home/ywatanabe/.dotfiles/src/.bash.d
        -> -home-ywatanabe--dotfiles-src--bash-d

Those three cover the rules that matter: ``/`` and ``.`` both become ``-``,
hyphens already in a path segment survive, and a hidden directory produces the
doubled dash rather than a tripled one.

The load-bearing test in this file is the LAST one: resolving the workdir locally
would resolve it against the SOURCE's filesystem, so an unobserved target workdir
must be UNKNOWN rather than a confident local guess.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._lifecycle._relocate_transport_paths import (
    CODE_DERIVED,
    CODE_UNKNOWN,
    TargetTranscriptDir,
    derive_target_dir,
    encode_workdir,
)

HOME = "/home/agent"


def _derive(**over):
    kwargs = {
        "target_home": HOME,
        "target_resolved_workdir": "/home/ywatanabe/proj/lead",
        "source_dir_name": None,
    }
    kwargs.update(over)
    return derive_target_dir(**kwargs)


def test_a_plain_path_encodes_with_leading_dash() -> None:
    # Arrange: measured on disk — the leading slash produces a leading dash, so
    # the name is not simply the path with separators swapped.
    # Act
    encoded = encode_workdir("/home/ywatanabe/proj/scitex-agent-container")
    # Assert
    assert encoded == "-home-ywatanabe-proj-scitex-agent-container"


def test_a_hidden_directory_produces_a_doubled_dash() -> None:
    # Arrange: measured on disk. `/.worktrees` would naively give three dashes;
    # Claude Code collapses them, and a transcript written to the naive name is
    # invisible.
    # Act
    encoded = encode_workdir("/home/y/proj/repo/.worktrees/topic")
    # Assert
    assert encoded == "-home-y-proj-repo--worktrees-topic"


def test_dots_inside_a_segment_also_become_dashes() -> None:
    # Arrange: measured on disk (`/home/ywatanabe/.dotfiles/src/.bash.d`) — the
    # rule is about the character, not about the leading position.
    # Act
    encoded = encode_workdir("/home/ywatanabe/.dotfiles/src/.bash.d")
    # Assert
    assert encoded == "-home-ywatanabe--dotfiles-src--bash-d"


def test_hyphens_already_in_the_path_survive() -> None:
    # Arrange: the fleet's repos are hyphenated, so this is every real path.
    # Act
    encoded = encode_workdir("/home/y/proj/scitex-agent-container")
    # Assert
    assert encoded == "-home-y-proj-scitex-agent-container"


def test_the_destination_hangs_off_the_targets_home() -> None:
    # Arrange: the store lives under $HOME; using this host's home would name a
    # directory on the wrong machine.
    # Act
    derived = _derive()
    # Assert
    assert derived.path == f"{HOME}/.claude/projects/-home-ywatanabe-proj-lead"


def test_a_successful_derivation_carries_the_derived_code() -> None:
    # Arrange: callers branch on the code.
    # Act
    derived = _derive()
    # Assert
    assert derived.code == CODE_DERIVED


def test_a_trailing_slash_on_the_home_does_not_double_up() -> None:
    # Arrange: a probe that reports `$HOME` with a trailing slash must not yield
    # a path with `//`, which some remote tools treat as a different path.
    # Act
    derived = _derive(target_home="/home/agent/")
    # Assert
    assert "//" not in derived.path


def test_a_matching_encoding_is_reported_as_matching() -> None:
    # Arrange: the common case — both hosts resolve the workdir identically.
    # Act
    derived = _derive(source_dir_name="-home-ywatanabe-proj-lead")
    # Assert
    assert derived.matches_source is True


def test_a_differing_encoding_is_reported_rather_than_refused() -> None:
    # Arrange: a mismatch is the NORMAL, correct outcome when the two hosts
    # resolve the workdir differently — and it is the whole reason the name is
    # recomputed. It must be surfaced, not treated as an error.
    # Act
    derived = _derive(source_dir_name="-mnt-c-elsewhere")
    # Assert
    assert derived.matches_source is False


def test_a_differing_encoding_still_derives_a_usable_path() -> None:
    # Arrange: refusing here would block exactly the relocation this module was
    # written to make correct.
    # Act
    derived = _derive(source_dir_name="-mnt-c-elsewhere")
    # Assert
    assert derived.path == f"{HOME}/.claude/projects/-home-ywatanabe-proj-lead"


def test_the_mismatch_reason_names_both_directory_names() -> None:
    # Arrange: a reader seeing "the names differ" needs to know which is which
    # to judge whether the target's workdir is what the spec claims.
    # Act
    derived = _derive(source_dir_name="-mnt-c-elsewhere")
    # Assert
    assert "-mnt-c-elsewhere" in derived.reason and "-home-ywatanabe-proj-lead" in (
        derived.reason
    )


def test_an_unasked_comparison_stays_unknown_rather_than_defaulting_to_same() -> None:
    # Arrange: defaulting to "same" would be a claim nobody made, and it is the
    # claim that is false in exactly the case this module guards.
    # Act
    derived = _derive(source_dir_name=None)
    # Assert
    assert derived.matches_source is None


def test_an_unobserved_target_home_refuses() -> None:
    # Arrange: this host's $HOME is not evidence about the other host's.
    # Act
    derived = _derive(target_home=None)
    # Assert
    assert derived.code == CODE_UNKNOWN


def test_an_unobserved_target_workdir_refuses_rather_than_resolving_locally() -> None:
    # Arrange: THE test. Resolving the workdir in this process resolves it
    # against the SOURCE's filesystem, so a local answer would be confidently
    # wrong in precisely the case that matters — and the resulting transcript is
    # readable, byte-identical, and invisible to the target's runner.
    # Act
    derived = _derive(target_resolved_workdir=None)
    # Assert
    assert derived.path is None


def test_the_unobserved_workdir_hint_names_the_measurement_to_take() -> None:
    # Arrange: an UNKNOWN calls for "go and measure it", so it must say what to
    # measure — the RESOLVED workdir, not the declared one.
    # Act
    derived = _derive(target_resolved_workdir=None)
    # Assert
    assert "realpath" in derived.hint or "readlink" in derived.hint


def test_a_failed_derivation_must_say_what_to_do_next() -> None:
    # Arrange: the invariant lives in the type — an unhelpful UNKNOWN is
    # unrepresentable rather than merely discouraged.
    # Act
    build = lambda: TargetTranscriptDir(  # noqa: E731
        path=None, code=CODE_UNKNOWN, reason="nope", hint=""
    )
    # Assert
    with pytest.raises(ValueError):
        build()


def test_the_derivation_defines_no_bool() -> None:
    # Arrange: `if target_dir:` on an underived one would read as "we know where
    # to put it". Pinned so a future convenience has to argue with a test.
    # Act
    defined = "__bool__" in vars(TargetTranscriptDir)
    # Assert
    assert defined is False
