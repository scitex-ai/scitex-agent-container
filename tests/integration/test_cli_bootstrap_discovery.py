"""Cold console facade discovery uses the real owner's public Click protocol."""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def cold_child(tmp_path, program):
    home = tmp_path / "home"
    home.mkdir()
    passfile = home / "empty.pgpass"
    passfile.touch(mode=0o600)
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "PYTHONPATH": str(ROOT / "src"),
        "SCITEX_STORE_DSN": "postgresql://sac_tests@127.0.0.1:1/tests_must_not_write_to_the_fleet_store",
        "PGPASSFILE": str(passfile),
        "PGHOST": "127.0.0.1",
        "PGPORT": "1",
        "PGDATABASE": "tests_must_not_write_to_the_fleet_store",
        "PGUSER": "sac_tests",
    }
    preamble = """import sys
sys.argv=['sac']
def deny_connections(event,args):
 if event=='socket.connect':raise RuntimeError('CLI discovery must not connect')
sys.addaudithook(deny_connections)
"""
    return subprocess.run(
        [sys.executable, "-c", preamble + program],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_cold_facade_lists_real_names_without_importing_command_modules(tmp_path):
    # Arrange
    program = """import click,json,sys
from _scitex_agent_container_bootstrap import cli_entry_point as facade
from scitex_agent_container.cli import main
before=set(sys.modules)
names=facade.list_commands(click.Context(facade))
print(json.dumps({'same_names':names==main.list_commands(click.Context(main)),
 'introspection':all(n in names for n in ('list-python-apis','mcp')),
 'new_modules':sorted(set(sys.modules)-before),'count':len(names)}))
"""
    # Act
    result = cold_child(tmp_path, program)
    data = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else {}
    # Assert
    assert (
        result.returncode,
        data.get("same_names"),
        data.get("introspection"),
        data.get("new_modules"),
        data.get("count", 0) > 0,
    ) == (0, True, True, [], True), result.stderr


def test_public_discovery_preserves_nested_real_commands_and_json_flags(tmp_path):
    # Arrange
    program = """import click,json
from _scitex_agent_container_bootstrap import cli_entry_point as facade
from scitex_agent_container.cli import main
context=click.Context(facade)
names=facade.list_commands(context)
commands={name:facade.get_command(context,name) for name in names}
api=commands.get('list-python-apis');mcp=commands.get('mcp')
with click.Context(mcp,parent=context) as child:
 nested={name:mcp.get_command(child,name) for name in mcp.list_commands(child)}
tools=nested.get('list-tools')
print(json.dumps({'same_api':api is main.get_command(context,'list-python-apis'),
 'same_mcp':mcp is main.get_command(context,'mcp') and mcp is facade.get_command(ctx=context,cmd_name='mcp'),
 'same_tools':tools is mcp.commands.get('list-tools'),
 'api_json':any('--json' in p.opts for p in api.params),
 'tools_json':any('--json' in p.opts for p in tools.params)}))
"""
    # Act
    result = cold_child(tmp_path, program)
    data = json.loads(result.stdout.splitlines()[-1]) if result.returncode == 0 else {}
    # Assert
    assert (result.returncode, data) == (
        0,
        {
            "same_api": True,
            "same_mcp": True,
            "same_tools": True,
            "api_json": True,
            "tools_json": True,
        },
    ), result.stderr


def test_public_discovery_keeps_unknown_command_refusal(tmp_path):
    # Arrange
    program = """import click,json
from _scitex_agent_container_bootstrap import cli_entry_point as facade
print(json.dumps({'unknown':facade.get_command(click.Context(facade),'not-a-real-sac-command') is None}))
"""
    # Act
    result = cold_child(tmp_path, program)
    # Assert
    assert (result.returncode, result.stdout.strip()) == (0, '{"unknown": true}'), (
        result.stderr
    )
