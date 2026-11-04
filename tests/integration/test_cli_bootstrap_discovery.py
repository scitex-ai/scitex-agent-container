"""The guarded console facade advertises the real lazy command surface."""

import json
import os
import subprocess
import sys
from pathlib import Path


def test_pristine_bootstrap_advertises_required_lazy_discovery_commands(tmp_path):
    # Arrange
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "SCITEX_DIR": str(tmp_path / "state"),
        "PYTHONPATH": str(source) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    script = """import json,click
import _scitex_agent_container_bootstrap as bootstrap
facade = bootstrap.cli_entry_point
context = click.Context(facade)
print(json.dumps(facade.list_commands(context)))
"""
    # Act
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    advertised = set(json.loads(result.stdout)) if result.returncode == 0 else set()
    # Assert
    assert (result.returncode, {"list-python-apis", "mcp"} <= advertised) == (0, True)


def test_bootstrap_resolves_real_json_catalog_commands_without_executing_them(tmp_path):
    # Arrange
    source = Path(__file__).resolve().parents[2] / "src"
    environment = {
        **os.environ,
        "HOME": str(tmp_path),
        "SCITEX_DIR": str(tmp_path / "state"),
        "PYTHONPATH": str(source) + os.pathsep + os.environ.get("PYTHONPATH", ""),
    }
    script = """import json,click
import _scitex_agent_container_bootstrap as bootstrap
from scitex_agent_container.cli import main
facade = bootstrap.cli_entry_point
context = click.Context(facade)
python_api = facade.get_command(context,"list-python-apis")
mcp = facade.get_command(context,"mcp")
mcp_context = click.Context(mcp,parent=context)
list_tools = mcp.get_command(mcp_context,"list-tools")
print(json.dumps({"same_python_command":python_api is main.get_command(context,"list-python-apis"),"same_mcp_group":mcp is main.get_command(context,"mcp"),"json_flags":[any("--json" in parameter.opts for parameter in command.params) for command in (python_api,list_tools)]}))
"""
    # Act
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )
    observed = json.loads(result.stdout) if result.returncode == 0 else result.stderr
    # Assert
    assert (result.returncode, observed) == (
        0,
        {
            "same_python_command": True,
            "same_mcp_group": True,
            "json_flags": [True, True],
        },
    )
