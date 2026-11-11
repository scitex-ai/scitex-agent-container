"""Fresh-process imports must preserve the shared CLI helper callable."""

import json
import os
import subprocess
import sys

import pytest


@pytest.fixture
def helper_import_environment(tmp_path):
    """An owned home; retain only coverage wiring needed by child tests."""
    home = tmp_path / "home"
    home.mkdir()
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "SCITEX_DIR": str(home / ".scitex"),
        "LANG": "C.UTF-8",
    }
    env.update(
        {
            key: os.environ[key]
            for key in ["COVERAGE_PROCESS_START", "COVERAGE_FILE"]
            if key in os.environ
        }
    )
    return env


@pytest.mark.parametrize(
    "prefix",
    [
        "from scitex_agent_container.cli_pkg._helpers._json_flag import _json_flag as direct",
        "from scitex_agent_container.cli_pkg._helpers import _json_flag as first",
        "import scitex_agent_container.cli_pkg._image_disk_cmd",
        "import scitex_agent_container.cli_pkg._host_ssh_options",
    ],
    ids=["submodule-first", "package-first", "disk-first", "ssh-first"],
)
def test_json_flag_remains_callable_after_import_order(
    prefix, helper_import_environment, tmp_path
):
    # Arrange
    code = (
        prefix
        + "\n"
        + """
import json,click
from scitex_agent_container.cli_pkg._helpers import _json_flag
from scitex_agent_container.cli_pkg._helpers._json_flag import _json_flag as direct
context=click.Context(click.Command('owned'),obj={'json':True})
print(json.dumps({'same':_json_flag is direct,'local':_json_flag(context,True),'root':_json_flag(context,False)}))
"""
    )
    # Act
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=helper_import_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Assert
    assert (result.returncode, result.stdout.strip()) == (
        0,
        json.dumps({"same": True, "local": True, "root": True}),
    ), result.stderr


def test_small_json_helper_does_not_import_heavy_agent_inventory(
    helper_import_environment, tmp_path
):
    # Arrange
    code = "import sys;from scitex_agent_container.cli_pkg._helpers import _json_flag;print('scitex_agent_container.cli_pkg._helpers._agent_list' in sys.modules)"
    # Act
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=helper_import_environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Assert
    assert (result.returncode, result.stdout.strip()) == (0, "False")
