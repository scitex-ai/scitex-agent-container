"""The installed console must expose lazy commands before any cache is primed."""

import json
import subprocess
import sys


def _surface(case):
    """Inspect the real installed launcher in a fresh, isolated interpreter."""
    program = """
import json, sys, click
from importlib import metadata
entry = next(item for item in metadata.distribution("scitex-agent-container").entry_points
             if item.group == "console_scripts" and item.name == "sac")
facade = entry.load()
context = click.Context(facade, info_name="sac")
if sys.argv[1] == "names":
    observed = facade.list_commands(context)
    from scitex_agent_container.cli import main
    print(json.dumps({"observed": observed, "expected": main.list_commands(context)}))
else:
    observed = facade.get_command(context, "mcp")
    from scitex_agent_container.cli import main
    expected = main.get_command(context, "mcp")
    assert expected is not None
    print(json.dumps({"present": observed is not None, "same": observed is expected}))
"""
    output = subprocess.check_output(
        [sys.executable, "-I", "-c", program, case], text=True
    )
    return json.loads(output)


def test_installed_console_enumerates_the_real_lazy_commands():
    # Arrange
    case = "names"
    # Act
    surface = _surface(case)
    # Assert
    assert surface["observed"] == surface["expected"]


def test_installed_console_resolves_required_groups_like_the_real_cli():
    # Arrange
    case = "mcp"
    # Act
    surface = _surface(case)
    # Assert
    assert surface == {"present": True, "same": True}
