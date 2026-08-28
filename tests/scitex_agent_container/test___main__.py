"""Tests for ``python -m scitex_agent_container`` (``__main__.py``).

The module entry point must route through ``cli_entry_point`` — the ONE
place the host-side process is handed its store identity (fleet DSN +
``PGUSER``, via ``apply_fleet_defaults_to_process``). The 2026-08-28
post-merge audit found it calling the bare Click group instead, which
reproduces the ``fe_sendauth: no password supplied`` failure class for any
store-opening subcommand launched as ``python -m scitex_agent_container
...`` — and ``runtimes/a2a_sidecar.py`` spawns exactly that form in
production.

No mocks: one subprocess run of the real module entry with a scrubbed
env, plus a source-level routing assertion whose limits are stated
inline.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import scitex_agent_container

_PKG_DIR = Path(scitex_agent_container.__file__).resolve().parent
_MAIN_PY = _PKG_DIR / "__main__.py"


def _scrubbed_env(tmp_path: Path) -> dict[str, str]:
    """A bare-host-shell env: no SAC_* / store-identity variables.

    ``PYTHONPATH`` is pinned to the package under test because pytest's
    ``pythonpath = ["src"]`` mutates only THIS process's ``sys.path`` —
    a child interpreter would otherwise import the installed copy, not
    the one these tests cover. Coverage keys pass through so the child
    is counted (see tests/conftest.py's subprocess-coverage wiring).
    """
    env = {
        "HOME": str(tmp_path),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONPATH": str(_PKG_DIR.parent),
    }
    for key in ("COVERAGE_PROCESS_START", "COVERAGE_FILE"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def test_module_entry_help_exits_zero_from_a_bare_env(tmp_path: Path) -> None:
    # Arrange — the exact production spelling (a2a_sidecar launches
    # `sys.executable -m scitex_agent_container ...`), from a shell that
    # carries no store identity — the environment the audit's repro used.
    env = _scrubbed_env(tmp_path)
    # Act
    result = subprocess.run(
        [sys.executable, "-m", "scitex_agent_container", "--help"],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # Assert
    assert result.returncode == 0, result.stderr


def test_module_entry_routes_through_cli_entry_point() -> None:
    # Arrange — source-level assertion, deliberately: proving the DSN+PGUSER
    # injection END-TO-END needs a live Postgres store this suite does not
    # have, and `--help` exits 0 with or without it. LIMIT: this pins only
    # the ROUTING (__main__ calls cli_entry_point()); cli_entry_point's own
    # call to apply_fleet_defaults_to_process is pinned separately by
    # tests/scitex_agent_container/runtimes/test__fleet_env.py — together
    # the two cover the chain without a mock.
    source = _MAIN_PY.read_text(encoding="utf-8")
    # Act
    calls_entry_point = "cli_entry_point()" in source
    # Assert
    assert calls_entry_point, (
        "__main__.py must invoke cli_entry_point() — the bare Click group "
        "skips the process's store-identity injection (fe_sendauth class)"
    )
