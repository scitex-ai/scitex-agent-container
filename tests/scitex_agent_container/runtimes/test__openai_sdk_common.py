"""Tests for ``runtimes/_openai_sdk_common.py`` (openai-compat-2).

Covers the auth-provisioning precedence contract (SAC env wins →
process env fallback → loud failure), model resolution, and the
``_provider_common`` re-exports. None of this needs the
``openai-agents`` SDK — the module is import-safe on Claude-only
deployments by design, and these tests prove it by never importing
``agents``.

STATE-DB PLACEMENT IS NO LONGER ONE OF THE CONCERNS. Seven tests here
pinned ``resolve_state_db_path`` — the directory, the filename, the
name sanitiser, the override — and they went with the function when
the OpenAI runner's conversation state moved to PostgreSQL. Nothing
replaced them at this level: a store TARGET is not a path, and the
behaviour that used to be checked here is now checked against a real
store in ``_runners/test_openai_session.py`` under ``pg_schema``.

PA-306: no ``monkeypatch`` / ``unittest.mock`` — env mutations use an
explicit save/restore fixture (the ``isolated_env`` pattern from
``tests/.../a2a/test__handlers.py``). STX-TQ002 AAA + STX-TQ007
one-assert-per-test.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container.runtimes import _openai_sdk_common as m
from scitex_agent_container.runtimes import _provider_common
from scitex_agent_container.runtimes._openai_sdk_common import (
    OpenAISDKCommonError,
    default_openai_model,
    provision_openai_auth,
)

_ENV_KEYS = ("SAC_OPENAI_API_KEY", "OPENAI_API_KEY", "SAC_OPENAI_MODEL")


@pytest.fixture
def clean_env():
    """Save the OpenAI-auth env keys, scrub them, restore on teardown."""
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    for k in _ENV_KEYS:
        os.environ.pop(k, None)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# provision_openai_auth — precedence contract
# ---------------------------------------------------------------------------


def test_provision_sac_env_returns_sac_env_path(clean_env) -> None:
    # Arrange
    os.environ["SAC_OPENAI_API_KEY"] = "sk-test-sac"
    # Act
    path = provision_openai_auth()
    # Assert
    assert path == "sac_env"


def test_provision_sac_env_mirrors_into_openai_api_key(clean_env) -> None:
    # Arrange
    os.environ["SAC_OPENAI_API_KEY"] = "sk-test-sac"
    # Act
    provision_openai_auth()
    # Assert
    assert os.environ["OPENAI_API_KEY"] == "sk-test-sac"


def test_provision_sac_env_overwrites_preexisting_openai_api_key(clean_env) -> None:
    # Arrange
    os.environ["OPENAI_API_KEY"] = "sk-test-stale"
    os.environ["SAC_OPENAI_API_KEY"] = "sk-test-sac"
    # Act
    provision_openai_auth()
    # Assert
    assert os.environ["OPENAI_API_KEY"] == "sk-test-sac"


def test_provision_process_env_returns_process_env_path(clean_env) -> None:
    # Arrange
    os.environ["OPENAI_API_KEY"] = "sk-test-process"
    # Act
    path = provision_openai_auth()
    # Assert
    assert path == "process_env"


def test_provision_process_env_leaves_value_untouched(clean_env) -> None:
    # Arrange
    os.environ["OPENAI_API_KEY"] = "sk-test-process"
    # Act
    provision_openai_auth()
    # Assert
    assert os.environ["OPENAI_API_KEY"] == "sk-test-process"


def test_provision_without_any_key_raises(clean_env) -> None:
    # Arrange
    raised: OpenAISDKCommonError | None = None
    # Act
    try:
        provision_openai_auth()
    except OpenAISDKCommonError as exc:
        raised = exc
    # Assert
    assert raised is not None


def test_provision_error_message_names_the_sac_env_var(clean_env) -> None:
    # Arrange
    raised: OpenAISDKCommonError | None = None
    # Act
    try:
        provision_openai_auth()
    except OpenAISDKCommonError as exc:
        raised = exc
    # Assert
    assert "SAC_OPENAI_API_KEY" in str(raised)


# ---------------------------------------------------------------------------
# default_openai_model
# ---------------------------------------------------------------------------


def test_default_model_unset_returns_none(clean_env) -> None:
    # Arrange
    # Act
    model = default_openai_model()
    # Assert
    assert model is None


def test_default_model_env_value_is_returned(clean_env) -> None:
    # Arrange
    os.environ["SAC_OPENAI_MODEL"] = "gpt-4o-mini"
    # Act
    model = default_openai_model()
    # Assert
    assert model == "gpt-4o-mini"


def test_default_model_whitespace_only_returns_none(clean_env) -> None:
    # Arrange
    os.environ["SAC_OPENAI_MODEL"] = "   "
    # Act
    model = default_openai_model()
    # Assert
    assert model is None


# ---------------------------------------------------------------------------
# Re-exports — the workspace helpers ARE _provider_common's (no fork)
# ---------------------------------------------------------------------------


def test_resolve_agent_workspace_is_provider_common_reexport() -> None:
    # Arrange
    # Act
    same = m.resolve_agent_workspace is _provider_common.resolve_agent_workspace
    # Assert
    assert same is True


def test_project_runtime_root_is_provider_common_reexport() -> None:
    # Arrange
    # Act
    same = m.project_runtime_root is _provider_common.project_runtime_root
    # Assert
    assert same is True
