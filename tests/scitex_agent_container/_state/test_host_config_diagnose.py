"""A MISSING config.yaml must not be indistinguishable from a valid one.

Regression guard for 2026-07-30: ``sac host validate`` returned
``errors: []`` while naming a config path that did not exist, so every
``sac host probe`` failed with "peer is not defined" while the validator
called the configuration clean.
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._state.host_config import load
from scitex_agent_container._state.host_config_diagnose import (
    STATE_ABSENT,
    STATE_EMPTY,
    STATE_MALFORMED,
    STATE_POPULATED,
    STATE_UNREADABLE,
    config_state_problems,
    describe_config_resolution,
    diagnose_host_config,
)

_TWO_PEERS = """\
peers:
  nas:
    ssh: nas
  spartan:
    ssh: spartan
"""

_CONTAINER_HOME = "/home/agent"


def _set_env(name: str, value: str | None):
    """Set/clear an env var, returning the previous value for restoration."""
    prev = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return prev


@pytest.fixture
def container_home():
    """Real ``$HOME`` mutation — the container case differs ONLY by $HOME."""
    prev = _set_env("HOME", _CONTAINER_HOME)
    yield _CONTAINER_HOME
    _set_env("HOME", prev)


@pytest.fixture
def explicit_override(tmp_path):
    """Set the documented override env var to a concrete path."""
    target = tmp_path / "override.yaml"
    prev = _set_env("SCITEX_AGENT_CONTAINER_CONFIG", str(target))
    yield target
    _set_env("SCITEX_AGENT_CONTAINER_CONFIG", prev)


@pytest.fixture
def scitex_dir_set():
    prev = _set_env("SCITEX_DIR", "/some/root")
    yield "/some/root"
    _set_env("SCITEX_DIR", prev)


@pytest.fixture
def populated_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_TWO_PEERS, encoding="utf-8")
    return cfg


@pytest.fixture
def no_peers_config(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("host:\n  canonical: solo\n", encoding="utf-8")
    return cfg


def test_absent_config_reports_state_absent(tmp_path):
    # Arrange
    missing = tmp_path / "nope" / "config.yaml"
    # Act
    state, _peers, _resolved = diagnose_host_config(missing)
    # Assert
    assert state == STATE_ABSENT


def test_absent_config_reports_zero_peers(tmp_path):
    # Arrange
    missing = tmp_path / "nope" / "config.yaml"
    # Act
    _state, peers, _resolved = diagnose_host_config(missing)
    # Assert
    assert peers == 0


def test_absent_config_echoes_back_the_path_it_looked_for(tmp_path):
    # Arrange
    missing = tmp_path / "nope" / "config.yaml"
    # Act
    _state, _peers, resolved = diagnose_host_config(missing)
    # Assert
    assert resolved == missing


def test_absent_config_produces_exactly_one_error(tmp_path):
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    errors, _warnings, _detail = config_state_problems(missing)
    # Assert
    assert len(errors) == 1


def test_absent_config_error_names_the_resolved_path(tmp_path):
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    errors, _warnings, _detail = config_state_problems(missing)
    # Assert
    assert str(missing) in errors[0]


def test_absent_config_produces_no_warning(tmp_path):
    """It is an ERROR, not a warning — a warning would keep exit code 0."""
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    _errors, warnings, _detail = config_state_problems(missing)
    # Assert
    assert warnings == []


def test_absent_config_error_names_home_as_the_discriminator(tmp_path, container_home):
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    errors, _warnings, _detail = config_state_problems(missing)
    # Assert
    assert container_home in errors[0]


def test_absent_config_error_names_the_override_lever(tmp_path):
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    errors, _warnings, _detail = config_state_problems(missing)
    # Assert
    assert "SCITEX_AGENT_CONTAINER_CONFIG" in errors[0]


def test_populated_config_yields_no_errors(populated_config):
    # Arrange: state prepared by the fixture
    # Act
    errors, _warnings, _detail = config_state_problems(populated_config)
    # Assert
    assert errors == []


def test_populated_config_yields_no_warnings(populated_config):
    # Arrange: state prepared by the fixture
    # Act
    _errors, warnings, _detail = config_state_problems(populated_config)
    # Assert
    assert warnings == []


def test_populated_config_reports_state_populated(populated_config):
    # Arrange: state prepared by the fixture
    # Act
    _errors, _warnings, detail = config_state_problems(populated_config)
    # Assert
    assert detail["state"] == STATE_POPULATED


def test_populated_config_counts_its_peers(populated_config):
    # Arrange: state prepared by the fixture
    # Act
    _errors, _warnings, detail = config_state_problems(populated_config)
    # Assert
    assert detail["peers"] == 2


def test_present_but_no_peers_is_not_an_error(no_peers_config):
    """A single-host install is SUPPORTED — failing it would teach operators to
    ignore the check, which is how a gate stops being read at all."""
    # Arrange: state prepared by the fixture
    # Act
    errors, _warnings, _detail = config_state_problems(no_peers_config)
    # Assert
    assert errors == []


def test_present_but_no_peers_warns(no_peers_config):
    # Arrange: state prepared by the fixture
    # Act
    _errors, warnings, _detail = config_state_problems(no_peers_config)
    # Assert
    assert len(warnings) == 1


def test_present_but_no_peers_reports_state_empty(no_peers_config):
    # Arrange: state prepared by the fixture
    # Act
    _errors, _warnings, detail = config_state_problems(no_peers_config)
    # Assert
    assert detail["state"] == STATE_EMPTY


def test_empty_file_is_empty_not_malformed(tmp_path):
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text("", encoding="utf-8")
    # Act
    state, _peers, _resolved = diagnose_host_config(cfg)
    # Assert
    assert state == STATE_EMPTY


def test_empty_peers_block_is_empty_not_populated(tmp_path):
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text("peers: {}\n", encoding="utf-8")
    # Act
    state, _peers, _resolved = diagnose_host_config(cfg)
    # Assert
    assert state == STATE_EMPTY


@pytest.mark.parametrize(
    "body",
    [
        "peers:\n  - nas\n",  # peers is a list, not a mapping
        "just a string\n",  # top level is not a mapping
        "[1, 2, 3]\n",  # top level is a list
        "peers: {unclosed\n",  # not parseable at all
    ],
)
def test_wrong_shapes_are_malformed(tmp_path, body):
    # Arrange
    cfg = tmp_path / "config.yaml"
    cfg.write_text(body, encoding="utf-8")
    # Act
    state, _peers, _resolved = diagnose_host_config(cfg)
    # Assert
    assert state == STATE_MALFORMED


def test_a_directory_is_unreadable_not_absent(tmp_path):
    """Present-but-unusable must not be reported as "not there"."""
    # Arrange
    as_dir = tmp_path / "config.yaml"
    as_dir.mkdir()
    # Act
    state, _peers, _resolved = diagnose_host_config(as_dir)
    # Assert
    assert state == STATE_UNREADABLE


def test_resolution_report_echoes_the_override_value(explicit_override):
    # Arrange: state prepared by the fixture
    # Act
    report = describe_config_resolution()
    # Assert
    assert report["override_value"] == str(explicit_override)


def test_resolution_report_resolves_to_the_override(explicit_override):
    # Arrange: state prepared by the fixture
    # Act
    report = describe_config_resolution()
    # Assert
    assert report["resolved"] == str(explicit_override)


def test_resolution_report_echoes_scitex_dir(scitex_dir_set):
    # Arrange: state prepared by the fixture
    # Act
    report = describe_config_resolution()
    # Assert
    assert report["scitex_dir_env"] == scitex_dir_set


def test_resolution_report_names_home(container_home):
    # Arrange: state prepared by the fixture
    # Act
    report = describe_config_resolution()
    # Assert
    assert report["home"] == container_home


def test_the_schema_validator_alone_cannot_see_an_absent_config(tmp_path):
    """Pins the MECHANISM, not just the symptom.

    ``load()`` maps a missing file onto the same defaults as a present one, so
    ``Config.validate()`` has nothing to complain about — that is precisely why
    the state diagnosis has to exist. If someone later makes ``load()`` itself
    reject a missing file, this test fails and points at the redundancy rather
    than leaving two half-guards each assuming the other does the work.
    """
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    cfg = load(missing)
    # Assert
    assert cfg.validate() == []


def test_the_diagnosis_is_what_catches_the_absent_config(tmp_path):
    # Arrange
    missing = tmp_path / "config.yaml"
    # Act
    state, _peers, _resolved = diagnose_host_config(missing)
    # Assert
    assert state == STATE_ABSENT
