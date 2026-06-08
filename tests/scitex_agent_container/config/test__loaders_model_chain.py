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


# ---------------------------------------------------------------------------
# v4 path — spec.model.<label>.*
# ---------------------------------------------------------------------------


def test_v4_spec_populates_model_chain(tmp_path):
    spec = _write_spec(
        tmp_path,
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
""",
    )
    cfg = load_config(spec)
    assert isinstance(cfg.model_chain, dict)
    assert list(cfg.model_chain.keys()) == ["primary", "backup"]
    assert cfg.model_chain["primary"] == ModelLabel(
        provider="anthropic",
        model_id="claude-sonnet-4-6",
        account="ywatanabe-scitex-ai",
        api_key="",
    )
    assert cfg.model_chain["backup"] == ModelLabel(
        provider="deepseek",
        model_id="deepseek-chat",
        account="",
        api_key="$DEEPSEEK_API_KEY",
    )


def test_v4_single_label_still_requires_label_key(tmp_path):
    """Operator must wrap a single config in a label (ADR-0018 §"Single-config")."""
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
    cfg = load_config(spec)
    assert list(cfg.model_chain.keys()) == ["default"]


def test_v4_insertion_order_preserved_through_loader(tmp_path):
    """Cascade fallback semantics depend on dict insertion order — verify the
    loader preserves the operator's intended order through validation.
    """
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
    cfg = load_config(spec)
    assert list(cfg.model_chain.keys()) == ["zebra", "aardvark", "middle"]


# ---------------------------------------------------------------------------
# v3 alias path — spec.claude.provider (registered name)
# ---------------------------------------------------------------------------


def test_v3_registered_provider_aliases_to_legacy_label(tmp_path):
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  claude:
    model: deepseek-chat
    provider: deepseek
""",
    )
    cfg = load_config(spec)
    # Legacy alias produces a single "legacy" label populated from the
    # v3 spec.claude block. The claude_spec ALSO populates for back-
    # compat consumers.
    assert list(cfg.model_chain.keys()) == ["legacy"]
    legacy = cfg.model_chain["legacy"]
    assert legacy.provider == "deepseek"
    assert legacy.model_id == "deepseek-chat"
    assert legacy.account == ""
    # claude block still populates (existing runtime continues to work).
    assert cfg.claude.model == "deepseek-chat"


# ---------------------------------------------------------------------------
# Empty path — neither block
# ---------------------------------------------------------------------------


def test_no_spec_claude_no_spec_model_yields_empty_chain(tmp_path):
    """When the spec declares NEITHER spec.claude NOR spec.model, the
    chain is empty — the v3 alias only fires when spec.claude exists.
    """
    spec = _write_spec(tmp_path, _BASELINE_SPEC)
    cfg = load_config(spec)
    assert cfg.model_chain == {}


def test_v3_spec_claude_model_only_aliases_to_legacy_with_empty_provider(tmp_path):
    """When spec.claude has model but no provider, the v3 alias still
    fires and produces a single 'legacy' label with the model id but an
    empty provider. The validator will reject this at strict-mode
    enforcement time (PR B); for PR A the schema is permissive — the
    parser doesn't try to invent a provider.
    """
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  claude:
    model: sonnet
""",
    )
    cfg = load_config(spec)
    assert list(cfg.model_chain.keys()) == ["legacy"]
    legacy = cfg.model_chain["legacy"]
    assert legacy.model_id == "sonnet"
    assert legacy.provider == ""  # operator declared no provider
    assert cfg.claude.model == "sonnet"


# ---------------------------------------------------------------------------
# Mutex — spec.claude + spec.model both present
# ---------------------------------------------------------------------------


def test_spec_claude_and_spec_model_both_present_rejected(tmp_path):
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  claude:
    model: claude-sonnet-4-6
  model:
    primary:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(spec)
    msg = str(excinfo.value)
    assert "spec.claude and spec.model are mutually exclusive" in msg


def test_spec_model_string_rejected_as_v2_alias(tmp_path):
    """Top-level ``spec.model: <string>`` is the deprecated v2 alias —
    the validator must reject with the v3 relocation hint pointing at
    ``spec.claude.model``."""
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model: claude-sonnet-4-6
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(spec)
    msg = str(excinfo.value)
    assert "spec.model is no longer accepted at the top level as a string" in msg


def test_spec_model_list_rejected_as_wrong_type(tmp_path):
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model:
    - foo
    - bar
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(spec)
    msg = str(excinfo.value)
    assert "spec.model must be a dict of label" in msg


# ---------------------------------------------------------------------------
# Validation through validate_model_chain — wired correctly?
# ---------------------------------------------------------------------------


def test_v4_unknown_provider_surfaces_validator_error(tmp_path):
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model:
    primary:
      provider: not-a-registered-provider
      model_id: some-model
      api_key: $SOME_KEY
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(spec)
    msg = str(excinfo.value)
    # validate_model_chain emits a "not a registered provider" error
    # that mentions the known providers; we don't tie the test to the
    # exact phrasing, just the substring that identifies the diagnostic.
    assert "not-a-registered-provider" in msg


def test_v4_api_key_and_account_both_set_rejected(tmp_path):
    spec = _write_spec(
        tmp_path,
        _BASELINE_SPEC
        + """\
  model:
    primary:
      provider: anthropic
      model_id: claude-sonnet-4-6
      account: ywatanabe-scitex-ai
      api_key: $SOME_KEY
""",
    )
    with pytest.raises(ValueError) as excinfo:
        load_config(spec)
    msg = str(excinfo.value)
    # The exact phrasing is validator-owned; just verify the mutex
    # diagnostic fires for label 'primary'.
    assert "primary" in msg
    assert "api_key" in msg or "account" in msg


def test_model_chain_typing_is_dict(tmp_path):
    """Smoke — ``ModelChain`` is a plain dict alias; runtime callers can
    iterate and index without isinstance-on-a-protocol gymnastics."""
    chain: ModelChain = {
        "alpha": ModelLabel(
            provider="anthropic",
            model_id="claude-sonnet-4-6",
            account="ywatanabe-scitex-ai",
        )
    }
    assert isinstance(chain, dict)
    assert list(chain.keys()) == ["alpha"]
