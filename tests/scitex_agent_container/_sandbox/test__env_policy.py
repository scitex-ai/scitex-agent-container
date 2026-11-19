"""Env seal: which names may enter a sealed run, and the cross-field refusals."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from scitex_agent_container._sandbox._env_policy import (
    allowed_env_names,
    filter_env,
    seal_profile_env,
    validate_sandbox_config,
)
from scitex_agent_container._sandbox._spec import parse_sandbox


def _cfg(*, env=None, overlay="", accounts=None, raw_args=None):
    sb = parse_sandbox(
        {
            "sandbox": {
                "egress": {"allow": ["opencode.ai:443"]},
                "env": {"allow": ["EXTRA_ONE"], "model_key": "MODEL_KEY"},
                "run": {"root": "/scratch/x", "size": "1G", "after": "remove"},
            }
        }
    )
    ap = SimpleNamespace(
        env=env or {}, overlay=overlay, relaxed=False, raw_args=raw_args or []
    )
    return SimpleNamespace(
        name="a",
        sandbox=sb,
        apptainer=ap,
        hermes_failover=SimpleNamespace(accounts=accounts or {}),
    )


def test_spec_env_keys_are_allowed():
    # Arrange
    cfg = _cfg(env={"FROM_SPEC": "1"})
    # Act
    names = allowed_env_names(cfg)
    # Assert
    assert {"FROM_SPEC", "EXTRA_ONE", "MODEL_KEY", "HERMES_HOME"} <= names


def test_fleet_identity_is_not_allowed():
    # Arrange
    cfg = _cfg()
    # Act
    names = allowed_env_names(cfg)
    # Assert
    assert (
        not {"SCITEX_CARDS_AGENT_ID", "PGUSER", "SCITEX_AGENT_CONTAINER_YAML_DIRS"}
        & names
    )


def test_unsandboxed_config_allows_nothing():
    # Arrange
    cfg = SimpleNamespace(sandbox=None)
    # Act
    names = allowed_env_names(cfg)
    # Assert
    assert names == frozenset()


def test_filter_env_reports_dropped_names_only():
    # Arrange
    pairs = {"MODEL_KEY": "v", "OTHER_TOKEN": "s"}
    # Act
    got = filter_env(pairs, frozenset({"MODEL_KEY"}))
    # Assert
    assert got == ({"MODEL_KEY": "v"}, ["OTHER_TOKEN"])


def test_second_failover_key_is_refused():
    # Arrange
    cfg = _cfg(accounts={"engine": ["MODEL_KEY", "MODEL_KEY_2"]})
    # Act
    # Assert
    with pytest.raises(ValueError, match="ONE model key"):
        validate_sandbox_config(cfg)


def test_single_failover_key_is_accepted():
    # Arrange
    cfg = _cfg(accounts={"engine": ["MODEL_KEY"]})
    # Act
    got = validate_sandbox_config(cfg)
    # Assert
    assert got is None


def test_spec_overlay_is_refused():
    # Arrange
    cfg = _cfg(overlay="/scratch/x/ov")
    # Act
    # Assert
    with pytest.raises(ValueError, match="writable layer"):
        validate_sandbox_config(cfg)


@pytest.mark.parametrize(
    "flag", ["--net", "--network=bridge", "--overlay", "--workdir"]
)
def test_raw_args_cannot_reopen_the_seal(flag):
    # Arrange
    cfg = _cfg(raw_args=[flag])
    # Act
    # Assert
    with pytest.raises(ValueError, match="raw_args"):
        validate_sandbox_config(cfg)


def test_profile_env_keeps_only_the_model_key():
    # Arrange
    cfg = _cfg()
    env = {"MODEL_KEY": "v", "GITHUB_TOKEN": "g", "SAC_LISTEN_BEARER": "b"}
    # Act
    kept = seal_profile_env(cfg, env, auth_env="MODEL_KEY")
    # Assert
    assert kept == {"MODEL_KEY": "v"}


def test_profile_env_refuses_another_engine_key():
    # Arrange
    cfg = _cfg()
    # Act
    # Assert
    with pytest.raises(ValueError, match="model_key"):
        seal_profile_env(cfg, {"K2": "v"}, auth_env="K2")
