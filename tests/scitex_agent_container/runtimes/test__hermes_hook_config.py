"""Native hook bindings must survive model/profile regeneration."""

import pytest
import yaml

from scitex_agent_container.runtimes._hermes_hook_config import preserve_hook_config


def test_profile_regeneration_preserves_hooks_without_old_credentials(tmp_path):
    # Arrange: only hook policy is retained from the previous profile.
    hooks = {
        "pre_tool_call": [
            {"command": "/policy/python -m generic.dispatch", "fail_closed": True}
        ]
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"model": "old", "api_key": "fake-old-key", "hooks": hooks})
    )
    generated = {"model": "new"}
    # Act
    result = preserve_hook_config(tmp_path, generated)
    # Assert
    assert result == {"model": "new", "hooks": hooks} and generated == {"model": "new"}


@pytest.mark.parametrize(
    "body", ["hooks: broken", "hooks: [invalid", "api_key: [fake-unparseable-key"]
)
def test_invalid_profile_hook_input_refuses_regeneration_without_secret_output(
    tmp_path, body
):
    # Arrange
    (tmp_path / "config.yaml").write_text(body)
    # Act
    with pytest.raises(ValueError) as error:
        preserve_hook_config(tmp_path, {})
    # Assert
    assert "fake-unparseable-key" not in str(error.value)


def test_new_profile_does_not_invent_hooks(tmp_path):
    # Arrange
    generated = {"model": "current"}
    # Act
    result = preserve_hook_config(tmp_path, generated)
    # Assert
    assert result == generated
