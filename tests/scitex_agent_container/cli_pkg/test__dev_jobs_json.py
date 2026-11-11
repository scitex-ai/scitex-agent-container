"""JSON status reaches the owning Dev CLI without host service mutations."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import click
import pytest

from scitex_agent_container.cli_pkg import _dev_jobs_backend as backend


@pytest.fixture
def isolated_status_console(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "supervisor-calls.jsonl"
    script = """#!{python}
import json,sys
with open({calls!r}, 'a') as out:
    out.write(json.dumps([sys.argv[0], *sys.argv[1:]])+'\\n')
args=sys.argv[1:]
if 'crontab' in sys.argv[0] and args == ['-l']:
    sys.exit(0)
if any(verb in args for verb in ('is-active','is-system-running','show-environment')):
    print('active')
    sys.exit(0)
sys.stderr.write('fixture refuses non-read supervisor calls\\n')
sys.exit(88)
""".format(python=sys.executable, calls=str(calls))
    for name in ("systemctl", "crontab"):
        p = bindir / name
        p.write_text(script)
        p.chmod(0o700)
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = {
        "PATH": str(bindir)
        + os.pathsep
        + str(Path(sys.executable).parent)
        + os.pathsep
        + os.defpath,
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "SCITEX_DIR": str(home / ".scitex"),
        "XDG_RUNTIME_DIR": str(runtime),
    }
    console = Path(sys.executable).with_name("sac")
    if not console.is_file():
        pytest.fail("the normal installed SAC console entrypoint is required")
    return console, env, calls


@pytest.fixture
def status_job(isolated_status_console):
    """Read the real catalog in the same isolated environment as status."""
    console, env, _ = isolated_status_console
    listing = subprocess.run(
        [str(console), "dev", "timer", "list", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if listing.returncode:
        pytest.fail(listing.stderr)
    return json.loads(listing.stdout)[0]


def test_timer_json_status_uses_real_dev_backend_and_read_only_supervisors(
    isolated_status_console,
    status_job,
):
    # Arrange
    console, env, calls = isolated_status_console
    job = status_job
    # Act
    result = subprocess.run(
        [str(console), "dev", "timer", "status", job["local_name"], "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    payload = json.loads(result.stdout) if result.returncode == 0 else {}
    observations = (
        [json.loads(line) for line in calls.read_text().splitlines()]
        if calls.exists()
        else []
    )
    mutations = {
        "install",
        "enable",
        "disable",
        "start",
        "stop",
        "restart",
        "daemon-reload",
    }
    # Assert
    assert (
        result.returncode,
        payload.get("name"),
        payload.get("kind"),
        any(
            mutations.intersection(call[1:])
            or (Path(call[0]).name == "crontab" and call[1:] != ["-l"])
            for call in observations
        ),
    ) == (0, job["name"], "timer", False)


def test_service_json_status_reaches_name_validation(isolated_status_console):
    # Arrange
    console, env, _ = isolated_status_console
    # Act
    result = subprocess.run(
        [str(console), "dev", "service", "status", "no-such-service", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Assert
    assert result.returncode == 5, result.stderr


@pytest.mark.parametrize("kind", ["service", "timer"])
def test_forwarded_json_status_is_accepted_by_the_installed_owner_parser(kind):
    # Arrange
    delegation = backend.resolve(kind, "status")
    leaf = backend.leaf_command(delegation.path, "status")
    name = "scitex-agent-container-example"
    # Act
    argv = backend.build_argv(delegation, name=name, yes=False, as_json=True, leaf=leaf)
    prefix = ["scitex-dev", "ecosystem", *delegation.path, "status"]
    with leaf.make_context("status", argv[len(prefix) :]) as context:
        values = context.params
    # Assert
    assert (values["name"], values["as_json"]) == (name, True)


def test_json_status_refuses_an_owner_without_json_capability():
    # Arrange
    from scitex_agent_container.cli_pkg._dev_jobs_json import json_arguments

    older_leaf = click.Command("status")
    # Act
    # Assert
    with pytest.raises(click.ClickException, match="does not declare --json"):
        json_arguments(("dev", "timer"), "status", leaf=older_leaf)


@pytest.mark.parametrize("kind,verb", [("timer", "disable"), ("service", "stop")])
def test_json_is_not_advertised_as_a_mutation_option(
    isolated_status_console, kind, verb
):
    # Arrange
    console, env, _ = isolated_status_console
    # Act
    result = subprocess.run(
        [str(console), "dev", kind, verb, "no-such-job", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Assert
    assert (
        result.returncode,
        "No such option" in result.stderr,
        "--json" in result.stderr,
    ) == (2, True, True)
