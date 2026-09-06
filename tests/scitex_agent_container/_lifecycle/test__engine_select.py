"""START-TIME engine selection — refusal, the three-valued verdict, parameters.

Drives the REAL ``select_engine_at_start`` against REAL ``AgentConfig``
objects produced by the REAL ``load_config`` from a REAL spec.yaml, and
the REAL ``auth_argv`` for the launch-path half. Nothing under test is
mocked; the only thing injected is the host ENVIRONMENT (which env var
is exported), because that IS the input the refusal reads.

POSITIVE CONTROLS everywhere a refusal is asserted: the same engine with
its auth env var EXPORTED must start clean. Without that pairing,
"raises" would pass just as well against an implementation that refuses
unconditionally.
"""

from __future__ import annotations

import os

import pytest
import yaml

from scitex_agent_container._lifecycle._engine_select import (
    EngineNotHonourableError,
    select_engine_at_start,
)
from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_honour import (
    VERDICT_HONOURABLE,
    VERDICT_NOT_HONOURABLE,
    VERDICT_UNKNOWN,
    probe_verdict,
    static_verdict,
)
from scitex_agent_container.config._engine_types import (
    UnknownEngineError,
    parse_engines,
)
from scitex_agent_container.config._explicit_validation import (
    explicit_spec_defaults,
)
from scitex_agent_container.runtimes._apptainer_auth import auth_argv

_TOKEN_ENV = "SAC_TEST_QWEN_KEY"


def _engines() -> dict:
    return {
        "claude": {"harness": "anthropic", "model": "fable[1m]", "default": True},
        "qwen38-27b": {
            "harness": "anthropic",
            "model": "qwen38-27b",
            "provider": {
                "base_url": "http://127.0.0.1:18772",
                "auth_token_env": _TOKEN_ENV,
            },
            "reasoning_effort": "low",
            "max_context_tokens": 393216,
        },
    }


def _write(tmp_path, name: str, engines: dict | None = None) -> str:
    spec = explicit_spec_defaults("Agent")
    spec["host"] = "${HOSTNAME}"
    if engines is not None:
        spec["engines"] = engines
    agent_dir = tmp_path / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    path = agent_dir / "spec.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": spec,
            },
            sort_keys=False,
        )
    )
    return str(path)


def _env_set(name: str, value: str | None):
    """Set (or unset) ``name`` in the REAL process env; yield; restore it.

    The refusal reads the host environment through the same scitex-config
    cascade the launch uses, so the environment IS the input under test —
    substituting reality here means writing the real ``os.environ`` and
    putting it back, not patching a resolver.
    """
    saved = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield name
    finally:
        if saved is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = saved


@pytest.fixture
def token_exported():
    """Export the engine's auth env var, as a working host would have it."""
    yield from _env_set(_TOKEN_ENV, "sk-test-not-a-real-key")


@pytest.fixture
def token_absent():
    """Guarantee the engine's auth env var is UNSET on this host."""
    yield from _env_set(_TOKEN_ENV, None)


@pytest.fixture
def deepseek_token_exported():
    """Export the registered deepseek provider's auth env var."""
    yield from _env_set("DEEPSEEK_API_KEY", "sk-test-not-a-real-key")


# ---------------------------------------------------------------------------
# Refusal — the engine cannot be honoured
# ---------------------------------------------------------------------------


def test_engine_with_an_unset_auth_env_var_refuses_the_start(
    tmp_path, token_absent
):
    # Arrange
    config = load_config(_write(tmp_path, "unhonourable", _engines()))
    # Act
    act = lambda: select_engine_at_start(  # noqa: E731
        config, "qwen38-27b", log=False
    )
    # Assert
    with pytest.raises(EngineNotHonourableError):
        act()


def test_the_refusal_names_the_engine_key(tmp_path, token_absent):
    # Arrange
    config = load_config(_write(tmp_path, "unhon-key", _engines()))
    # Act
    message = _refusal_text(config, "qwen38-27b")
    # Assert
    assert "'qwen38-27b'" in message


def test_the_refusal_names_the_unresolvable_env_var(tmp_path, token_absent):
    # Arrange
    config = load_config(_write(tmp_path, "unhon-env", _engines()))
    # Act
    message = _refusal_text(config, "qwen38-27b")
    # Assert
    assert f"${_TOKEN_ENV}" in message


def test_the_refusal_states_that_sac_does_not_fall_back(tmp_path, token_absent):
    # Arrange
    config = load_config(_write(tmp_path, "unhon-nofb", _engines()))
    # Act
    message = _refusal_text(config, "qwen38-27b")
    # Assert
    assert "does NOT fall back" in message


def test_a_refused_start_leaves_the_config_on_the_engine_it_refused(
    tmp_path, token_absent
):
    # The refusal must NOT quietly rewind onto the default engine — that
    # would be the fallback wearing an exception as a disguise.
    # Arrange
    config = load_config(_write(tmp_path, "unhon-noswap", _engines()))
    _refusal_text(config, "qwen38-27b")
    # Act
    resolved = config.engine_key
    # Assert
    assert resolved == "qwen38-27b"


def _refusal_text(config, key: str) -> str:
    try:
        select_engine_at_start(config, key, log=False)
    except EngineNotHonourableError as exc:
        return str(exc)
    return ""


def test_the_same_engine_with_its_token_exported_starts_clean(
    tmp_path, token_exported
):
    # POSITIVE CONTROL for every refusal above: the ONLY difference is the
    # exported env var, so the refusals measure honourability rather than
    # an implementation that says no to everything.
    # Arrange
    config = load_config(_write(tmp_path, "honourable", _engines()))
    # Act
    selected = select_engine_at_start(config, "qwen38-27b", log=False)
    # Assert
    assert selected.key == "qwen38-27b"


def test_an_unknown_engine_key_raises_on_the_start_path(tmp_path, token_exported):
    # Arrange
    config = load_config(_write(tmp_path, "start-unknown", _engines()))
    # Act
    act = lambda: select_engine_at_start(config, "gpt-9", log=False)  # noqa: E731
    # Assert
    with pytest.raises(UnknownEngineError):
        act()


def test_an_unknown_engine_key_does_not_leave_the_default_selected(
    tmp_path, token_exported
):
    # POSITIVE CONTROL that the raise is a REFUSAL, not a raise-after-apply:
    # the config must still carry the DEFAULT the loader folded in.
    # Arrange
    config = load_config(_write(tmp_path, "start-unknown-2", _engines()))
    # Act
    key = _engine_key_after_unknown_request(config)
    # Assert
    assert key == "claude"


def _engine_key_after_unknown_request(config) -> str:
    """``config.engine_key`` after an unknown --engine key was refused."""
    try:
        select_engine_at_start(config, "gpt-9", log=False)
    except UnknownEngineError:
        pass
    return config.engine_key


# ---------------------------------------------------------------------------
# Legacy single-backend spec — the unchanged path
# ---------------------------------------------------------------------------


def test_a_legacy_spec_with_no_engine_requested_selects_nothing(tmp_path):
    # Arrange
    config = load_config(_write(tmp_path, "legacy-start"))
    # Act
    selected = select_engine_at_start(config, None, log=False)
    # Assert
    assert selected is None


def test_a_legacy_spec_asked_for_an_engine_refuses_rather_than_ignoring_it(
    tmp_path,
):
    # Arrange
    config = load_config(_write(tmp_path, "legacy-start-engine"))
    # Act
    act = lambda: select_engine_at_start(config, "qwen", log=False)  # noqa: E731
    # Assert
    with pytest.raises(UnknownEngineError):
        act()


# ---------------------------------------------------------------------------
# The three-valued verdict
# ---------------------------------------------------------------------------


def test_an_unregistered_provider_name_is_not_honourable():
    # Arrange
    engine = parse_engines({"engines": {"e": {"provider": "no-such-vendor"}}})["e"]
    # Act
    verdict = static_verdict(engine)
    # Assert
    assert verdict.verdict == VERDICT_NOT_HONOURABLE


def test_an_incomplete_inline_provider_is_not_honourable():
    # Arrange
    engine = parse_engines(
        {"engines": {"e": {"provider": {"base_url": "http://x:1"}}}}
    )["e"]
    # Act
    verdict = static_verdict(engine)
    # Assert
    assert verdict.verdict == VERDICT_NOT_HONOURABLE


def test_a_registered_provider_with_its_token_exported_is_honourable(
    deepseek_token_exported,
):
    # POSITIVE CONTROL for the two verdicts above.
    # Arrange -- the harness is passed because an engine that states none
    # INHERITS the spec's (the harness/engine split); with neither stated
    # the pairing is genuinely undetermined and must say so.
    engine = parse_engines({"engines": {"e": {"provider": "deepseek"}}})["e"]
    # Act
    verdict = static_verdict(engine, "anthropic")
    # Assert
    assert verdict.verdict == VERDICT_HONOURABLE


def test_a_declaration_fault_outranks_an_undetermined_harness(
    deepseek_token_exported,
):
    """A DEFINITE answer beats "I could not tell": an unregistered
    provider name is wrong whatever the harness turns out to be, so the
    undetermined pairing must not hide it."""
    # Arrange
    engine = parse_engines({"engines": {"e": {"provider": "no-such-backend"}}})["e"]
    # Act
    verdict = static_verdict(engine)
    # Assert
    assert verdict.verdict == VERDICT_NOT_HONOURABLE


def test_a_clean_engine_with_no_harness_anywhere_is_could_not_tell(
    deepseek_token_exported,
):
    """...and when nothing definite is found, the undetermined pairing is
    RETURNED, not discarded. "I do not know" never renders as "fine"."""
    # Arrange
    engine = parse_engines({"engines": {"e": {"provider": "deepseek"}}})["e"]
    # Act
    verdict = static_verdict(engine)
    # Assert
    assert verdict.undetermined


def test_an_unresolvable_host_in_base_url_is_could_not_tell_not_a_refusal():
    # A name that cannot resolve is the network declining to answer, which
    # is NOT evidence the endpoint is down. It must land in the third
    # state rather than being collapsed into either certainty.
    # Arrange
    engine = parse_engines(
        {
            "engines": {
                "e": {
                    "provider": {
                        "base_url": "http://sac-engine-probe-no-such-host.invalid:9",
                        "auth_token_env": _TOKEN_ENV,
                    }
                }
            }
        }
    )["e"]
    # Act
    verdict = probe_verdict(engine, timeout_s=0.4)
    # Assert
    assert verdict.verdict == VERDICT_UNKNOWN


def test_a_closed_port_on_localhost_is_a_definite_refusal():
    # POSITIVE CONTROL for the state above: a REFUSED connection is a
    # definite answer and must refuse, so "could not tell" is not simply
    # what the probe always returns.
    # Arrange
    engine = parse_engines(
        {
            "engines": {
                "e": {
                    "provider": {
                        "base_url": "http://127.0.0.1:9",
                        "auth_token_env": _TOKEN_ENV,
                    }
                }
            }
        }
    )["e"]
    # Act
    verdict = probe_verdict(engine, timeout_s=1.0)
    # Assert
    assert verdict.verdict == VERDICT_NOT_HONOURABLE


def test_an_engine_with_no_provider_needs_no_probe():
    # Arrange
    engine = parse_engines({"engines": {"e": {"model": "fable[1m]"}}})["e"]
    # Act
    verdict = probe_verdict(engine, timeout_s=0.1)
    # Assert
    assert verdict.verdict == VERDICT_HONOURABLE


# ---------------------------------------------------------------------------
# Per-engine parameters reach the LAUNCH path
# ---------------------------------------------------------------------------


def test_the_selected_engines_reasoning_effort_lands_on_the_config(
    tmp_path, token_exported
):
    # Arrange
    config = load_config(_write(tmp_path, "params-config", _engines()))
    # Act
    select_engine_at_start(config, "qwen38-27b", log=False)
    # Assert
    assert config.reasoning_effort == "low"


def test_the_selected_engines_reasoning_effort_reaches_the_launch_argv(
    tmp_path, token_exported
):
    # The launch path is ``auth_argv`` — what apptainer is actually told.
    # Arrange
    config = load_config(_write(tmp_path, "params-argv", _engines()))
    select_engine_at_start(config, "qwen38-27b", log=False)
    # Act
    argv = auth_argv(config, tmp_path / "state")
    # Assert
    assert "SAC_ENGINE_REASONING_EFFORT=low" in argv


def test_the_selected_engines_max_context_tokens_reaches_the_launch_argv(
    tmp_path, token_exported
):
    # Arrange
    config = load_config(_write(tmp_path, "params-ctx", _engines()))
    select_engine_at_start(config, "qwen38-27b", log=False)
    # Act
    argv = auth_argv(config, tmp_path / "state")
    # Assert
    assert "SAC_ENGINE_MAX_CONTEXT_TOKENS=393216" in argv


def test_the_engine_key_reaches_the_launch_argv(tmp_path, token_exported):
    # Arrange
    config = load_config(_write(tmp_path, "params-key", _engines()))
    select_engine_at_start(config, "qwen38-27b", log=False)
    # Act
    argv = auth_argv(config, tmp_path / "state")
    # Assert
    assert "SAC_ENGINE=qwen38-27b" in argv


def test_the_default_engine_contributes_no_reasoning_effort_flag(
    tmp_path, token_exported
):
    # POSITIVE CONTROL for the three argv tests: the default engine
    # declares no reasoning_effort, so the flag must be ABSENT — which
    # proves the flags above come from the SELECTED entry rather than
    # being emitted unconditionally.
    # Arrange
    config = load_config(_write(tmp_path, "params-default", _engines()))
    select_engine_at_start(config, None, log=False)
    # Act
    argv = auth_argv(config, tmp_path / "state")
    # Assert
    assert not any(a.startswith("SAC_ENGINE_REASONING_EFFORT") for a in argv)


def test_a_legacy_spec_emits_no_engine_env_flags_at_all(tmp_path):
    # POSITIVE CONTROL for the migration: a legacy single-backend spec's
    # launch argv must be untouched by this axis.
    # Arrange
    config = load_config(_write(tmp_path, "legacy-argv"))
    # Act
    argv = auth_argv(config, tmp_path / "state")
    # Assert
    assert not any(a.startswith("SAC_ENGINE") for a in argv)


def test_an_explicit_engine_overruling_the_spec_default_is_recorded(
    tmp_path, token_exported, caplog
):
    # Arrange
    import logging

    config = load_config(_write(tmp_path, "overruled", _engines()))

    # Act
    with caplog.at_level(logging.WARNING):
        select_engine_at_start(config, "qwen38-27b")

    # Assert
    assert "OVERRULING" in caplog.text


def test_an_explicit_engine_agreeing_with_the_spec_default_is_not_recorded(
    tmp_path, token_exported, caplog
):
    # Arrange
    import logging

    config = load_config(_write(tmp_path, "agreeing", _engines()))

    # Act
    with caplog.at_level(logging.WARNING):
        select_engine_at_start(config, "claude")

    # Assert
    assert "OVERRULING" not in caplog.text


def test_a_start_with_no_explicit_engine_is_not_recorded_as_an_override(
    tmp_path, token_exported, caplog
):
    # Arrange
    import logging

    config = load_config(_write(tmp_path, "plain", _engines()))

    # Act
    with caplog.at_level(logging.WARNING):
        select_engine_at_start(config, None)

    # Assert
    assert "OVERRULING" not in caplog.text


def test_the_override_record_names_the_engine_the_spec_declared(
    tmp_path, token_exported, caplog
):
    # Arrange
    import logging

    config = load_config(_write(tmp_path, "named", _engines()))

    # Act
    with caplog.at_level(logging.WARNING):
        select_engine_at_start(config, "qwen38-27b")

    # Assert
    assert "'claude'" in caplog.text
