"""Observation reads presence, never value — and never destroys what it reads.

Real files in ``tmp_path`` throughout. The fixture token strings are
distinctive so the "never returns the value" test can prove absence
rather than assume it.
"""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from scitex_agent_container._credstate._observe import observe_locator, parse_locator

FAKE_ACCESS = "FAKEACCESS0000000000"
FAKE_REFRESH = "FAKEREFRESH111111111"
EXPIRY_MS = 2_000_000_000_000


def _write_creds(tmp_path, *, refresh: bool = True, mode: int = 0o600, nested=False):
    payload = {"accessToken": FAKE_ACCESS, "expiresAt": EXPIRY_MS}
    if refresh:
        payload["refreshToken"] = FAKE_REFRESH
    body = {"deep": {"deeper": payload}} if nested else {"claudeAiOauth": payload}
    path = tmp_path / ".credentials.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    path.chmod(mode)
    return path


def test_a_file_locator_is_split_into_scheme_and_path():
    # Arrange
    locator = "file:/home/agent/.claude/.credentials.json"
    # Act
    scheme, rest = parse_locator(locator)
    # Assert
    assert (scheme, rest) == ("file", "/home/agent/.claude/.credentials.json")


def test_an_env_locator_is_split_into_scheme_and_variable_name():
    # Arrange
    locator = "env:CCT_BOT_TOKEN_3"
    # Act
    scheme, rest = parse_locator(locator)
    # Assert
    assert (scheme, rest) == ("env", "CCT_BOT_TOKEN_3")


def test_an_unschemed_locator_is_not_guessed_at():
    # Arrange — guessing could report PRESENT from an unrelated file.
    locator = "/home/agent/.claude/.credentials.json"
    # Act
    scheme, _rest = parse_locator(locator)
    # Assert
    assert scheme is None


def test_an_unknown_scheme_is_not_guessed_at():
    # Arrange
    locator = "vault://secret/path"
    # Act
    scheme, _rest = parse_locator(locator)
    # Assert
    assert scheme is None


def test_a_missing_file_is_reported_absent(tmp_path):
    # Arrange
    locator = f"file:{tmp_path / 'nope.json'}"
    # Act
    observation = observe_locator(locator)
    # Assert
    assert observation.present is False


def test_a_missing_file_detail_names_the_path(tmp_path):
    # Arrange
    missing = tmp_path / "nope.json"
    # Act
    observation = observe_locator(f"file:{missing}")
    # Assert
    assert str(missing) in (observation.detail or "")


def test_a_present_credential_file_is_reported_present(tmp_path):
    # Arrange
    path = _write_creds(tmp_path)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.present is True


def test_the_permission_bits_are_recorded(tmp_path):
    # Arrange — mode is a real exposure finding recorded nowhere today.
    path = _write_creds(tmp_path, mode=0o600)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.file_mode == "0600"


def test_an_owner_only_file_is_not_world_readable(tmp_path):
    # Arrange
    path = _write_creds(tmp_path, mode=0o600)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.world_readable is False


def test_a_group_and_world_readable_file_is_flagged(tmp_path):
    # Arrange — this is the mode the live artifact was found in.
    path = _write_creds(tmp_path, mode=0o644)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.world_readable is True


def test_the_flagged_file_reports_its_actual_mode(tmp_path):
    # Arrange
    path = _write_creds(tmp_path, mode=0o644)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.file_mode == "0644"


def test_refresh_material_present_is_detected(tmp_path):
    # Arrange — the one-bit "am I the origin" test.
    path = _write_creds(tmp_path, refresh=True)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.holds_refresh_material is True


def test_refresh_material_absent_is_detected(tmp_path):
    # Arrange — an access-only replica.
    path = _write_creds(tmp_path, refresh=False)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.holds_refresh_material is False


def test_the_artifacts_own_expiry_is_read(tmp_path):
    # Arrange — a fact about the token, not about whether a timer ran.
    path = _write_creds(tmp_path)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.artifact_expires_at.timestamp() == EXPIRY_MS / 1000.0


def test_the_expiry_is_timezone_aware_utc(tmp_path):
    # Arrange
    path = _write_creds(tmp_path)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.artifact_expires_at.tzinfo == timezone.utc


def test_an_expiry_at_a_different_nesting_depth_is_still_found(tmp_path):
    # Arrange — dialect shape is exactly what varies between versions.
    path = _write_creds(tmp_path, nested=True)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.artifact_expires_at is not None


def test_a_present_but_unparseable_file_is_still_reported_present(tmp_path):
    # Arrange — presence is the answer that matters most.
    path = tmp_path / "creds.json"
    path.write_text("not json at all", encoding="utf-8")
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.present is True


def test_a_present_but_unparseable_file_admits_it_could_not_be_read(tmp_path):
    # Arrange
    path = tmp_path / "creds.json"
    path.write_text("not json at all", encoding="utf-8")
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert observation.holds_refresh_material is None


def test_a_set_environment_variable_is_reported_present():
    # Arrange
    env = {"CCT_BOT_TOKEN_3": "value"}
    # Act
    observation = observe_locator("env:CCT_BOT_TOKEN_3", env=env)
    # Assert
    assert observation.present is True


def test_an_unset_environment_variable_is_reported_absent():
    # Arrange
    env = {}
    # Act
    observation = observe_locator("env:CCT_BOT_TOKEN_3", env=env)
    # Assert
    assert observation.present is False


def test_an_unset_variable_detail_names_the_variable():
    # Arrange
    env = {}
    # Act
    observation = observe_locator("env:CCT_BOT_TOKEN_3", env=env)
    # Assert
    assert "CCT_BOT_TOKEN_3" in (observation.detail or "")


def test_an_unresolvable_locator_says_so_rather_than_reporting_absent():
    # Arrange — "cannot check" and "missing" are different answers.
    locator = "vault://secret/path"
    # Act
    observation = observe_locator(locator)
    # Assert
    assert "unresolvable" in (observation.detail or "")


@pytest.mark.parametrize("secret", [FAKE_ACCESS, FAKE_REFRESH])
def test_no_observed_field_ever_carries_the_token_value(tmp_path, secret):
    # Arrange — the whole safety property of this module in one assertion.
    path = _write_creds(tmp_path)
    # Act
    observation = observe_locator(f"file:{path}")
    # Assert
    assert secret not in repr(observation)
