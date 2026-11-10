"""Native proxy children keep user state and file logging inside owned fixtures."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ._a2a_proxy_workspace import proxy_workspace


@pytest.fixture
def native_child_paths(tmp_path, env_save_restore):
    """Real proxy imports and explicit file logging against owned host-state stand-ins."""
    host_home = tmp_path / "host-home"
    host_home.mkdir()
    host_state = tmp_path / "host-state"
    host_state.mkdir()
    env_save_restore.set("HOME", str(host_home))
    env_save_restore.set("SCITEX_DIR", str(host_state))
    before = (os.environ["HOME"], os.environ["SCITEX_DIR"])
    code = (
        "import json,sys; from pathlib import Path; "
        "from scitex_agent_container._runners.a2a_proxy import logger; "
        "from scitex_logging._handlers import get_default_log_path; "
        "import scitex_logging; "
        "scitex_logging.configure(level='info', enable_console=False, capture_prints=False); "
        "logger.info('owned-proxy-isolation-probe'); "
        "sys.__stdout__.write(json.dumps({'home':str(Path.home()),"
        "'log_path':get_default_log_path()})+'\\n')"
    )
    with proxy_workspace() as (root, child_env):
        result = subprocess.run(
            [sys.executable, "-c", code],
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        report = json.loads(result.stdout.splitlines()[-1])
        yield root, report, host_state, before


def test_native_proxy_child_home_is_owned(native_child_paths):
    # Arrange
    root, report, _host_state, _before = native_child_paths
    # Act
    child_home = Path(report["home"])
    # Assert
    assert child_home.is_relative_to(root) and child_home.is_dir()


def test_native_proxy_logging_does_not_write_host_state(native_child_paths):
    # Arrange
    root, report, host_state, _before = native_child_paths
    # Act
    log = Path(report["log_path"])
    # Assert
    assert (
        log.is_relative_to(root),
        "owned-proxy-isolation-probe" in log.read_text(),
        list(host_state.iterdir()),
    ) == (True, True, [])


def test_proxy_workspace_preserves_parent_environment(native_child_paths):
    # Arrange
    _root, _report, _host_state, before = native_child_paths
    # Act
    parent = (os.environ["HOME"], os.environ["SCITEX_DIR"])
    # Assert
    assert parent == before
