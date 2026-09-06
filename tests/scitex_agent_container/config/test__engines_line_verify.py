"""The provider block's SHAPE, and the self-check that gates the edit.

Split from ``test__engines_line`` on the module line budget. Same unit, same
no-mocks rule; these are the cases where the edit must either preserve
something exactly or REFUSE, and mutation testing found every one of the
gating mechanisms untested:

* the provider block is copied VERBATIM, which has to be true of nesting and
  of blank lines, not only of the two keys anyone has written so far;
* a comment glued to the previous block is that block's trailing note, and
  the comment-count guard cannot see one that MOVED;
* ``REFUSED_VERIFY_FAILED`` was produced by no test, so ``_verify`` could be
  deleted outright with the suite still green; likewise ``_backend_drift``
  replaced by ``return ""`` and the comment-loss detector by ``return []``.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.config._engine_types import parse_engines
from scitex_agent_container.config._engines_line import (
    REFUSED_EMPTY_MODEL,
    REFUSED_LEGACY_HARNESS_ALIAS,
    REFUSED_NO_MODEL,
    REFUSED_PROXY,
    REFUSED_VERIFY_FAILED,
    _backend_drift,
    lost_comment_lines,
    migrate_engines_block,
)
from tests.scitex_agent_container.config.test__engines_line import (
    _PINNED,
    CLAUDE_SPEC,
    LOCAL_SPEC,
)


def _spec_of(text: str) -> dict:
    return yaml.safe_load(text)["spec"]


def _engines_of(text: str) -> dict:
    return parse_engines(_spec_of(text))


# ---------------------------------------------------------------------------
# Refusals — named and loud, never a silent skip
# ---------------------------------------------------------------------------


def test_a_spec_stating_no_model_is_refused() -> None:
    # Arrange — 1 of 119 today, and the shape every fixture default has.
    text = CLAUDE_SPEC.replace("model: opus[1m]", "model: ''")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason == REFUSED_EMPTY_MODEL


def test_a_spec_stating_no_model_comes_back_byte_identical() -> None:
    # Arrange
    text = CLAUDE_SPEC.replace("model: opus[1m]", "model: ''")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.text == text


def test_a_spec_with_no_model_line_at_all_is_refused() -> None:
    # Arrange
    text = CLAUDE_SPEC.replace("    model: opus[1m]\n", "")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason == REFUSED_NO_MODEL


def test_the_deprecated_harness_alias_is_refused_rather_than_guessed() -> None:
    # Arrange — spec.provider is the retired spelling of spec.harness.
    text = CLAUDE_SPEC.replace("  harness: anthropic", "  provider: anthropic")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason == REFUSED_LEGACY_HARNESS_ALIAS


def test_a_proxy_spec_is_refused_because_engines_are_forbidden_there() -> None:
    # Arrange
    text = CLAUDE_SPEC.replace("kind: Agent", "kind: AgentProxy")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason == REFUSED_PROXY


def test_an_unparsable_spec_is_refused_and_not_raised() -> None:
    # Arrange
    text = "spec:\n  claude:\n   - [unbalanced\n"
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.changed is False


def test_a_refusal_always_carries_a_reason() -> None:
    # Arrange — reason is None exactly when changed is True.
    text = "not a spec at all\n"
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason


def test_a_successful_edit_carries_no_reason() -> None:
    # Arrange
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason is None


# ---------------------------------------------------------------------------
# The provider block is restated VERBATIM — including its shape
# ---------------------------------------------------------------------------

# A provider carrying a nested mapping. No spec in the 2026-09-06 corpus has
# one (surveyed: 11 dict providers, all flat 2-key), so this is the shape the
# edit must not silently destroy rather than one it meets today.
NESTED_PROVIDER_SPEC = LOCAL_SPEC.replace(
    "      base_url: http://127.0.0.1:4000\n",
    "      base_url: http://127.0.0.1:4000\n"
    "      extra_headers:\n"
    "        X-Route: spartan\n"
    "        X-Tier: gold\n",
)

BLANK_IN_PROVIDER_SPEC = LOCAL_SPEC.replace(
    "      base_url: http://127.0.0.1:4000\n",
    "      base_url: http://127.0.0.1:4000\n\n",
)

# A comment GLUED to the block above it — its trailing note, not an
# introduction to `claude:`. Hoisting it below a 20-line engines block makes
# it a note about the wrong key, and `_comment_counts` compares multisets so
# a comment that MOVED is invisible to it.
TRAILING_COMMENT_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  name: alpha
spec:
  host: scitex-compute-01
  runtime: tui
  harness: anthropic
  apptainer:
    image: base.sif
    writable: false
  # ^ the overlay above is per-agent; never share one (2026-05 ruling)
  claude:
    model: opus[1m]
    session: null
    provider: null
    account: ''
"""


def test_a_nested_provider_mapping_is_not_flattened() -> None:
    # Arrange — stripping every child re-emitted them at ONE indent, so
    # `extra_headers` became null and its two keys became its siblings.
    edit = migrate_engines_block(NESTED_PROVIDER_SPEC)
    # Act
    engines = yaml.safe_load(edit.text)["spec"]["engines"]
    # Assert
    assert engines["qwen36-35b-a3b"]["provider"]["extra_headers"] == {
        "X-Route": "spartan",
        "X-Tier": "gold",
    }


def test_a_nested_provider_keeps_its_endpoint() -> None:
    # Arrange
    edit = migrate_engines_block(NESTED_PROVIDER_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["qwen36-35b-a3b"].provider.base_url == "http://127.0.0.1:4000"


def test_a_blank_line_inside_the_provider_block_survives() -> None:
    # Arrange — "restated verbatim" has to be true of the whitespace too.
    edit = migrate_engines_block(BLANK_IN_PROVIDER_SPEC)
    # Act
    text = edit.text
    # Assert
    assert (
        "        base_url: http://127.0.0.1:4000\n\n        auth_token_env:" in text
    )


def test_a_trailing_comment_is_not_reparented_under_the_engines_block() -> None:
    # Arrange
    edit = migrate_engines_block(TRAILING_COMMENT_SPEC)
    # Act
    text = edit.text
    # Assert — it still sits directly under the block it annotates.
    assert (
        "    writable: false\n  # ^ the overlay above is per-agent" in text
    )


def test_a_comment_introducing_the_claude_block_still_stays_with_it() -> None:
    # Arrange — the other half of the same rule: a comment SEPARATED from
    # what precedes it introduces what follows, and must not be left behind.
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    text = edit.text
    # Assert
    assert (
        f'{_PINNED}\n  # an account that then failed every turn with "Login '
        f'expired".\n  claude:' in text
    )


# ---------------------------------------------------------------------------
# The self-check — the mechanisms that decide whether the edit is written
# ---------------------------------------------------------------------------


def test_an_unregistered_provider_name_is_refused_by_the_verification() -> None:
    # Arrange — REFUSED_VERIFY_FAILED was produced by no test at all, so
    # `_verify` could be deleted outright with the suite still green.
    text = CLAUDE_SPEC.replace("provider: null", "provider: ''")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.reason == REFUSED_VERIFY_FAILED


def test_a_verification_refusal_writes_nothing() -> None:
    # Arrange
    text = CLAUDE_SPEC.replace("provider: null", "provider: ''")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.text == text


def test_a_verification_refusal_names_what_failed() -> None:
    # Arrange
    text = CLAUDE_SPEC.replace("provider: null", "provider: ''")
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.detail


def test_lost_comment_lines_reports_a_deleted_comment() -> None:
    # Arrange — the guard that makes "comments survive" a per-spec check
    # rather than an argument made once in a docstring.
    before = "# PINNED 2026-08-14\nmodel: opus\n"
    after = "model: opus\n"
    # Act
    lost = lost_comment_lines(before, after)
    # Assert
    assert lost == ["# PINNED 2026-08-14"]


def test_lost_comment_lines_reports_nothing_when_all_survive() -> None:
    # Arrange
    before = "# PINNED\nmodel: opus\n"
    after = "engines:\n# PINNED\nmodel: ''\n"
    # Act
    lost = lost_comment_lines(before, after)
    # Assert
    assert lost == []


def test_lost_comment_lines_counts_duplicates() -> None:
    # Arrange — a multiset, so losing one of two identical comments counts.
    before = "# note\n# note\n"
    after = "# note\n"
    # Act
    lost = lost_comment_lines(before, after)
    # Assert
    assert lost == ["# note"]


def test_backend_drift_reports_a_changed_model() -> None:
    # Arrange — the one property that makes the sweep safe over 119 files,
    # and it survived being replaced by `return ""`.
    engine = _engines_of(migrate_engines_block(CLAUDE_SPEC).text)["claude"]
    old_spec = _spec_of(CLAUDE_SPEC)
    old_spec["claude"]["model"] = "haiku"
    # Act
    drift = _backend_drift(old_spec, engine)
    # Assert
    assert "model would change" in drift


def test_backend_drift_reports_a_changed_harness() -> None:
    # Arrange
    engine = _engines_of(migrate_engines_block(CLAUDE_SPEC).text)["claude"]
    old_spec = _spec_of(CLAUDE_SPEC)
    old_spec["harness"] = "codex"
    # Act
    drift = _backend_drift(old_spec, engine)
    # Assert
    assert "harness would change" in drift


def test_backend_drift_reports_a_changed_provider() -> None:
    # Arrange
    engine = _engines_of(migrate_engines_block(CLAUDE_SPEC).text)["claude"]
    old_spec = _spec_of(CLAUDE_SPEC)
    old_spec["claude"]["provider"] = {
        "base_url": "http://127.0.0.1:9",
        "auth_token_env": "TOKEN",
    }
    # Act
    drift = _backend_drift(old_spec, engine)
    # Assert
    assert "provider would change" in drift


def test_backend_drift_is_silent_when_the_backend_is_restated() -> None:
    # Arrange — the positive control: a check that always fires is no check.
    engine = _engines_of(migrate_engines_block(CLAUDE_SPEC).text)["claude"]
    # Act
    drift = _backend_drift(_spec_of(CLAUDE_SPEC), engine)
    # Assert
    assert drift == ""
