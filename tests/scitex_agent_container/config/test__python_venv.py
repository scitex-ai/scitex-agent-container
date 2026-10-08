"""Container private paths defer to the target without relaxing host paths."""

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._python_venv import resolve_python_venv
from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec


@pytest.mark.parametrize("value", ["/uvwork/venv-agent", ["/uvwork/venv-agent"]])
def test_resolve_python_venv_defers_declared_private_container_path(value):
    # Arrange
    declaration = value
    # Act
    result = resolve_python_venv(declaration, container_namespace=True)
    # Assert
    assert result == "/uvwork/venv-agent"


@pytest.mark.parametrize("value", ["/not-a-venv", "/uvwork/not-a-venv"])
def test_resolve_python_venv_still_refuses_other_missing_absolute_paths(value):
    # Arrange
    declaration = value
    # Act
    refusal = pytest.raises(RuntimeError, match="bin/activate")
    # Assert
    with refusal:
        resolve_python_venv(declaration, container_namespace=True)


def test_resolve_python_venv_keeps_explicit_private_choice_before_host_alternate():
    # Arrange
    declaration = ["/uvwork/venv-agent", "/not-a-venv"]
    # Act
    result = resolve_python_venv(declaration, container_namespace=True)
    # Assert
    assert result == "/uvwork/venv-agent"


def test_resolve_python_venv_retains_host_refusal_outside_container_namespace():
    # Arrange
    declaration = "/uvwork/not-a-venv"
    # Act
    refusal = pytest.raises(RuntimeError, match="bin/activate")
    # Assert
    with refusal:
        resolve_python_venv(declaration)


@pytest.mark.parametrize("runtime", ["tui", "apptainer"])
def test_load_config_preserves_private_target_path_for_container_runtime(
    tmp_path, runtime
):
    # Arrange
    path = tmp_path / "private-agent" / "spec.yaml"
    path.parent.mkdir()
    path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "metadata": {"labels": {}},
                "spec": explicit_spec(
                    {
                        "runtime": runtime,
                        "host": "${HOSTNAME}",
                        "python-venv": "/uvwork/venv-agent",
                    }
                ),
            }
        )
    )
    # Act
    result = load_config(path)
    # Assert
    assert result.python_venv == "/uvwork/venv-agent"
