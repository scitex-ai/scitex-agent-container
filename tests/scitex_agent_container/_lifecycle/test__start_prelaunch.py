"""The selected start-time engine owns auth, ahead of credential rotation."""

from pathlib import Path

import pytest
import yaml

from scitex_agent_container._creds import NoHealthyAccountError
from scitex_agent_container._lifecycle._engine_select import EngineNotHonourableError
from scitex_agent_container._lifecycle._start_prelaunch import run_prelaunch
from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_types import UnknownEngineError
from tests.scitex_agent_container._helpers.spec_authority import (
    establish_test_spec_authority,
)
from tests.scitex_agent_container._lifecycle.test__engine_select import (
    _TOKEN_ENV,
    _engines,
    _write,
)


def _config_with_expired_default_auth(tmp_path: Path, env_save_restore):
    env_save_restore.set("HOME", str(tmp_path))
    stale = tmp_path / "accounts" / "expired" / ".credentials.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"claudeAiOauth":{"expiresAt":1}}')
    path = Path(_write(tmp_path, "prelaunch", _engines()))
    doc = yaml.safe_load(path.read_text())
    doc["spec"]["claude"]["credentials_file"] = str(stale)
    doc["spec"]["a2a"]["port"] = None
    path.write_text(yaml.safe_dump(doc, sort_keys=False))
    establish_test_spec_authority(path)
    return load_config(str(path)), str(path)


def _prelaunch(config, path: str, engine: str | None) -> None:
    run_prelaunch(
        config, path, strict_drift=None, session_override=None,
        resume_id_override=None, engine_override=engine, probe_engine=False,
        one_shot=False, dry_run=True,
    )


def test_selected_provider_engine_is_not_blocked_by_default_oauth(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — a real, authoritative spec defaults to an expired Claude pin.
    config, path = _config_with_expired_default_auth(tmp_path, env_save_restore)
    env_save_restore.set(_TOKEN_ENV, "provider-fixture-value")
    # Act — the start requests the valid provider engine instead.
    _prelaunch(config, path, "qwen38-27b")
    # Assert
    assert config.engine_key == "qwen38-27b"


def test_selected_provider_engine_still_refuses_its_missing_key(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — neither unrelated default auth nor selected provider is usable.
    config, path = _config_with_expired_default_auth(tmp_path, env_save_restore)
    env_save_restore.delete(_TOKEN_ENV)
    # Act
    def start():
        _prelaunch(config, path, "qwen38-27b")
    # Assert
    with pytest.raises(EngineNotHonourableError, match="qwen38-27b"):
        start()


def test_unknown_selected_engine_refuses_without_falling_back_to_default_auth(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange
    config, path = _config_with_expired_default_auth(tmp_path, env_save_restore)
    # Act
    def start():
        _prelaunch(config, path, "unknown-engine")
    # Assert
    with pytest.raises(UnknownEngineError, match="unknown-engine"):
        start()


def test_default_claude_engine_still_refuses_its_expired_pin(
    tmp_path: Path, env_save_restore,
) -> None:
    # Arrange — leaving the engine unstated really selects the Claude default.
    config, path = _config_with_expired_default_auth(tmp_path, env_save_restore)
    # Act
    def start():
        _prelaunch(config, path, None)
    # Assert
    with pytest.raises(NoHealthyAccountError):
        start()
