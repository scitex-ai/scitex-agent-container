"""No row may carry credential material, and no error may quote it.

Every fake below is built from repeated characters on purpose. A test
suite that contains a real credential shape is itself a leak, and these
strings end up in CI logs.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._credstate._material import (
    CredentialMaterialError,
    assert_no_material,
    find_material,
)

FAKE_ANTHROPIC = "sk-ant-" + "A" * 40
FAKE_JWT = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
FAKE_PEM = "-----BEGIN OPENSSH PRIVATE KEY-----"
FAKE_TELEGRAM = "123456789:" + "B" * 35
FAKE_GITHUB = "ghp_" + "C" * 36
FAKE_ENTROPY = "D" * 50

CLEAN_ROW = {
    "row_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e",
    "origin_node": "scitex-nas-03",
    "revision": 1,
    "cred_key": "anthropic-oauth:ywatanabe@scitex.ai",
    "kind": "oauth_session",
    "account": "ywatanabe",
    "tier": "primary_secret",
    "primary_node": "scitex-nas-03",
    "locator": "file:/home/agent/.claude/.credentials.json",
    "refresh_command": "sac accounts refresh --all --include-active",
    "note": "held on the fleet's single refresh holder",
}


@pytest.mark.parametrize(
    "field",
    ["refresh_token", "access_token", "api_key", "password", "secret"],
)
def test_a_field_named_for_material_is_refused_at_the_top_level(field):
    # Arrange
    row = {"cred_key": "k", field: "whatever"}
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError, match=field):
        assert_no_material(row, what="row")


def test_a_material_named_field_nested_in_a_dict_is_still_found():
    # Arrange — the guard must not depend on nesting shape.
    row = {"cred_key": "k", "meta": {"inner": {"refresh_token": "x"}}}
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError, match="refresh_token"):
        assert_no_material(row, what="row")


def test_a_material_named_field_nested_in_a_list_is_still_found():
    # Arrange
    row = {"cred_key": "k", "items": [{"api_key": "x"}]}
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError, match="api_key"):
        assert_no_material(row, what="row")


@pytest.mark.parametrize(
    "value",
    [FAKE_ANTHROPIC, FAKE_JWT, FAKE_PEM, FAKE_TELEGRAM, FAKE_GITHUB, FAKE_ENTROPY],
)
def test_secret_shaped_values_are_refused_even_in_an_innocent_field(value):
    # Arrange — the realistic case: nobody adds a column called
    # refresh_token, they paste a token into `note`.
    row = {"cred_key": "k", "note": value}
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        assert_no_material(row, what="row")


@pytest.mark.parametrize(
    "value",
    [FAKE_ANTHROPIC, FAKE_JWT, FAKE_TELEGRAM, FAKE_GITHUB, FAKE_ENTROPY],
)
def test_the_refusal_message_never_quotes_the_offending_value(value):
    # Arrange — this message reaches logs and transcripts.
    row = {"cred_key": "k", "note": value}
    message = ""
    # Act
    try:
        assert_no_material(row, what="row")
    except CredentialMaterialError as exc:
        message = str(exc)
    # Assert — non-empty proves it refused; absence proves it stayed quiet.
    assert message and value not in message


def test_a_realistic_clean_descriptor_row_is_accepted():
    # Arrange
    row = dict(CLEAN_ROW)
    # Act
    result = assert_no_material(row, what="row")
    # Assert
    assert result is None


def test_an_env_locator_is_accepted():
    # Arrange — naming the variable is the design, not a leak.
    row = {"cred_key": "k", "locator": "env:CCT_BOT_TOKEN_3"}
    # Act
    result = assert_no_material(row, what="row")
    # Assert
    assert result is None


def test_a_long_file_locator_is_accepted_despite_an_unbroken_segment():
    # Arrange — locators legitimately hold long paths.
    long_segment = "e" * 60
    row = {"cred_key": "k", "locator": f"file:/home/agent/{long_segment}/creds.json"}
    # Act
    result = assert_no_material(row, what="row")
    # Assert
    assert result is None


def test_a_locator_carrying_a_provider_prefix_is_still_refused():
    # Arrange — the entropy waiver never waives an explicit provider shape.
    row = {"cred_key": "k", "locator": f"file:/tmp/{FAKE_ANTHROPIC}"}
    # Act
    # Assert
    with pytest.raises(CredentialMaterialError):
        assert_no_material(row, what="row")


def test_a_uuid_is_not_mistaken_for_a_key():
    # Arrange
    row = {"row_uuid": "0f8fad5b-d9cb-469f-a165-70867728950e"}
    # Act
    offences = find_material(row)
    # Assert
    assert offences == []


def test_a_dotted_identifier_is_not_mistaken_for_a_key():
    # Arrange
    row = {"note": "scitex_agent_container._account.token_keepalive.keepalive_push"}
    # Act
    offences = find_material(row)
    # Assert
    assert offences == []


def test_the_reported_path_locates_the_offending_field():
    # Arrange
    row = {"meta": {"inner": {"note": FAKE_ANTHROPIC}}}
    # Act
    offences = find_material(row)
    # Assert
    assert offences[0].startswith("meta.inner.note")
