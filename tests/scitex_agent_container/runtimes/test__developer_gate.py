"""The full-developer gate must read the field specs actually write.

MEASURED 2026-08-03 across all 102 fleet specs:

    labels.group   (SINGULAR — what the gate read)   ->   0 specs
    labels.groups  (PLURAL   — what specs write)     ->  86 specs

So the scalar branch had NEVER FIRED and every agent was classified by the
fallback alone: whether its role STRING sat on a four-item allowlist. When the
operator elevated scitex-hub from maintainer to "product-lead-orchestrator" on
2026-07-17, that rename moved it off the allowlist and silently out of the host
deep-merge — and switched off the provenance display that would have explained
where its hooks came from.

The load-bearing test is the FLIP case: groups=["developer"] with an
off-allowlist role must now be True. It was False before this change, so a gate
that ignored `groups` would fail it.

The second-most-important is the REFUSAL case: an explicit non-developer group
must NOT fall through to the role allowlist, or a role name would silently
re-grant what the group list deliberately withheld.

PA-307 / STX-TQ002 / STX-TQ007 — one assert per test, full AAA markers.
"""

from __future__ import annotations

from types import SimpleNamespace

from scitex_agent_container.runtimes._developer_gate import is_full_developer


def _cfg(**labels):
    """An object shaped like AgentConfig for the one attribute the gate reads."""
    return SimpleNamespace(labels=dict(labels))


def test_groups_list_containing_developer_grants():
    # Arrange — THE FLIP CASE. This is scitex-hub verbatim: the plural field
    # the specs write, plus a role that is NOT on the allowlist. False before.
    config = _cfg(groups=["developer"], role="product-lead-orchestrator")
    # Act
    result = is_full_developer(config)
    # Assert
    assert result


def test_a_non_developer_groups_list_does_not_revoke_a_qualifying_role():
    # Arrange — THE NO-REGRESSION CASE, and it pins a deliberate asymmetry.
    # My first implementation made `groups` authoritative in BOTH directions.
    # That is more principled AND it revoked the host deep-merge from SEVEN
    # live agents (claude-code-telegrammer, neurovista, neurovista-paper-writer,
    # paper-ripple-wm, sales-worker + 2 templates) which declare a
    # non-developer group while carrying an allowlisted role. Measured by an
    # old-vs-new diff over 102 specs -- my dry-run had modelled an ADDITIVE
    # rule while the code I wrote also refused, so the simulation validated a
    # different rule than the one under test.
    config = _cfg(groups=["solitary"], role="project-maintainer")
    # Act
    result = is_full_developer(config)
    # Assert
    assert result


def test_the_scalar_group_still_refuses_a_non_developer_value():
    # Arrange — the historical scalar form keeps its refusing semantics; only
    # the new plural form is additive.
    config = _cfg(group="solitary", role="project-maintainer")
    # Act
    result = is_full_developer(config)
    # Assert
    assert not result


def test_a_bare_string_groups_value_is_tolerated():
    # Arrange — a spec written with a scalar under the plural key must not
    # crash or be silently ignored (str is iterable; naive membership would
    # test characters).
    config = _cfg(groups="developer")
    # Act
    result = is_full_developer(config)
    # Assert
    assert result


def test_the_historical_scalar_group_still_grants():
    # Arrange — back-compat: a spec written the old way keeps its meaning.
    config = _cfg(group="developer")
    # Act
    result = is_full_developer(config)
    # Assert
    assert result


def test_the_role_fallback_still_applies_when_no_group_is_declared():
    # Arrange — 80 of 102 specs rely on this path today; the change must not
    # disturb them.
    config = _cfg(role="project-maintainer")
    # Act
    result = is_full_developer(config)
    # Assert
    assert result


def test_an_off_allowlist_role_with_no_group_still_refuses():
    # Arrange — the pre-existing behaviour for undeclared agents is unchanged;
    # this fix grants only where a group was actually declared.
    config = _cfg(role="product-lead-orchestrator")
    # Act
    result = is_full_developer(config)
    # Assert
    assert not result
