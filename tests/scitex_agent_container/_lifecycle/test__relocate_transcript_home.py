"""The host-side directory that backs a containerised agent's ``~/.claude``.

The failure being pinned is the one measured on this fleet on 2026-08-09: a
transcript that landed in the overlay's upper layer while the runtime tree — the
one that LOOKS like it should hold it — held only the boot seed. Carrying the
wrong tree relocates an agent's config and leaves its conversation behind.
"""

from __future__ import annotations

from scitex_agent_container._lifecycle._relocate_transcript_home import (
    CODE_FROM_BIND,
    CODE_FROM_OVERLAY,
    CODE_UNCONTAINED,
    CODE_UNKNOWN,
    transcript_home_from_spec,
)

LEAF_BIND = {
    "spec": {
        "runtime": "tui",
        "apptainer": {
            "binds": [
                "/host/runtime/a/home/.claude/projects:/home/agent/.claude/projects:rw",
                "/home/ywatanabe:/home/ywatanabe:rw",
            ],
            "raw_args": ["--overlay", "/host/overlays/a/"],
        },
    }
}

OVERLAY_ONLY = {
    "spec": {
        "runtime": "tui",
        "apptainer": {
            "binds": ["/home/ywatanabe:/home/ywatanabe:rw"],
            "raw_args": ["--home", "/home/agent", "--overlay", "/host/overlays/a/"],
        },
    }
}


def test_a_leaf_projects_bind_yields_the_container_home() -> None:
    # Arrange: the shape that demonstrably works — bind the projects directory
    # itself, because a bind OVER /home/agent loses to apptainer's --home.
    # Act
    answer = transcript_home_from_spec(LEAF_BIND)
    # Assert
    assert answer.path == "/host/runtime/a/home"


def test_a_leaf_projects_bind_is_reported_as_coming_from_a_bind() -> None:
    # Arrange: provenance matters — the same path could have been derived two
    # ways, and which one it was decides whether it is still true tomorrow.
    # Act
    answer = transcript_home_from_spec(LEAF_BIND)
    # Assert
    assert answer.code == CODE_FROM_BIND


def test_a_bind_wins_over_the_overlay_when_both_are_present() -> None:
    # Arrange: LEAF_BIND carries BOTH a projects bind and an --overlay. The bind
    # is where the writes actually go; preferring the overlay would name a
    # directory the running agent does not write to.
    # Act
    answer = transcript_home_from_spec(LEAF_BIND)
    # Assert
    assert "overlays" not in (answer.path or "")


def test_no_bind_falls_through_to_the_overlay_upper_layer() -> None:
    # Arrange: the fleet's default shape, and the one that surprised everybody
    # on 2026-08-09.
    # Act
    answer = transcript_home_from_spec(OVERLAY_ONLY)
    # Assert
    assert answer.path == "/host/overlays/a/upper/home/agent"


def test_the_overlay_answer_is_labelled_as_such() -> None:
    # Arrange: same reason as the bind case — the caller may want to know which
    # mechanism it is relying on.
    # Act
    answer = transcript_home_from_spec(OVERLAY_ONLY)
    # Assert
    assert answer.code == CODE_FROM_OVERLAY


def test_a_last_bind_over_the_same_container_path_wins() -> None:
    # Arrange: apptainer applies binds in order, so a later one shadows an
    # earlier one. Taking the FIRST match would name a directory that is mounted
    # over and never written to.
    spec = {
        "spec": {
            "apptainer": {
                "binds": [
                    "/first/home/.claude/projects:/home/agent/.claude/projects:rw",
                    "/second/home/.claude/projects:/home/agent/.claude/projects:rw",
                ]
            }
        }
    }
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.path == "/second/home"


def test_a_bind_whose_halves_disagree_is_refused_rather_than_trimmed() -> None:
    # Arrange: host side does not end in the suffix the container side implies.
    # Silently trimming something else off would name a real directory that is
    # the wrong one, which is worse than refusing.
    spec = {
        "spec": {
            "apptainer": {"binds": ["/somewhere/else:/home/agent/.claude/projects:rw"]}
        }
    }
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.path is None


def test_a_mismatched_bind_carries_the_unknown_code() -> None:
    # Arrange: the discipline the rest of the relocate machinery uses — an
    # unresolved value is absent and carries CODE_UNKNOWN, never a guess.
    spec = {
        "spec": {
            "apptainer": {"binds": ["/somewhere/else:/home/agent/.claude/projects:rw"]}
        }
    }
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.code == CODE_UNKNOWN


def test_an_uncontained_agent_uses_the_observed_ssh_home() -> None:
    # Arrange: no container at all, so the host's own $HOME IS the answer — but
    # only because it was OBSERVED and passed in.
    spec = {"spec": {"runtime": "none"}}
    # Act
    answer = transcript_home_from_spec(spec, ssh_home="/home/ywatanabe")
    # Assert
    assert answer.code == CODE_UNCONTAINED


def test_an_uncontained_agent_with_no_observed_home_refuses() -> None:
    # Arrange: this host's $HOME is not evidence about that host's, so an
    # unobserved one must not be substituted.
    spec = {"spec": {"runtime": "none"}}
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.path is None


def test_a_containerised_spec_with_neither_mechanism_refuses() -> None:
    # Arrange: no bind covering the home and no --overlay means nobody can say
    # where the conversation is durably written. A guess here produces an agent
    # that starts, reports healthy, and has no memory.
    spec = {"spec": {"runtime": "tui", "apptainer": {"binds": ["/a:/b:rw"]}}}
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.path is None


def test_an_unresolved_home_says_what_to_measure_next() -> None:
    # Arrange: every refusal in this feature has to be actionable.
    spec = {"spec": {"runtime": "tui", "apptainer": {"binds": ["/a:/b:rw"]}}}
    # Act
    answer = transcript_home_from_spec(spec)
    # Assert
    assert answer.hint
