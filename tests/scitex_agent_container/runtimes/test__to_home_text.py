"""Tests for the to_home/ text interpolators.

Focus: ``interpolate_env`` must NEVER bake per-agent IDENTITY vars at
deploy time — they stay as literal ``${VAR}`` placeholders for RUNTIME
expansion from the agent's own env (INCIDENT 2026-07-02, card
sac-mcp-json-per-agent-identity-not-ambient-env). Non-identity vars are
still substituted from ``os.environ`` (regression guard against
over-denylisting).

Also covers the 2026-07-05 corrected design: a SYNTAX-based escape marker
``${RUNTIME:VAR}`` that ``interpolate_env`` recognizes purely by SHAPE, with
zero name-based logic — the decision of which vars are runtime-only shifts
to the template author, who opts individual ``${VAR}`` refs into
``${RUNTIME:VAR}`` in their OWN template files. The deprecated NAME-based
fallback (``_RUNTIME_ONLY_VARS`` / the ``CCT_`` prefix rule) is kept running
IN PARALLEL for now (see the module header in ``_to_home_text.py``) — both
mechanisms must keep working until downstream templates migrate.

STX-NM002: no mocks/monkeypatch — env mutated via the ``env_save_restore``
fixture (auto-reverts on teardown).
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

from scitex_agent_container.runtimes._to_home_text import interpolate_env


# interpolate_env — per-agent identity stays literal (deprecated NAME-based fallback)
def test_interpolate_env_keeps_cct_agent_id_literal_even_when_set(env_save_restore):
    # Arrange
    env_save_restore.set("CCT_AGENT_ID", "scitex-agent-container")
    # Act
    out = interpolate_env("id=${CCT_AGENT_ID}")
    # Assert
    assert out == "id=${CCT_AGENT_ID}"


def test_interpolate_env_keeps_scitex_todo_agent_id_literal_even_when_set(
    env_save_restore,
):
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "agent:scitex-agent-container")
    # Act
    out = interpolate_env("agent=${SCITEX_TODO_AGENT_ID}")
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT_ID}"


def test_interpolate_env_keeps_legacy_scitex_todo_agent_literal_even_when_set(
    env_save_restore,
):
    # Legacy pre-0.7.30 name kept as a guard: a stale deployer shell must
    # still not bake it into materialized files.
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT", "agent:scitex-agent-container")
    # Act
    out = interpolate_env("agent=${SCITEX_TODO_AGENT}")
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT}"


def test_interpolate_env_keeps_cct_bot_token_literal_even_when_set(env_save_restore):
    # Arrange
    env_save_restore.set("CCT_BOT_TOKEN", "123:wrong-token")
    # Act
    out = interpolate_env("token=${CCT_BOT_TOKEN}")
    # Assert
    assert out == "token=${CCT_BOT_TOKEN}"


# interpolate_env — non-identity vars still substitute (regression)
def test_interpolate_env_substitutes_non_identity_var(env_save_restore):
    # Arrange
    env_save_restore.set("SAC_TEST_NON_IDENTITY_VAR", "expanded-value")
    # Act
    out = interpolate_env("v=${SAC_TEST_NON_IDENTITY_VAR}")
    # Assert
    assert out == "v=expanded-value"


def test_interpolate_env_leaves_unset_non_identity_var_literal(env_save_restore):
    # Arrange
    env_save_restore.delete("SAC_TEST_DEFINITELY_UNSET_VAR")
    # Act
    out = interpolate_env("v=${SAC_TEST_DEFINITELY_UNSET_VAR}")
    # Assert
    assert out == "v=${SAC_TEST_DEFINITELY_UNSET_VAR}"


# interpolate_env — ${RUNTIME:VAR} SYNTAX-based escape marker (2026-07-05
# corrected design). Recognized by SHAPE only: works for ANY variable name,
# including names sac has never heard of, proving there is no name-based
# branching involved.
def test_interpolate_env_runtime_escape_collapses_to_plain_var_literal(
    env_save_restore,
):
    # Arrange: an arbitrary var name NOT in any hardcoded list, unset.
    env_save_restore.delete("MYPKG_ARBITRARY_IDENTITY_VAR")
    # Act
    out = interpolate_env("agent=${RUNTIME:MYPKG_ARBITRARY_IDENTITY_VAR}")
    # Assert: collapses to plain ${VAR} for the container's own runtime expansion.
    assert out == "agent=${MYPKG_ARBITRARY_IDENTITY_VAR}"


def test_interpolate_env_runtime_escape_never_substitutes_even_when_set(
    env_save_restore,
):
    # Proves shape-based recognition: this name is unknown to sac (not in
    # _RUNTIME_ONLY_VARS, no CCT_ prefix) yet the escape marker still wins
    # over substitution purely because of the ${RUNTIME:...} shape.
    # Arrange
    env_save_restore.set("MYPKG_ARBITRARY_IDENTITY_VAR", "wrong-deploy-time-value")
    # Act
    out = interpolate_env("agent=${RUNTIME:MYPKG_ARBITRARY_IDENTITY_VAR}")
    # Assert
    assert out == "agent=${MYPKG_ARBITRARY_IDENTITY_VAR}"


def test_interpolate_env_runtime_escape_works_for_hardcoded_name_too(
    env_save_restore,
):
    # The new syntax mechanism is independent of the deprecated name list —
    # it also works for a name that happens to already be hardcoded.
    # Arrange
    env_save_restore.set("SCITEX_TODO_AGENT_ID", "wrong-deploy-time-value")
    # Act
    out = interpolate_env("agent=${RUNTIME:SCITEX_TODO_AGENT_ID}")
    # Assert
    assert out == "agent=${SCITEX_TODO_AGENT_ID}"


def test_interpolate_env_plain_var_form_unaffected_by_escape_marker_support(
    env_save_restore,
):
    # A plain (non-escaped) ${VAR} for a normal, non-identity var must keep
    # substituting exactly as before — the new alternation in the regex
    # must not change existing plain-form behavior.
    # Arrange
    env_save_restore.set("SAC_TEST_NON_IDENTITY_VAR", "expanded-value")
    # Act
    out = interpolate_env("v=${SAC_TEST_NON_IDENTITY_VAR}")
    # Assert
    assert out == "v=expanded-value"
