"""The FOURTH harness in the registry — ``codex-sdk`` (spec.harness: codex).

Card ``sac-codex-python-sdk-harness-20260814``. The v4 descriptor
registry (PR #1041) was built so that adding a vendor is one row in a
table; codex is the first vendor added since, so these tests pin both
halves of that claim:

* the ROW resolves and carries the field values its evidence supports
  (``hosted="runner"`` / ``beat_writer="in-process"`` /
  ``can_resume=True``);
* the DERIVED sets — spec validation, the harness enum, the provider
  module's own copy — pick the new family up with no edit of their own,
  which is the actual test of the seam.

Plus the loud-refusal path specific to THIS vendor: ``codex`` is the
first name to appear on both the harness axis and the inference axis
(``spec.claude.provider: codex``), and stating both must refuse rather
than silently honour one.

Env is handled by real yield fixtures that set ``os.environ`` and
restore it on teardown — no ``monkeypatch``, per the ecosystem rule.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.config._harness_registry import (
    CLAUDE_AGENT_SDK,
    CLAUDE_CODE_TUI,
    CODEX_SDK,
    CODEX_TUI,
    HARNESS_DESCRIPTORS,
    OPENAI_AGENTS,
    UnmappableHarnessError,
    known_harnesses,
    resolve_harness_key,
    valid_runtime_spellings,
)
from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes import _apptainer_codex_env as codex_env
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError


@pytest.fixture
def codex_descriptor():
    """The registry row under test."""
    return HARNESS_DESCRIPTORS[CODEX_SDK]


@pytest.fixture
def codex_home(tmp_path):
    """A real CODEX_HOME on disk, exported for the duration of one test."""
    home = tmp_path / "codexhome"
    home.mkdir()
    previous = os.environ.get(codex_env.CODEX_HOME_ENV)
    os.environ[codex_env.CODEX_HOME_ENV] = str(home)
    yield home
    if previous is None:
        os.environ.pop(codex_env.CODEX_HOME_ENV, None)
    else:
        os.environ[codex_env.CODEX_HOME_ENV] = previous


@pytest.fixture
def no_harness_override():
    """Guarantee the ops-only $SAC_PROVIDER override is not in play."""
    previous = os.environ.pop("SAC_PROVIDER", None)
    yield
    if previous is not None:
        os.environ["SAC_PROVIDER"] = previous


@pytest.fixture
def codex_config_with_claude_provider(no_harness_override):
    """A spec stating BOTH axes — the collision codex is first to create."""
    config = AgentConfig(name="t", harness="codex")
    config.claude.provider = type(
        "P", (), {"base_url": "http://127.0.0.1:18765", "auth_token_env": "X"}
    )()
    return config


# ---------------------------------------------------------------------------
# The row itself
# ---------------------------------------------------------------------------


def test_codex_entry_is_registered_under_the_codex_family(codex_descriptor):
    # Arrange
    descriptor = codex_descriptor
    # Act
    family = descriptor.spec_harness
    # Assert
    assert family == "codex"


def test_codex_is_runner_hosted_not_external(codex_descriptor):
    # Arrange — the SDK spawns `codex app-server` as a SUBPROCESS, which
    # is exactly the fact that could mislead this field to "external".
    # The axis asks who owns the SAC-VISIBLE loop: our session runner
    # does, and the vendor process is its child (claude-agent-sdk has
    # the same shape). "external" is reserved for claude-code-tui.
    descriptor = codex_descriptor
    # Act
    hosted = descriptor.hosted
    # Assert
    assert hosted == "runner"


def test_codex_beats_are_written_in_process(codex_descriptor):
    # Arrange — a runner-hosted entry stamps its own heartbeat; only the
    # external TUI needs a host-side prober.
    descriptor = codex_descriptor
    # Act
    writer = descriptor.beat_writer
    # Assert
    assert writer == "in-process"


def test_codex_declares_resume_supported(codex_descriptor):
    # Arrange — evidence: AsyncCodex.thread_resume(thread_id, ...) is a
    # real method on the installed 0.144.4 SDK, and AsyncThread.id is
    # the id to persist. This is the FIRST runner-hosted entry whose
    # can_resume is True, so it exercises the ACCEPT side of the gate.
    descriptor = codex_descriptor
    # Act
    can_resume = descriptor.can_resume
    # Assert
    assert can_resume is True


def test_codex_runner_module_is_the_codex_session_entrypoint(codex_descriptor):
    # Arrange
    descriptor = codex_descriptor
    # Act
    module = descriptor.runner_module
    # Assert
    assert module == "scitex_agent_container._runners.codex_session"


def test_codex_does_not_claim_any_runtime_spelling(codex_descriptor):
    # Arrange — the runtime axis spells ANTHROPIC launch modes, so a
    # sole-entry family must not widen it (a claimed spelling here would
    # collide with the Claude entries and trip _check_registry).
    descriptor = codex_descriptor
    # Act
    spellings = descriptor.spec_runtimes
    # Assert
    assert spellings == frozenset()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def test_resolve_maps_the_codex_harness_to_its_key():
    # Arrange
    spec = {"harness": "codex"}
    # Act
    key = resolve_harness_key(spec)
    # Assert -- since 2026-09-05 the family's default is the pane, as for Claude.
    assert key == CODEX_TUI


def test_resolve_honours_the_legacy_provider_alias_for_codex():
    # Arrange — spec.provider is the deprecated spelling of the HARNESS
    # axis and must resolve identically for the new family too.
    spec = {"provider": "codex"}
    # Act
    key = resolve_harness_key(spec)
    # Assert
    assert key == CODEX_TUI


def test_resolve_accepts_a_loaded_config_codex_harness():
    # Arrange
    config = AgentConfig(name="t", harness="codex")
    # Act
    key = resolve_harness_key(config)
    # Assert
    assert key == CODEX_TUI


def test_resolve_refuses_the_registry_key_spelling_as_a_harness_name():
    # Arrange — the new family must not have widened the door: the KEY
    # spelling is not a spec.harness value and must still raise.
    spec = {"harness": "codex-sdk"}
    # Act
    def resolve():
        return resolve_harness_key(spec)

    # Assert
    with pytest.raises(UnmappableHarnessError):
        resolve()


def test_unknown_harness_error_lists_codex_among_the_known_families():
    # Arrange
    spec = {"harness": "nope"}
    # Act
    try:
        resolve_harness_key(spec)
        message = ""
    except UnmappableHarnessError as exc:
        message = str(exc)
    # Assert — the operator sees the pickable set, codex included.
    assert "codex" in message


# ---------------------------------------------------------------------------
# The DERIVED sets — the actual seam test
# ---------------------------------------------------------------------------


def test_known_harnesses_now_includes_codex():
    # Arrange
    expected = ("anthropic", "codex", "openai")
    # Act
    families = known_harnesses()
    # Assert
    assert families == expected


def test_spec_validation_accepts_harness_codex_with_no_edit_of_its_own():
    # Arrange — config._harness_types derives its accepted set from
    # known_harnesses(); if the seam is real, adding the row was enough.
    from scitex_agent_container.config._harness_types import is_known_harness

    # Act
    accepted = is_known_harness("codex")
    # Assert
    assert accepted


def test_provider_modules_harness_set_also_derived_the_new_family():
    # Arrange — runtimes._apptainer_provider keeps its OWN constant,
    # derived from the same helper. A hardcoded copy would fail here.
    from scitex_agent_container.runtimes._apptainer_provider import (
        _VALID_AGENT_HARNESSES,
    )

    # Act
    harnesses = _VALID_AGENT_HARNESSES
    # Assert
    assert "codex" in harnesses


def test_adding_codex_did_not_widen_the_runtime_spellings():
    # Arrange — the runtime axis is untouched by a new harness family;
    # a regression here would mean the row leaked into launch modes.
    expected = frozenset({"", "apptainer", "claude-agent-sdk", "tui"})
    # Act
    spellings = valid_runtime_spellings()
    # Assert
    assert spellings == expected


def test_the_registry_holds_exactly_the_five_known_harness_keys():
    # Arrange
    expected = sorted(
        [CLAUDE_CODE_TUI, CLAUDE_AGENT_SDK, OPENAI_AGENTS, CODEX_SDK, CODEX_TUI]
    )
    # Act
    keys = sorted(HARNESS_DESCRIPTORS)
    # Assert
    assert keys == expected


# ---------------------------------------------------------------------------
# env_and_binds: the CODEX_HOME bind and the two-axis refusal
# ---------------------------------------------------------------------------


def test_codex_env_flags_decline_quietly_for_a_non_codex_launch(
    tmp_path, no_harness_override
):
    # Arrange — every env_and_binds hook must be a no-op for launches
    # it does not serve, so a Claude launch stays byte-identical.
    config = AgentConfig(name="t", harness="anthropic")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert argv == []


def test_codex_env_flags_bind_the_codex_home_directory(
    tmp_path, codex_home, no_harness_override
):
    # Arrange — auth.json AND config.toml both live in CODEX_HOME, and
    # config.toml is what points codex at a self-hosted endpoint, so the
    # launch binds the DIRECTORY rather than injecting a key string.
    config = AgentConfig(name="t", harness="codex")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert f"{codex_home}:{codex_env.CONTAINER_CODEX_HOME}" in argv


def test_codex_env_flags_export_the_in_container_codex_home(
    tmp_path, codex_home, no_harness_override
):
    # Arrange — the container must read CODEX_HOME as the BIND TARGET,
    # not the host path, or the codex binary looks in the wrong place.
    config = AgentConfig(name="t", harness="codex")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert f"{codex_env.CODEX_HOME_ENV}={codex_env.CONTAINER_CODEX_HOME}" in argv


@pytest.fixture
def codex_routing_env():
    """Export the SAC_CODEX_* routing vars for one test, then restore."""
    values = {
        "SAC_CODEX_MODEL": "qwen36-35b-a3b",
        "SAC_CODEX_MODEL_PROVIDER": "vllm",
        "SAC_CODEX_SANDBOX": "full-access",
    }
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    yield values
    for key, was in previous.items():
        if was is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = was


def test_codex_env_flags_forward_the_model_provider_routing_var(
    tmp_path, codex_home, codex_routing_env, no_harness_override
):
    # Arrange — the model_provider names a [model_providers.*] entry in
    # config.toml, and it is the ONLY surface that points a codex agent
    # at a self-hosted endpoint. Dropping it would silently hardwire the
    # agent to codex's OpenAI-hosted default.
    config = AgentConfig(name="t", harness="codex")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert "SAC_CODEX_MODEL_PROVIDER=vllm" in argv


def test_codex_env_flags_forward_the_sandbox_routing_var(
    tmp_path, codex_home, codex_routing_env, no_harness_override
):
    # Arrange — codex sandboxes with bubblewrap, and nested bwrap fails
    # inside apptainer, so an in-container agent needs to say
    # full-access explicitly. Measured 2026-08-14 on scitex-compute-04.
    config = AgentConfig(name="t", harness="codex")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert "SAC_CODEX_SANDBOX=full-access" in argv


def test_codex_env_flags_omit_routing_vars_that_are_unset(
    tmp_path, codex_home, no_harness_override
):
    # Arrange — an unset routing var must not become an empty --env,
    # which would override codex's own default with nothing.
    config = AgentConfig(name="t", harness="codex")
    # Act
    argv = codex_env.codex_env_flags(config, tmp_path)
    # Assert
    assert not any(a.startswith("SAC_CODEX_MODEL=") for a in argv)


def test_codex_harness_refuses_to_compose_with_a_claude_provider_override(
    tmp_path, codex_config_with_claude_provider
):
    # Arrange — THE collision this harness is the first to create:
    # spec.claude.provider: codex is an INFERENCE backend (Claude Code
    # still drives), spec.harness: codex is a HARNESS (codex drives).
    config = codex_config_with_claude_provider
    # Act
    def render():
        return codex_env.codex_env_flags(config, tmp_path)

    # Assert
    with pytest.raises(ProviderEnvError):
        render()


def test_the_two_axis_refusal_message_names_the_inference_axis(
    tmp_path, codex_config_with_claude_provider
):
    # Arrange — an operator hitting this must be told WHICH two things
    # collided; a bare "invalid config" would send them hunting.
    config = codex_config_with_claude_provider
    # Act
    try:
        codex_env.codex_env_flags(config, tmp_path)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "spec.claude.provider" in message


def test_the_two_axis_refusal_message_names_the_harness_axis(
    tmp_path, codex_config_with_claude_provider
):
    # Arrange
    config = codex_config_with_claude_provider
    # Act
    try:
        codex_env.codex_env_flags(config, tmp_path)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "spec.harness: codex" in message
