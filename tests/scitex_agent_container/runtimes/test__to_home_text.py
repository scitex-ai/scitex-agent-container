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


def test_interpolate_env_keeps_scitex_cards_agent_id_literal_even_when_set(
    env_save_restore,
):
    # The CURRENT board identity. Its RETIRED predecessor
    # (SCITEX_TODO_AGENT_ID, above) was guarded from the start and this one
    # was not, so the canonical name was the only identity var deploy-time
    # interpolation would still have baked — and retiring the legacy key from
    # the baseline .mcp.json requires a template to reference this one.
    # Arrange
    env_save_restore.set("SCITEX_CARDS_AGENT_ID", "agent:scitex-agent-container")
    # Act
    out = interpolate_env("agent=${SCITEX_CARDS_AGENT_ID}")
    # Assert
    assert out == "agent=${SCITEX_CARDS_AGENT_ID}"


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


# interpolate_env — scitex-cards STORE IDENTITY stays literal (INCIDENT
# 2026-08-12, card sac-cards-db-store-identity-not-baked-20260812). The
# operator's shared to_home/.mcp.json template writes a plain
# ``"SCITEX_CARDS_DB": "${SCITEX_CARDS_DB}"``, and ``interpolate_env`` runs
# host-side inside ``sac agents start`` — so a launching shell that exported
# the var baked its DSN into the materialized file, and Claude Code's MCP
# ``env`` block (spread LAST over inherited env) made that baked value win
# inside the server. The agent then wrote cards to one postgres database and
# read them from another.
def test_interpolate_env_keeps_cards_db_literal_even_when_set(env_save_restore):
    # Arrange: the launching shell exports the operator's own DSN.
    env_save_restore.set(
        "SCITEX_CARDS_DB", "postgresql://scitex_cards@127.0.0.1:5442/scitex_cards"
    )
    # Act
    out = interpolate_env("db=${SCITEX_CARDS_DB}")
    # Assert: survives as a literal ref for runtime expansion from the
    # container's OWN env.
    assert out == "db=${SCITEX_CARDS_DB}"


def test_interpolate_env_keeps_cards_store_uuid_literal_even_when_set(
    env_save_restore,
):
    # The store-identity PIN: scitex-cards reads it ONLY from env (never from
    # the DB, never from a path) and it decides the ACCEPT/ADOPT/REFUSE
    # verdict, so a host-baked pin would let the launching shell declare which
    # store the agent considers legitimate.
    # Arrange
    env_save_restore.set("SCITEX_CARDS_STORE_UUID", "deploy-shell-uuid")
    # Act
    out = interpolate_env("pin=${SCITEX_CARDS_STORE_UUID}")
    # Assert
    assert out == "pin=${SCITEX_CARDS_STORE_UUID}"


#: The real ``SCITEX_CARDS_DB`` line from the operator-authored shared
#: template ``agents/_shared/to_home/.mcp.json`` (line 16), verbatim.
_CARDS_DB_TEMPLATE_LINE = '"SCITEX_CARDS_DB": "${SCITEX_CARDS_DB}",'

#: The two DSNs that were live during the incident: the operator's
#: interactive-shell export (``~/.bashrc``) and the agent's own container env.
_SHELL_A_DSN = "postgresql://scitex_cards@127.0.0.1:5442/scitex_cards"
_SHELL_B_DSN = "postgresql://scitex_cards@127.0.0.1:55432/scitex_cards"


def _materialize_from_shell(env_save_restore, dsn: str) -> str:
    """Interpolate the real template line as if launched from a shell
    exporting ``dsn`` — i.e. one ``sac agents start`` invocation."""
    env_save_restore.set("SCITEX_CARDS_DB", dsn)
    return interpolate_env(_CARDS_DB_TEMPLATE_LINE)


def test_interpolate_env_cards_db_materializes_identically_from_two_shells(
    env_save_restore,
):
    # THE INVARIANT THE INCIDENT BROKE: whichever shell the operator happened
    # to type `sac agents start` in silently decided which database that
    # agent's MCP server wrote to, and a restart from a different shell
    # silently moved the agent to a different store. Interpolating the SAME
    # template under TWO different launching-shell environments must therefore
    # produce the SAME materialized output. Before the fix this produced two
    # different databases, silently.
    # Arrange: shell A is the operator's ~/.bashrc export.
    out_shell_a = _materialize_from_shell(env_save_restore, _SHELL_A_DSN)
    # Act: the same template, materialized again from shell B.
    out_shell_b = _materialize_from_shell(env_save_restore, _SHELL_B_DSN)
    # Assert: the launching shell has no say at all.
    assert out_shell_a == out_shell_b


def test_interpolate_env_cards_db_materialized_output_leaks_no_shell_dsn(
    env_save_restore,
):
    # The companion fact: the two outputs agree because the ref stays LITERAL,
    # not because both collapsed to some other shared value. Equality with the
    # untouched template line is what proves no DSN was baked in.
    # Arrange: the launching shell exports the operator's own DSN.
    env_save_restore.set("SCITEX_CARDS_DB", _SHELL_A_DSN)
    # Act
    out_shell_a = interpolate_env(_CARDS_DB_TEMPLATE_LINE)
    # Assert
    assert out_shell_a == _CARDS_DB_TEMPLATE_LINE


def test_interpolate_env_runtime_escape_collapses_cards_db_to_plain_literal(
    env_save_restore,
):
    # The migration target: once the scitex-cards-authored template writes
    # ${RUNTIME:SCITEX_CARDS_DB}, the syntax mechanism alone must protect it —
    # which is the condition for removing the name-based entry.
    # Arrange
    env_save_restore.set(
        "SCITEX_CARDS_DB", "postgresql://scitex_cards@127.0.0.1:5442/scitex_cards"
    )
    # Act
    out = interpolate_env("db=${RUNTIME:SCITEX_CARDS_DB}")
    # Assert
    assert out == "db=${SCITEX_CARDS_DB}"


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
