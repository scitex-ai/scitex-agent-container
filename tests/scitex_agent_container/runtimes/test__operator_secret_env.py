"""Operator secret pass-through into the Hermes profile secret scope.

TDD for the sac-agent-full-permissions slice: the materializer passes the
operator's secret set (names only, resolved from the trusted pool at launch)
into the agent's profile ``.env``. Fake key names/values throughout — no real
secret ever appears here.
"""

from __future__ import annotations

import logging

from scitex_agent_container.runtimes._hermes_profile import operator_secret_env


def _write_envrc(tmp_path, body: str) -> str:
    path = tmp_path / "operator.env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_pass_through_includes_present_pool_keys_by_name(tmp_path, monkeypatch, caplog):
    # Arrange: the pool carries two of the allowlisted names (fake values).
    monkeypatch.setenv(
        "SAC_SECRETS_ENVRC", _write_envrc(tmp_path, "SAC_TEST_ALPHA_KEY=aaa\n")
    )
    names = ("SAC_TEST_ALPHA_KEY", "SAC_TEST_MISSING_KEY")
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._hermes_profile.OPERATOR_SECRET_ENV_NAMES",
        names,
    )
    # Act
    with caplog.at_level(logging.DEBUG):
        out = operator_secret_env()
    # Assert — exactly one entry: the present key, nothing invented.
    assert out == {"SAC_TEST_ALPHA_KEY": "aaa"}


def test_missing_names_warn_by_name_without_values(tmp_path, monkeypatch, caplog):
    # Arrange
    monkeypatch.setenv(
        "SAC_SECRETS_ENVRC", _write_envrc(tmp_path, "SAC_TEST_ALPHA_KEY=aaa\n")
    )
    names = ("SAC_TEST_ALPHA_KEY", "SAC_TEST_MISSING_KEY")
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._hermes_profile.OPERATOR_SECRET_ENV_NAMES",
        names,
    )
    # Act
    with caplog.at_level(logging.DEBUG):
        operator_secret_env()
    # Assert — the missing NAME is reported, its (unknown) value cannot leak.
    assert any("SAC_TEST_MISSING_KEY" in r.getMessage() for r in caplog.records)


def test_values_never_reach_the_log(tmp_path, monkeypatch, caplog):
    # Arrange
    monkeypatch.setenv(
        "SAC_SECRETS_ENVRC",
        _write_envrc(tmp_path, "SAC_TEST_ALPHA_KEY=super-secret-aaa\n"),
    )
    names = ("SAC_TEST_ALPHA_KEY",)
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._hermes_profile.OPERATOR_SECRET_ENV_NAMES",
        names,
    )
    # Act
    with caplog.at_level(logging.DEBUG):
        operator_secret_env()
    # Assert
    assert not any("super-secret-aaa" in r.getMessage() for r in caplog.records)
