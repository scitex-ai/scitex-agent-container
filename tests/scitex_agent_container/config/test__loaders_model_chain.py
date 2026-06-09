"""Integration tests: ``load_v3`` populates ``AgentConfig.model_chain``.

Verifies the ADR-0018 PR A wiring end-to-end at the loader surface:

* v4 spec (``spec.model.<label>.*`` dict) → ``AgentConfig.model_chain``
  carries one :class:`ModelLabel` per declared label, in operator
  insertion order. ``AgentConfig.claude`` is the default-empty
  :class:`ClaudeSpec` (v4 has no ``spec.claude`` block).

* v3 spec (``spec.claude.{provider, model, account}``) →
  ``AgentConfig.model_chain`` carries a SINGLE ``"legacy"`` label
  synthesised by the parser's v3 alias path. ``AgentConfig.claude``
  ALSO populates from the same block (so existing runtime code keeps
  reading the legacy view; the chain rides alongside for PR B/C/D).

* spec with NEITHER ``spec.claude.provider`` NOR ``spec.model`` →
  empty ``model_chain``, no errors.

The validator's spec.claude/spec.model mutex check is exercised here
because :func:`load_config` runs the validator before the loader; a
spec declaring both blocks raises with the mutex message.

PA-306 fixtures: real ``tmp_path`` + real YAML loading; no
``unittest.mock.patch``.

STX-TQ007 split: every test below holds a single assertion. When a
prior test verified N facets, this file now carries N one-assert
tests sharing a small Arrange+Act helper.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config import (
    ModelChain,
    ModelLabel,
    load_config,
)

# ---------------------------------------------------------------------------
# Helpers — write a minimal v3-valid spec.yaml to ``tmp_path`` and return
# the path. Each test mutates the dict in-place before write, so the
# baseline stays in ONE place.
# ---------------------------------------------------------------------------

_BASELINE_SPEC = """\
apiVersion: scitex-agent-container/v3
kind: Agent
spec:
  runtime: apptainer
  apptainer:
    image: ~/.scitex/agent-container/containers/sac-scitex.sif
"""


def _write_spec(tmp_path, body: str) -> "pytest.Path":
    """Write ``body`` to ``<tmp_path>/<agent>/spec.yaml`` and return the path.

    The agent name comes from the parent dir (per v3 §"agent name is
    derived from the parent dir name"), so we put the file under a
    named sub-dir.
    """
    agent_dir = tmp_path / "test-agent"
    agent_dir.mkdir()
    spec = agent_dir / "spec.yaml"
    spec.write_text(body)
    return spec


def _load_or_capture_error(tmp_path, body: str) -> str:
    """Arrange + Act helper for the validator-error cases.

    Writes ``body`` as a spec and calls :func:`load_config`; expects it
    to raise :class:`ValueError`. Returns ``str(exception)`` so each
    split test can assert on a single substring without re-running the
    loader. If no exception fires, raises ``AssertionError`` so the
    test setup itself is obviously broken (we never silently return).
    """
    spec = _write_spec(tmp_path, body)
    try:
        load_config(spec)
    except ValueError as exc:
        return str(exc)
    raise AssertionError(
        "_load_or_capture_error expected ValueError from load_config but "
        "none was raised"
    )


# ---------------------------------------------------------------------------
# v4 path — spec.model.<label>.*
#
# The two-label spec below is loaded once per test (the loader is cheap
# and tmp_path is per-test). Each test pins ONE facet of the resulting
# AgentConfig.
# ---------------------------------------------------------------------------

_TWO_LABEL_V4_BODY = (
    _BASELINE_SPEC
    + """\
  model:
    primary:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
    backup:
      provider: deepseek
      model_id: deepseek-chat
      api_key: $DEEPSEEK_API_KEY
"""
)


def test_v4_spec_model_chain_is_a_dict(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _TWO_LABEL_V4_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert isinstance(cfg.model_chain, dict)


def test_v4_spec_model_chain_preserves_label_order(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _TWO_LABEL_V4_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert list(cfg.model_chain.keys()) == ["primary", "backup"]


def test_v4_spec_primary_label_carries_anthropic_account_form(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _TWO_LABEL_V4_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["primary"] == ModelLabel(
        provider="anthropic",
        model_id="claude-sonnet-4-6",
        account="ywatanabe-scitex-ai",
        api_key="",
    )


def test_v4_spec_backup_label_carries_api_key_form(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _TWO_LABEL_V4_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["backup"] == ModelLabel(
        provider="deepseek",
        model_id="deepseek-chat",
        account="",
        api_key="$DEEPSEEK_API_KEY",
    )


def test_v4_single_label_still_requires_label_key(tmp_path):
    """Operator must wrap a single config in a label (ADR-0018 §"Single-config")."""
    # Arrange
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model:
    default:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
""",
    )
    # Act
    cfg = load_config(spec)
    # Assert
    assert list(cfg.model_chain.keys()) == ["default"]


def test_v4_insertion_order_preserved_through_loader(tmp_path):
    """Cascade fallback semantics depend on dict insertion order — verify the
    loader preserves the operator's intended order through validation.
    """
    # Arrange
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model:
    zebra:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
    aardvark:
      provider: deepseek
      model_id: deepseek-chat
      api_key: $DEEPSEEK_API_KEY
    middle:
      provider: mimo
      model_id: mimo-v2.5-pro
      api_key: $XIAOMI_API_KEY
""",
    )
    # Act
    cfg = load_config(spec)
    # Assert
    assert list(cfg.model_chain.keys()) == ["zebra", "aardvark", "middle"]


# ---------------------------------------------------------------------------
# v3 alias path — spec.claude.provider (registered name).
#
# The shared spec below populates spec.claude with deepseek; the v3
# alias produces a single "legacy" label AND keeps claude_spec
# populated for back-compat consumers. Each split test verifies one
# facet.
# ---------------------------------------------------------------------------

_V3_DEEPSEEK_CLAUDE_BODY = (
    _BASELINE_SPEC
    + """\
  claude:
    model: deepseek-chat
    provider: deepseek
"""
)


def test_v3_registered_provider_aliases_to_single_legacy_label(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_DEEPSEEK_CLAUDE_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert list(cfg.model_chain.keys()) == ["legacy"]


def test_v3_legacy_label_provider_matches_spec_claude_provider(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_DEEPSEEK_CLAUDE_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["legacy"].provider == "deepseek"


def test_v3_legacy_label_model_id_matches_spec_claude_model(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_DEEPSEEK_CLAUDE_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["legacy"].model_id == "deepseek-chat"


def test_v3_legacy_label_account_defaults_to_empty(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_DEEPSEEK_CLAUDE_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["legacy"].account == ""


def test_v3_claude_block_still_populates_for_back_compat(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_DEEPSEEK_CLAUDE_BODY)
    # Act
    cfg = load_config(spec)
    # Assert — existing runtime code reading cfg.claude continues to work.
    assert cfg.claude.model == "deepseek-chat"


# ---------------------------------------------------------------------------
# Empty path — neither block
# ---------------------------------------------------------------------------


def test_no_spec_claude_no_spec_model_yields_empty_chain(tmp_path):
    """When the spec declares NEITHER spec.claude NOR spec.model, the
    chain is empty — the v3 alias only fires when spec.claude exists.
    """
    # Arrange
    spec = _write_spec(tmp_path, _BASELINE_SPEC)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain == {}


# ---------------------------------------------------------------------------
# v3 — spec.claude.model only (no provider).
#
# The v3 alias still fires and produces a single 'legacy' label with
# the model id but an empty provider. The validator will reject this at
# strict-mode enforcement time (PR B); for PR A the schema is
# permissive — the parser doesn't try to invent a provider.
# ---------------------------------------------------------------------------

_V3_CLAUDE_MODEL_ONLY_BODY = (
    _BASELINE_SPEC
    + """\
  claude:
    model: sonnet
"""
)


def test_v3_spec_claude_model_only_aliases_to_single_legacy_label(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_CLAUDE_MODEL_ONLY_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert list(cfg.model_chain.keys()) == ["legacy"]


def test_v3_spec_claude_model_only_legacy_label_carries_model_id(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_CLAUDE_MODEL_ONLY_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.model_chain["legacy"].model_id == "sonnet"


def test_v3_spec_claude_model_only_legacy_label_provider_is_empty(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_CLAUDE_MODEL_ONLY_BODY)
    # Act
    cfg = load_config(spec)
    # Assert — operator declared no provider; parser does not invent one.
    assert cfg.model_chain["legacy"].provider == ""


def test_v3_spec_claude_model_only_claude_block_still_populates(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _V3_CLAUDE_MODEL_ONLY_BODY)
    # Act
    cfg = load_config(spec)
    # Assert
    assert cfg.claude.model == "sonnet"


# ---------------------------------------------------------------------------
# Mutex — spec.claude + spec.model both present.
#
# The validator must reject. Split into (a) "the call raises" and
# (b) "the message names the mutex" so a future phrasing tweak only
# breaks one of the two tests.
# ---------------------------------------------------------------------------

_BOTH_BLOCKS_BODY = (
    _BASELINE_SPEC
    + """\
  claude:
    model: claude-sonnet-4-6
  model:
    primary:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
"""
)


def test_spec_claude_and_spec_model_both_present_raises(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _BOTH_BLOCKS_BODY)
    # Act + Assert (single `pytest.raises` block — one assertion)
    with pytest.raises(ValueError):
        load_config(spec)


def test_spec_claude_and_spec_model_both_present_error_names_mutex(tmp_path):
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _BOTH_BLOCKS_BODY)
    # Assert
    assert "spec.claude and spec.model are mutually exclusive" in msg


# ---------------------------------------------------------------------------
# Type rejections — spec.model: <string> (deprecated v2 alias).
# ---------------------------------------------------------------------------

_SPEC_MODEL_STRING_BODY = (
    _BASELINE_SPEC
    + """\
  model: claude-sonnet-4-6
"""
)


def test_spec_model_string_raises(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_MODEL_STRING_BODY)
    # Act + Assert
    with pytest.raises(ValueError):
        load_config(spec)


def test_spec_model_string_error_points_to_spec_claude_model(tmp_path):
    """Top-level ``spec.model: <string>`` is the deprecated v2 alias —
    the validator must reject with the v3 relocation hint pointing at
    ``spec.claude.model``."""
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _SPEC_MODEL_STRING_BODY)
    # Assert
    assert "spec.model is no longer accepted at the top level as a string" in msg


# ---------------------------------------------------------------------------
# Type rejections — spec.model: <list> (wrong shape).
# ---------------------------------------------------------------------------

_SPEC_MODEL_LIST_BODY = (
    _BASELINE_SPEC
    + """\
  model:
    - foo
    - bar
"""
)


def test_spec_model_list_raises(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _SPEC_MODEL_LIST_BODY)
    # Act + Assert
    with pytest.raises(ValueError):
        load_config(spec)


def test_spec_model_list_error_says_must_be_dict_of_label(tmp_path):
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _SPEC_MODEL_LIST_BODY)
    # Assert
    assert "spec.model must be a dict of label" in msg


# ---------------------------------------------------------------------------
# Validation through validate_model_chain — wired correctly?
# ---------------------------------------------------------------------------

_UNKNOWN_PROVIDER_BODY = (
    _BASELINE_SPEC
    + """\
  model:
    primary:
      provider: not-a-registered-provider
      model_id: some-model
      api_key: $SOME_KEY
"""
)


def test_v4_unknown_provider_raises(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _UNKNOWN_PROVIDER_BODY)
    # Act + Assert
    with pytest.raises(ValueError):
        load_config(spec)


def test_v4_unknown_provider_error_mentions_offending_provider_name(tmp_path):
    """``validate_model_chain`` emits a "not a registered provider" error
    that mentions the known providers; we don't tie the test to the
    exact phrasing, just the substring that identifies the diagnostic.
    """
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _UNKNOWN_PROVIDER_BODY)
    # Assert
    assert "not-a-registered-provider" in msg


# ---------------------------------------------------------------------------
# Validation — api_key + account mutex on a single label.
#
# Three facets pinned: the validator raises, the diagnostic names the
# label, and the diagnostic names the conflicting field. Split into
# three one-assert tests.
# ---------------------------------------------------------------------------

_API_KEY_AND_ACCOUNT_BOTH_BODY = (
    _BASELINE_SPEC
    + """\
  model:
    primary:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
      api_key: $SOME_KEY
"""
)


def test_v4_api_key_and_account_both_set_raises(tmp_path):
    # Arrange
    spec = _write_spec(tmp_path, _API_KEY_AND_ACCOUNT_BOTH_BODY)
    # Act + Assert
    with pytest.raises(ValueError):
        load_config(spec)


def test_v4_api_key_and_account_both_set_error_names_the_label(tmp_path):
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _API_KEY_AND_ACCOUNT_BOTH_BODY)
    # Assert — the diagnostic must identify which label is in violation.
    assert "primary" in msg


def test_v4_api_key_and_account_both_set_error_names_conflicting_field(tmp_path):
    # Arrange + Act
    msg = _load_or_capture_error(tmp_path, _API_KEY_AND_ACCOUNT_BOTH_BODY)
    # Assert — the diagnostic must name at least one of the two
    # conflicting fields (exact phrasing is validator-owned).
    assert "api_key" in msg or "account" in msg


# ---------------------------------------------------------------------------
# ModelChain typing smoke
# ---------------------------------------------------------------------------


def _build_alpha_only_chain() -> ModelChain:
    """Arrange helper: a single-label chain used by the typing smoke tests."""
    return {
        "alpha": ModelLabel(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            account="ywatanabe-scitex-ai",
        )
    }


def test_model_chain_typing_is_a_plain_dict():
    """``ModelChain`` is a plain dict alias; runtime callers can iterate
    and index without isinstance-on-a-protocol gymnastics."""
    # Arrange
    chain = _build_alpha_only_chain()
    # Act
    instance_check = isinstance(chain, dict)
    # Assert
    assert instance_check is True


def test_model_chain_typing_round_trips_inserted_label_key():
    # Arrange
    chain = _build_alpha_only_chain()
    # Act
    keys = list(chain.keys())
    # Assert
    assert keys == ["alpha"]
