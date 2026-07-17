"""Identity comes from the AGENT, never from the directory it stands in.

A directory names a PROJECT, never an agent; the second agent in a repo must be
a second AGENT, not the first one twice.

Incident 2026-07-17: identity was derived from the workdir basename, so one repo
= one identity = one agent, structurally. Three scitex-cards UI agents working in
~/proj/scitex-todo took the scitex-todo steward's identity and its bot. sac's own
`_default_agent_id` did the same thing deliberately ("matching the per-project
.envrc convention"), so the theft did not even need the .envrc.

AAA; each name states the behaviour under test.
"""

from scitex_agent_container.runtimes._cct_token_pool import (
    _default_agent_id,
    _slot_candidates,
)

_SHARED_REPO = "/home/ywatanabe/proj/scitex-todo"


def test_two_agents_in_one_repo_get_different_slots():
    # Arrange: the exact shape of the incident -- siblings sharing a workdir.
    # Act
    chat = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    gui = _slot_candidates("scitex-cards-gui", _SHARED_REPO)
    # Assert: the whole point. Under the old workdir-first rule both returned
    # TODO first and the second agent became the first.
    assert chat[0] != gui[0]


def test_two_agents_in_one_repo_get_different_identities():
    # Arrange
    # Act
    chat = _default_agent_id("scitex-cards-chat", _SHARED_REPO)
    gui = _default_agent_id("scitex-cards-gui", _SHARED_REPO)
    # Assert: the silent half. A stolen slot 409s loudly; a stolen identity just
    # writes under someone else's name.
    assert chat != gui


def test_an_agents_identity_is_its_own_name_even_inside_another_agents_repo():
    # Arrange: the steward of this repo is 'scitex-todo'; the worker is not.
    # Act
    identity = _default_agent_id("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert identity == "scitex-cards-chat"


def test_slot_never_includes_the_short_slot_of_the_project_worked_in():
    # Arrange: CCT_BOT_TOKEN_TODO exists in the live pool and CARDS does not, so
    # a workdir-first rule hands this agent the steward's REGISTERED bot.
    # Act
    candidates = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert "TODO" not in candidates


def test_slot_never_includes_the_long_slot_of_the_project_worked_in():
    # Arrange
    # Act
    candidates = _slot_candidates("scitex-cards-chat", _SHARED_REPO)
    # Assert
    assert "SCITEX_TODO" not in candidates


def test_agent_whose_name_matches_its_project_is_unaffected():
    # Arrange: 9 of 12 live agents look like this -- the rules agree, and the
    # fix must be a no-op for them.
    # Act
    candidates = _slot_candidates("scitex-dev", "/home/ywatanabe/proj/scitex-dev")
    # Assert
    assert candidates == ["SCITEX_DEV", "DEV"]


def test_scitex_prefix_still_yields_the_short_pool_slot():
    # Arrange: the pool names core packages by short slot (TODO, DEV, ...).
    # Act
    candidates = _slot_candidates("scitex-storage", "/anywhere/at/all")
    # Assert: no regression in the stripping behaviour the pool depends on.
    assert candidates == ["SCITEX_STORAGE", "STORAGE"]


def test_workdir_cannot_influence_the_slot_at_all():
    # Arrange: same agent, wildly different locations.
    # Act
    a = _slot_candidates("grant", "/home/ywatanabe/proj/grant")
    b = _slot_candidates("grant", "/home/ywatanabe/proj/scitex-todo")
    c = _slot_candidates("grant", "")
    # Assert: location is not an input to identity. Full stop.
    assert a == b == c
