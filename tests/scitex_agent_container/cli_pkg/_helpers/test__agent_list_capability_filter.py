"""``--capability handyman`` must find the handymen.

WHY THIS FILE EXISTS. On 2026-08-18 the operator told the fleet to route work
to the qwen handymen aggressively, because they cost no Anthropic quota. Within
the hour TWO agents independently ran ``agent_list(capability="handyman")``,
got ZERO while eight handymen were running and registered, and both came close
to reporting "no handymen available" — which would have sent every delegating
agent back to spending the quota the instruction exists to save.

The filter was correct about the field it read and wrong about the question it
was being asked. Those agents carry::

    purpose:      general-handyman
    capabilities: edit, refactor, read, test, investigate, cleanup

"handyman" lives in ``purpose``; ``capabilities`` holds verbs.

A DISCOVERY SURFACE THAT SILENTLY ANSWERS "NONE" IS WORSE THAN ONE THAT ERRORS,
because nobody investigates a zero. That is the same empty-vs-absent shape as
the hosted-runner guard fixed in #1116 (unresolvable rendered as unhosted) and
as the cross-host router reporting "not in registry" for a live agent whose
port was never propagated. Three instances in one surface in one night, which
is why this is pinned by a test rather than left to a comment.

Both directions are asserted, because a filter that matches everything is as
useless as one that matches nothing — and the widening fix is the tempting one.
"""

from __future__ import annotations

from scitex_agent_container.cli_pkg._helpers._agent_list import (
    _label_capability_matches,
)

#: The real label shape of handyman-c03-01..08, copied from a live spec rather
#: than paraphrased — a paraphrase would test the paraphrase.
HANDYMAN_LABELS = {
    "role": "project-maintainer",
    "purpose": "general-handyman",
    "capabilities": "edit, refactor, read, test, investigate, cleanup",
}


def test_the_word_people_search_for_finds_the_handymen() -> None:
    # Arrange: the exact query two agents ran and got zero from.
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "handyman")
    # Assert
    assert found is True


def test_a_real_capability_verb_still_matches() -> None:
    # Arrange: the pre-existing behaviour must survive the widening.
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "refactor")
    # Assert
    assert found is True


def test_an_unrelated_word_still_does_not_match() -> None:
    # Arrange: the both-directions half. A filter that matches everything is
    # as useless as one that matches nothing, and widening is the tempting fix.
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "deploy")
    # Assert
    assert found is False


def test_capabilities_match_whole_tokens_not_substrings() -> None:
    # Arrange: `read` is a real capability; `rea` must NOT match it. Substring
    # matching on the token list would make `read` match `spread` and quietly
    # widen every query that already works.
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "rea")
    # Assert
    assert found is False


def test_role_is_searchable_too() -> None:
    # Arrange: role carries the other phrase people search by.
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "maintainer")
    # Assert
    assert found is True


def test_an_agent_with_no_labels_matches_nothing() -> None:
    # Arrange: absent fields must be a clean False, not a crash — the filter
    # runs over every agent on disk, including ones with sparse specs.
    labels: dict = {}
    # Act
    found = _label_capability_matches(labels, "handyman")
    # Assert
    assert found is False


def test_an_empty_query_matches_nothing() -> None:
    # Arrange: guards the caller's `if capability and not ...` shape — an empty
    # string must never be treated as "match everything".
    labels = HANDYMAN_LABELS
    # Act
    found = _label_capability_matches(labels, "   ")
    # Assert
    assert found is False
