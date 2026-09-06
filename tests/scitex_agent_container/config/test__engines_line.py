"""``migrate_engines_block`` — the pure spec-text edit behind the sweep.

Pure string in, value out. No mocks, no monkeypatch, no tmp files: the whole
unit is a function from spec TEXT to spec TEXT, and every fixture below is a
real v3 spec body written the way the fleet writes them — comments included,
because the comments are the thing most at risk.

Each shape here was taken from the census of the 119 tracked specs
(2026-09-06): 107 Claude-backed with a null provider, 11 with an inline
provider dict pointing at a local endpoint, 1 already carrying an engines
block, 1 on the codex harness, and the ``model: ''`` shape.

STX-NM002: no mocks. STX-TQ002 / TQ007: AAA markers, one fact per test.
"""

from __future__ import annotations

import yaml

from scitex_agent_container.config._engine_types import (
    default_engine,
    parse_engines,
    select_engine,
)
from scitex_agent_container.config._engine_validation import validate_engines
from scitex_agent_container.config._engines_line import (
    REFUSED_ALREADY_DECLARED,
    REFUSED_EMPTY_MODEL,
    REFUSED_LEGACY_HARNESS_ALIAS,
    REFUSED_NO_MODEL,
    REFUSED_PROXY,
    migrate_engines_block,
)
from scitex_agent_container.config._qwen_gateway import (
    QWEN_ENGINE_KEY,
    QWEN_GATEWAY_HOST,
    QWEN_GATEWAY_PROVIDER,
)

# The 107-spec majority shape: Claude model, no provider override. The two
# comments are load-bearing for these tests — one introduces a key INSIDE the
# claude block, one introduces the claude block ITSELF, and an insertion that
# lands between a comment and the key it explains breaks the second.
CLAUDE_SPEC = """\
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

  # PINNED 2026-08-14. Was '' (empty), which let the pool below resolve to
  # an account that then failed every turn with "Login expired".
  claude:
    # MUST match a model_name the router knows EXPLICITLY.
    model: opus[1m]
    session: null
    provider: null
    account: ''
"""

_PINNED = "  # PINNED 2026-08-14. Was '' (empty), which let the pool below resolve to"

# The 10-spec local shape: an inline provider dict at a loopback address, with
# an operator comment above it that must not move.
LOCAL_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
metadata:
  name: capsule
spec:
  host: scitex-compute-04
  runtime: claude-agent-sdk
  harness: anthropic
  claude:
    model: qwen36-35b-a3b
    session: fresh
    # 2026-08-22: the forward must be repointed when the node changes.
    provider:
      base_url: http://127.0.0.1:4000
      auth_token_env: CLEW_VLLM_TOKEN
    account: ''
"""

_FORWARD = "# 2026-08-22: the forward must be repointed when the node changes."


def _spec_of(text: str) -> dict:
    return yaml.safe_load(text)["spec"]


def _engines_of(text: str) -> dict:
    return parse_engines(_spec_of(text))


def _comments(text: str) -> "set[str]":
    return {line.strip() for line in text.splitlines() if line.strip().startswith("#")}


# ---------------------------------------------------------------------------
# Derivation — one test per shape found in the corpus
# ---------------------------------------------------------------------------


def test_a_claude_backed_spec_gains_both_a_claude_and_a_qwen_engine() -> None:
    # Arrange
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.engine_keys == ("claude", QWEN_ENGINE_KEY)


def test_the_default_engine_restates_the_specs_own_model() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["claude"].model == "opus[1m]"


def test_the_default_engine_restates_the_specs_own_harness() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["claude"].harness == "anthropic"


def test_a_spec_with_no_provider_states_anthropic_rather_than_nothing() -> None:
    # Arrange — an unstated provider silently defaults to a vendor; the
    # registry's own sentinel says the same thing out loud.
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["claude"].provider_declared == "anthropic"


def test_the_claude_entry_is_marked_default() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    chosen = default_engine(_engines_of(edit.text))
    # Assert
    assert chosen.key == "claude"


def test_a_local_provider_dict_is_restated_verbatim() -> None:
    # Arrange — repointing a spec's endpoint would be a behaviour change.
    edit = migrate_engines_block(LOCAL_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["qwen36-35b-a3b"].provider.base_url == "http://127.0.0.1:4000"


def test_a_local_provider_specs_token_env_is_restated_verbatim() -> None:
    # Arrange
    edit = migrate_engines_block(LOCAL_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines["qwen36-35b-a3b"].provider.auth_token_env == "CLEW_VLLM_TOKEN"


def test_a_local_provider_spec_is_keyed_by_its_model_not_by_claude() -> None:
    # Arrange
    text = LOCAL_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert — "claude" would be a false name for an entry holding Qwen.
    assert edit.default_key == "qwen36-35b-a3b"


def test_a_codex_harness_is_restated_on_the_default_engine() -> None:
    # Arrange — an engine entry omitting harness defaults to anthropic and
    # would then hard-error as a legacy conflict against `harness: codex`.
    text = CLAUDE_SPEC.replace("harness: anthropic", "harness: codex")
    # Act
    engines = _engines_of(migrate_engines_block(text).text)
    # Assert
    assert engines["claude"].harness == "codex"


def test_a_spec_already_running_the_gateway_model_gets_one_engine() -> None:
    # Arrange — the 8 handymen. Two entries cannot share the alternate's key.
    text = LOCAL_SPEC.replace("qwen36-35b-a3b", QWEN_ENGINE_KEY)
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert edit.engine_keys == (QWEN_ENGINE_KEY,)


# ---------------------------------------------------------------------------
# The gateway address lives in ONE place
# ---------------------------------------------------------------------------


def test_the_qwen_entry_names_the_gateway_by_provider_name() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines[QWEN_ENGINE_KEY].provider_declared == QWEN_GATEWAY_PROVIDER


def test_the_gateway_address_is_not_written_into_the_spec() -> None:
    # Arrange — the whole point: one address, not 119 copies of it.
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert QWEN_GATEWAY_HOST not in edit.text


def test_the_qwen_entry_still_resolves_to_an_endpoint() -> None:
    # Arrange — naming it by reference must not mean naming nothing.
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines[QWEN_ENGINE_KEY].provider.base_url


# ---------------------------------------------------------------------------
# Comments survive — the operator's rulings are the only record of WHY
# ---------------------------------------------------------------------------


def test_an_operator_comment_survives_the_edit() -> None:
    # Arrange — a yaml.safe_load + dump round-trip destroys this line.
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert _PINNED in edit.text


def test_every_comment_in_the_original_survives_the_edit() -> None:
    # Arrange
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert _comments(text) <= _comments(edit.text)


def test_a_comment_introducing_the_claude_block_still_precedes_it() -> None:
    # Arrange — the block must not wedge itself between a comment and its key.
    lines = migrate_engines_block(CLAUDE_SPEC).text.splitlines()
    # Act
    gap = lines.index("  claude:") - lines.index(_PINNED)
    # Assert
    assert gap == 2


def test_a_comment_inside_the_claude_block_survives_the_emptied_model() -> None:
    # Arrange
    text = CLAUDE_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert "    # MUST match a model_name the router knows EXPLICITLY." in edit.text


def test_the_comment_above_a_replaced_provider_block_survives() -> None:
    # Arrange — emptying the provider deletes LINES, the riskiest edit here.
    text = LOCAL_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert _FORWARD in edit.text


def test_every_comment_survives_a_provider_block_replacement() -> None:
    # Arrange
    text = LOCAL_SPEC
    # Act
    edit = migrate_engines_block(text)
    # Assert
    assert _comments(text) <= _comments(edit.text)


# ---------------------------------------------------------------------------
# The legacy fields the engines block supersedes
# ---------------------------------------------------------------------------


def test_the_legacy_model_is_emptied() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    spec = _spec_of(edit.text)
    # Assert
    assert spec["claude"]["model"] == ""


def test_the_legacy_model_key_is_still_present() -> None:
    # Arrange — the explicit-spec ruling requires the key to stay written.
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    spec = _spec_of(edit.text)
    # Assert
    assert "model" in spec["claude"]


def test_a_stated_legacy_provider_is_emptied() -> None:
    # Arrange
    edit = migrate_engines_block(LOCAL_SPEC)
    # Act
    spec = _spec_of(edit.text)
    # Assert
    assert spec["claude"]["provider"] is None


def test_the_legacy_provider_key_is_still_present() -> None:
    # Arrange
    edit = migrate_engines_block(LOCAL_SPEC)
    # Act
    spec = _spec_of(edit.text)
    # Assert
    assert "provider" in spec["claude"]


def test_the_legacy_harness_is_left_stated() -> None:
    # Arrange — it AGREES with the default engine, which is the case the
    # engine axis blesses; emptying it would blind every raw-spec reader.
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    spec = _spec_of(edit.text)
    # Assert
    assert spec["harness"] == "anthropic"


# ---------------------------------------------------------------------------
# The emitted block goes through the production readers
# ---------------------------------------------------------------------------


def test_validate_engines_accepts_the_emitted_block() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    errors = validate_engines(_spec_of(edit.text))
    # Assert
    assert errors == []


def test_select_engine_can_pick_the_qwen_entry() -> None:
    # Arrange
    engines = _engines_of(migrate_engines_block(CLAUDE_SPEC).text)
    # Act
    picked = select_engine(engines, QWEN_ENGINE_KEY)
    # Assert
    assert picked.model == QWEN_ENGINE_KEY


def test_the_qwen_entry_carries_the_measured_context_window() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines[QWEN_ENGINE_KEY].max_context_tokens == 1048576


def test_the_qwen_entry_runs_at_low_reasoning_effort() -> None:
    # Arrange
    edit = migrate_engines_block(CLAUDE_SPEC)
    # Act
    engines = _engines_of(edit.text)
    # Assert
    assert engines[QWEN_ENGINE_KEY].reasoning_effort == "low"


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------


def test_running_it_twice_produces_no_second_change() -> None:
    # Arrange
    once = migrate_engines_block(CLAUDE_SPEC)
    # Act
    twice = migrate_engines_block(once.text)
    # Assert
    assert twice.changed is False


def test_the_second_run_names_the_spec_as_already_declaring_engines() -> None:
    # Arrange
    once = migrate_engines_block(CLAUDE_SPEC)
    # Act
    twice = migrate_engines_block(once.text)
    # Assert
    assert twice.reason == REFUSED_ALREADY_DECLARED


def test_the_second_run_returns_the_text_byte_identical() -> None:
    # Arrange
    once = migrate_engines_block(CLAUDE_SPEC)
    # Act
    twice = migrate_engines_block(once.text)
    # Assert
    assert twice.text == once.text


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
